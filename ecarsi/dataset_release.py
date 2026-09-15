"""Publish v2 unit results on a Pool worker, with an exact cross-round cell ledger."""
import argparse
import json
from pathlib import Path
import shutil

from .agent_session import reference, verified
from .crosssample_v2 import artifact
from .warm_pool.state import lock, read, save


def collect(unit_ref):
    import pandas as pd
    from .ledger import _obs, _cell_ids, _partition, _table
    from .run_state import file_identity
    from .sample_mapping import SAMPLE_KEY

    unit = verified(unit_ref)
    if unit['state'] != 'complete' or not unit['rounds']:
        raise ValueError('Release requires a completed analysis unit')
    source = Path(unit['unit']['path']) / 'input/organized.h5ad'
    if file_identity(source) != verified(unit['unit']['manifest'])['identity']:
        raise ValueError('Organize input changed')
    original = _obs(source, ['source_unit', 'eca_source_cell_id', SAMPLE_KEY])
    ledger = original.rename(columns={'source_unit': 'source_id', 'eca_source_cell_id': 'source_cell_id',
                                      SAMPLE_KEY: 'sample_id'}).copy()
    ledger.index.name = 'cell_uid'
    if 'sample_id' not in ledger:
        ledger['sample_id'] = ''
    for column in ('source_id', 'source_cell_id'):
        if column not in ledger or ledger[column].isna().any():
            raise ValueError('Missing original cell identity: ' + column)
    alive = _cell_ids(original.index, 'Organize')
    exclusions, stages, decisions = [], [], []

    def decision(bundle, name, number, stage, scope=''):
        if name in bundle['files']:
            ref = bundle['files'][name]
            decisions.append(dict(round=number, stage=stage, scope=scope, source=ref, value=verified(ref)))

    def stage(name, label, current, removed, expected, number, source_ref, counts):
        nonlocal alive
        _partition(expected, current.index, removed.cell_uid, name)
        if (counts['n_input'], counts['n_survived'], counts['n_removed']) != (len(expected), len(current), len(removed)):
            raise ValueError('Publication cell counts disagree with artifacts: ' + name)
        if removed.reason.str.strip().eq('').any():
            raise ValueError('Every exclusion needs a reason: ' + name)
        for column in ('source_id', 'source_cell_id'):
            if removed[column].tolist() != ledger.loc[removed.cell_uid, column].astype(str).tolist():
                raise ValueError('Excluded source cell identity changed: ' + name)
        for column, original_column in (('source_id', 'source_unit'), ('source_cell_id', 'eca_source_cell_id')):
            if current[original_column].astype(str).tolist() != ledger.loc[current.index, column].astype(str).tolist():
                raise ValueError('Surviving source cell identity changed: ' + name)
        status = name + '_status'
        ledger[status] = ''
        ledger.loc[current.index, status] = 'kept'
        # Keep graph labels short; full unmodified reasons belong in the exclusion ledger.
        ledger.loc[removed.cell_uid, status] = 'removed:' + name
        for suffix, column in (('coarse', label + '_coarse'), ('fine', label + '_fine')):
            ledger[name + '_' + suffix] = ''
            if len(current) and (column not in current or current[column].isna().any()
                                 or current[column].astype(str).str.strip().eq('').any()):
                raise ValueError('Missing survivor annotation: ' + column)
            if column in current:
                ledger.loc[current.index, name + '_' + suffix] = current[column].astype(str)
        stages.append((name, name + '_coarse', status))
        removed = removed.copy()
        removed['release_stage'], removed['round'] = name, number
        removed['publication'] = source_ref['path']
        exclusions.append(removed)
        alive = set(current.index)

    labels = ['source_unit', 'eca_source_cell_id', '_ann_coarse', '_ann_fine',
              'msp_ann_coarse', 'msp_ann_fine', 'zmip_ann_coarse', 'zmip_ann_fine']
    per = verified(unit['per_sample'])
    if per['state'] != 'complete' or per['failed_samples']:
        raise ValueError('Per-sample is incomplete')
    ref = per['partition_exclusions']
    if reference(ref['path']) != ref:
        raise ValueError('Partition exclusions changed')
    omitted = _table(Path(ref['path']), ['cell_id', 'excluded_reason']).rename(
        columns={'cell_id': 'cell_uid', 'excluded_reason': 'reason'})
    _cell_ids(omitted.cell_uid, 'partition exclusions')
    for column in ('source_id', 'source_cell_id'):
        omitted[column] = ledger.loc[omitted.cell_uid, column].astype(str).to_numpy()
    omitted['operation'] = 'persample.partition'
    removed, kept = [omitted], []
    for sample_ref in per['samples']:
        bundle = verified(sample_ref)
        gone = _table(artifact(bundle, 'cell_exclusions.csv.gz'), ['cell_uid', 'reason', 'source_id', 'source_cell_id'])
        cells = _obs(artifact(bundle, 'clustered.h5ad'), labels) if not bundle['empty'] else original.iloc[:0]
        inputs = _table(artifact(bundle, 'input_cells.csv.gz'), ['cell_id'])
        _partition(_cell_ids(inputs.cell_id, 'sample input'), cells.index, gone.cell_uid, bundle['sample'])
        ledger.loc[inputs.cell_id, 'sample_id'] = bundle['sample']
        removed.append(gone)
        kept.append(cells)
        decision(bundle, 'annotation_proposal.json', 0, 'per-sample', bundle['sample'])
    stage('per-sample', '_ann', pd.concat(kept) if kept else original.iloc[:0], pd.concat(removed),
          alive, 0, unit['per_sample'], per)
    previous = unit['per_sample']
    for number, round_ref in enumerate(unit['rounds'], 1):
        record = verified(round_ref)
        if record['round'] != number:
            raise ValueError('Rounds must be consecutive')
        for kind, filename, prefix in (('cross_sample', 'annotated.h5ad', 'msp_ann'),
                                        ('zoom_in', 'annotated_zmip.h5ad', 'zmip_ann')):
            ref = record[kind]
            bundle = verified(ref)
            if bundle['state'] != 'complete' or bundle['input'] != previous:
                raise ValueError('Broken publication dependency chain')
            final_path = artifact(bundle, filename)
            current = _obs(final_path, labels)
            gone = _table(artifact(bundle, 'cell_exclusions.csv.gz'), ['cell_uid', 'reason', 'source_id', 'source_cell_id'])
            name = f'round{number:02d}.' + kind.replace('_', '-')
            stage(name, prefix, current, gone, alive, number, ref, bundle)
            for filename in ('annotation_proposal.json', 'inspection_proposal.json', 'zmip_plan.json'):
                decision(bundle, filename, number, kind.replace('_', '-'))
            for lineage_ref in bundle.get('lineages', []):
                lineage = verified(lineage_ref)
                decision(lineage, 'annotation_proposal.json', number, 'zoom-in', lineage['lineage']['name'])
            previous = ref
    if previous != unit['final'] or (unit['n_input'], unit['n_survived'], unit['n_removed']) != (
            len(ledger), len(alive), len(ledger) - len(alive)):
        raise ValueError('Final unit publication does not conserve its original input')
    exclusions = pd.concat(exclusions, ignore_index=True).fillna('')
    _partition(set(ledger.index), alive, exclusions.cell_uid, 'release')
    ledger['final_status'] = 'removed'
    ledger.loc[list(alive), 'final_status'] = 'kept'
    return unit, ledger, exclusions, stages, decisions, final_path


def review_items(unit, exclusions, decisions):
    """Reuse review records; count actual removed cells, not proposed cluster sizes."""
    from .review import Item, _loop_items, _annotation_items, _mark_recurring
    items = []
    for ref in unit['rounds']:
        record = verified(ref)
        items += _loop_items(record['round'], record['stats'], unit['forced_release'], record['round'] == len(unit['rounds']))
    for (number, stage), rows in exclusions.groupby(['round', 'release_stage'], sort=False):
        uncertain = []
        for row in rows.itertuples():
            # OSP rule reasons are plain strings; later reasons contain decision evidence.
            try:
                reason = json.loads(row.reason)
            except json.JSONDecodeError:
                continue
            def low(value):
                if isinstance(value, dict):
                    return value.get('confidence') in {'low', 'medium'} or any(low(v) for v in value.values())
                return isinstance(value, list) and any(low(v) for v in value)
            if low(reason):
                uncertain.append(row.cell_uid)
        if uncertain:
            items.append(Item('removed', int(number), stage, n_cells=len(uncertain), action='remove',
                note='Removal evidence includes medium or low confidence; see cell_exclusions.csv.gz.',
                link='cell_exclusions.csv.gz'))
    for entry in decisions:
        prop = entry['value']
        # Zoom-in stores separate resolution-1 type and resolution-2 quality decisions.
        typed = prop.get('types', prop)
        if entry['stage'] == 'per-sample':
            typed = {**typed, 'clusters': [{**c, 'cluster_id': c['cluster'],
                'coarse_label': c.get('label_coarse', ''), 'fine_label': c.get('label_fine', ''),
                'action': 'keep'} for c in typed.get('clusters', [])]}
        if typed.get('clusters') and all('cluster_id' in c for c in typed['clusters']):
            items += [item for item in _annotation_items(entry['round'], entry['stage'], entry['scope'],
                typed, {}, {}, entry['source']['path']) if item.kind != 'removed']
        quality = prop.get('quality', prop if 'inspection' in Path(entry['source']['path']).name else {})
        for cluster in quality.get('clusters', []):
            for flag in cluster.get('decisions', [cluster]):
                if flag.get('action') != 'remove' and (flag.get('action') == 'flag'
                        or flag.get('verdict') == 'ambiguous' or flag.get('confidence') == 'low'):
                    items.append(Item('inspect_flag', entry['round'], entry['stage'], entry['scope'],
                        str(cluster.get('cluster_id', cluster.get('cluster', ''))),
                        action=flag.get('action', ''), confidence=flag.get('confidence', ''),
                        note=flag.get('rationale', ''), link=entry['source']['path']))
        for warning in prop.get('host_warnings', []):
            items.append(Item('plan_warning', entry['round'], entry['stage'], entry['scope'],
                note=str(warning), link=entry['source']['path']))
        for line in prop.get('lineages', []):
            if not line['zoom']:
                items.append(Item('lineage_skipped', entry['round'], entry['stage'], line['name'],
                    n_cells=line['n_cells'], note=line.get('reason', ''), link=entry['source']['path']))
    _mark_recurring(items)
    return items


def publish(unit_ref):
    """Commit once. A retry validates the existing receipt and never reruns science."""
    from .release_state import publication, recover
    from .ledger import sankey_data
    from .umapdata import write_umap_json
    from .review import to_json, to_markdown
    root = Path(unit_ref['path']).parent
    with lock(root / 'release.lock'):
        recover(root)
        target = root / 'release'
        previous = read(target / 'receipt.json')
        if previous:
            if previous['input'] != unit_ref:
                raise ValueError('Cannot replace a release from another unit publication')
            for name, identity in previous['files'].items():
                if reference(target / name)['sha256'] != identity:
                    raise ValueError('Released artifact changed: ' + name)
            return reference(target / 'receipt.json')
        unit, ledger, excluded, stages, decisions, final = collect(unit_ref)
        with publication(root) as destination:
            # Copy: release consumers must not be able to mutate the accepted worker artifact.
            shutil.copy2(final, destination / 'final.h5ad')
            if reference(destination / 'final.h5ad')['sha256'] != verified(unit['final'])['files']['annotated_zmip.h5ad']['sha256']:
                raise ValueError('Final H5AD changed while copying')
            compression = {'method': 'gzip', 'mtime': 0}
            ledger.to_csv(destination / 'cell_ledger.csv.gz', compression=compression)
            excluded.to_csv(destination / 'cell_exclusions.csv.gz', index=False, compression=compression)
            save(destination / 'sankey.json', sankey_data(ledger, stages))
            save(destination / 'decisions.json', decisions)
            items = review_items(unit, excluded, decisions)
            (destination / 'needs_review.json').write_text(to_json(items))
            markdown = to_markdown(items, unit['unit']['name'], len(unit['rounds']))
            markdown = markdown.replace('Everything the agents were unsure about or the host overrode',
                'Removal confidence, annotation, quality, convergence and lineage-plan advisories')
            (destination / 'needs_review.md').write_text(markdown)
            write_umap_json(final, destination / 'umap.json')
            save(destination / 'summary.json', dict(unit=unit['unit']['name'], rounds=len(unit['rounds']),
                forced=unit['forced_release'], reason=unit['reason'], final_h5ad='final.h5ad',
                input_cells=unit['n_input'], final_cells=unit['n_survived'], removed_cells=unit['n_removed']))
            save(destination / 'receipt.json', dict(state='complete', input=unit_ref,
                files={p.name: reference(p)['sha256'] for p in sorted(destination.iterdir()) if p.is_file()}))
        return reference(target / 'receipt.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', type=Path)
    args = parser.parse_args()
    packet = read(args.packet)
    save(Path.cwd() / 'released.json', dict(state='complete', input=packet['input'], release=publish(packet['input'])))
