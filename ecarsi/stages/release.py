"""Publish v2 unit results on a Pool worker, with an exact cross-round cell ledger."""
import argparse
import json
from pathlib import Path
import shutil

from ..files import reference, verified
from .common import RETAINED, STRESS_HOST_POLICY, artifact, sealed
from ..files import lock, read, save
from ..contracts import check


def collect(unit_ref, *, complete=True):
    """The ledger of every input cell across the stages run so far.

    `complete=False` accepts a unit that is still looping -- `unit_ref` is then the
    round's own view {unit, per_sample, rounds} rather than a unit publication -- so a
    round can publish its ledger and Sankey while the loop continues."""
    import pandas as pd
    from .ledger import _obs, _cell_ids, _partition, _table
    from ..run_state import file_identity
    from ..sample_mapping import SAMPLE_KEY

    unit = check('unit', verified(unit_ref)) if complete else unit_ref
    if complete and (unit['state'] != 'complete' or not unit['rounds']):
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
    ledger['retained_state'], ledger['retained_stage'] = '', ''  # the stress policy's keeps (decision 0017)
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
        if RETAINED in current:  # the stage that first kept a cell under the stress policy
            state = current[RETAINED].astype(object).fillna('').astype(str)
            new = state.index[state.ne('').to_numpy() & ledger.loc[state.index, 'retained_stage'].eq('').to_numpy()]
            ledger.loc[new, 'retained_state'], ledger.loc[new, 'retained_stage'] = state[new], name
        stages.append((name, name + '_coarse', status))
        removed = removed.copy()
        removed['release_stage'], removed['round'] = name, number
        removed['publication'] = source_ref['path']
        exclusions.append(removed)
        alive = set(current.index)

    labels = ['source_unit', 'eca_source_cell_id', '_ann_coarse', '_ann_fine',
              'msp_ann_coarse', 'msp_ann_fine', 'zmip_ann_coarse', 'zmip_ann_fine', RETAINED]
    per = check('per-sample', verified(unit['per_sample']))
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
        record = check('round', verified(round_ref))
        if record['round'] != number:
            raise ValueError('Rounds must be consecutive')
        for kind, filename, prefix in (('cross_sample', 'annotated.h5ad', 'msp_ann'),
                                        ('zoom_in', 'annotated_zmip.h5ad', 'zmip_ann')):
            ref = record[kind]
            bundle = check('stage', verified(ref))
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
    if complete and (previous != unit['final'] or (unit['n_input'], unit['n_survived'], unit['n_removed']) != (
            len(ledger), len(alive), len(ledger) - len(alive))):
        raise ValueError('Final unit publication does not conserve its original input')
    exclusions = pd.concat(exclusions, ignore_index=True).fillna('')
    _partition(set(ledger.index), alive, exclusions.cell_uid, 'release')
    ledger['final_status'] = 'removed'
    ledger.loc[list(alive), 'final_status'] = 'kept'
    return unit, ledger, exclusions, stages, decisions, final_path


def reassign_items(entry, quality):
    """Zoom-in reassignments live in the resolution-2 quality decisions, not in the type proposal the
    annotation reader sees; without them the 'recurs' alarm never fired on this path (2026-09-24)."""
    from ..review import Item
    items = []
    for cluster in quality.get('clusters', []):
        for flag in cluster.get('decisions', []):
            if flag.get('action') != 'reassign':
                continue
            bounce = flag.get('recurring') or {}
            note = (f"[already moved in {bounce['round']}, {bounce['share']:.0%} of cells] " if bounce else '') + flag.get('rationale', '')
            items.append(Item('reassigned', entry['round'], entry['stage'], entry['scope'],
                str(cluster.get('cluster_id', '')) + ':' + ','.join(flag.get('type_clusters', [])),
                label=str(flag.get('fine_label', '')), action='→ ' + str(flag.get('reassign_to', '')),
                confidence=str(flag.get('confidence', '')), note=note, link=entry['source'].get('link', entry['source']['path']),
                extra={'reassign_to': flag.get('reassign_to', '')}))
    return items


LOW_KEEP_MIN_CELLS = 20  # a zoom-in keep at low confidence over fewer cells is not worth a review line (#45)


def flag_items(entry, quality):
    """inspect_flag items. Cross-sample verdicts that were flagged, ambiguous or low get a line each; a zoom-in QC
    cluster's keeps at low confidence share one line, without the type intersections of fewer than
    LOW_KEEP_MIN_CELLS cells (#45: one decision per intersection made 30-123 lines of "single cell, keep")."""
    from ..review import Item
    link = entry['source'].get('link', entry['source']['path'])
    cells = intersection_cells(entry['source']['path'])
    items = []
    for cluster in quality.get('clusters', []):
        cid, unsure = str(cluster.get('cluster_id', cluster.get('cluster', ''))), []
        for flag in cluster.get('decisions', [cluster]):
            if 'decisions' in cluster and flag.get('action') == 'keep' and flag.get('confidence') == 'low':
                n = None if cells is None else sum(cells.get((cid, t), 0) for t in flag.get('type_clusters', []))
                if n is None or n >= LOW_KEEP_MIN_CELLS:
                    unsure.append((flag, n))
            elif flag.get('action') != 'remove' and (flag.get('action') == 'flag'
                    or flag.get('verdict') == 'ambiguous' or flag.get('confidence') == 'low'):
                items.append(Item('inspect_flag', entry['round'], entry['stage'], entry['scope'], cid,
                    action=flag.get('action', ''), confidence=flag.get('confidence', ''),
                    note=flag.get('rationale', ''), link=link))
        if unsure:
            sizes = [n for _, n in unsure if n is not None]
            items.append(Item('inspect_flag', entry['round'], entry['stage'], entry['scope'], cid,
                n_cells=sum(sizes) if sizes else None, action='keep', confidence='low', link=link,
                note=' | '.join(f"types {','.join(f.get('type_clusters', []))}" + (f' ({n} cells)' if n is not None else '')
                                + ': ' + f.get('rationale', '')[:200] for f, n in unsure)))
    return items


def intersection_cells(proposal_path):
    """{(QC cluster, type cluster): cells} from the type_quality_intersections.csv a zoom-in lineage writes beside its
    proposal; None without one."""
    import csv
    path = Path(proposal_path).parent / 'type_quality_intersections.csv'
    if not path.is_file():
        return None
    with open(path, newline='') as f:
        header, *rows = list(csv.reader(f))
    return {(row[0], t): int(float(v)) for row in rows for t, v in zip(header[1:], row[1:]) if v}


def type_proposal(prop):
    """The per-cluster type decisions of a stage's proposal. Zoom-in stores separate resolution-1 type
    and resolution-2 quality decisions under `types` / `quality`; a cross-sample inspection proposal is
    the type proposal itself, even when the agent added a per-cluster `types` list of its own beside
    `clusters` (2 of 1,338 proposals on 2026-09-24; one failed the release of pansci-lung_WT_p5of5)."""
    return prop['types'] if isinstance(prop.get('types'), dict) else prop


def local_link(root, entry):
    """A decision's copy inside the unit, as a unit-relative link: the pool request that holds the original
    is pruned once the run finishes, which left these links dead (#26). The stages copy their proposals next
    to their publications (control/artifacts.py); without an identical copy the original path is kept."""
    from .. import layout as L
    from ..files import file_digest
    name = Path(entry['source']['path']).name
    if entry['stage'] == 'per-sample':
        rel = Path(L.GEN2_PERSAMPLE) / entry['scope'] / name
    else:
        rel = Path(L.ROUNDS) / f"round{entry['round']:02d}" / (L.GEN2_CROSS if entry['stage'] == 'cross-sample' else L.GEN2_ZOOM)
        if entry['stage'] == 'zoom-in' and entry['scope']:
            from zmip.api import slug
            rel = rel / slug(entry['scope'])
        rel = rel / name
    copy = Path(root) / rel
    return str(rel) if copy.is_file() and file_digest(copy) == entry['source']['sha256'] else entry['source']['path']


def stress_retained_items(ledger, decisions):
    """needs_review 'stress_retained': released cells the stress policy kept, by the stage that kept them, their
    state and their coarse label there, with the code's evidence notes of that round and stage."""
    from ..review import Item
    notes = {}
    for entry in decisions:
        value = entry['value']
        for group in value.get('clusters', []) + value.get('quality', {}).get('clusters', []):
            for decision in group.get('decisions', [group]):
                adjustment = decision.get('host_adjustment', {})
                if adjustment.get('policy') == STRESS_HOST_POLICY:
                    where = (entry['scope'] + ' ' if entry['scope'] else '') + 'cluster ' + str(group.get('cluster_id', ''))
                    notes.setdefault((entry['round'], entry['stage'], adjustment['state']), []).append(where + ': ' + adjustment['evidence'])
    items = []
    kept = ledger[ledger.final_status.eq('kept') & ledger.retained_stage.ne('')]
    for (name, state), rows in kept.groupby(['retained_stage', 'retained_state']):
        number, step = int(name[5:7]), name.split('.', 1)[1]
        why = notes.get((number, step, state), [])
        for label, n in rows[name + '_coarse'].value_counts().items():
            items.append(Item('stress_retained', number, step, label=str(label), n_cells=int(n), action='keep',
                              note=state + ('; ' + ' | '.join(why[:3]) if why else '')))
    return items


def review_items(unit, exclusions, decisions, root=None, ledger=None):
    """Reuse review records; count actual removed cells, not proposed cluster sizes. `root` is the unit
    directory, where decisions link to their local copies."""
    from .. import layout as L
    import re
    from ..degraded import read as read_degraded
    from ..review import Item, _loop_items, _annotation_items, _mark_recurring
    from ..run_state import read_json
    items = []
    exclusions_link = f'{L.RELEASE}/cell_exclusions.csv.gz'  # links are unit-relative (review.Item.link)
    # steps that failed without failing the run, kept under <run>/degraded/ (ecarsi.degraded)
    for record in (read_degraded(Path(root).parent.parent) if root is not None else []):
        if record.get('unit') in (None, Path(root).name):
            number = re.search(r'round(\d+)', record.get('stage', ''))
            items.append(Item('degraded', int(number.group(1)) if number else 0, record.get('stage', ''),
                record.get('scope', ''), note=f"{record['what']}: {record['error']}"))
    for ref in unit['rounds']:
        record = verified(ref)
        items += _loop_items(record['round'], record['stats'], unit['forced_release'], record['round'] == len(unit['rounds']))
    # sources ECA-PP rejected: organize skipped them; the whole dataset's inventory is in its organize manifest
    organized = L.organize_manifest(Path(unit['unit']['path']).parents[1]) if 'unit' in unit else None
    for source in (read_json(organized).get('source_inventory', []) if organized and organized.is_file() else []):
        if source.get('state') == 'rejected':
            items.append(Item('upstream_review', 0, 'organize', source.get('name', ''), action='skip',
                note='ECA-PP rejected this source (status rejected, exit code 2); organize skipped it'))
    if 'operation' in exclusions:
        for reason, rows in exclusions[exclusions.operation.eq('persample.partition')].groupby('reason', sort=True):
            items.append(Item('policy_excluded', 0, 'per-sample', str(reason), n_cells=len(rows), action='remove',
                note='excluded before OSP by the sample map (spec organize.sample_map)', link=exclusions_link))
    per_sample = verified(unit['per_sample'])
    for entry in per_sample.get('skipped_samples', []):
        items.append(Item('agent_skipped', 0, 'per-sample', entry['sample'], n_cells=entry['n_cells'],
            note='annotation agent failed twice; survivors kept unannotated: ' + str(entry.get('error', ''))[:300],
            link=f"{L.GEN2_PERSAMPLE}/{entry['sample']}/report.html"))
    for ref in per_sample.get('samples', []):  # gene names genesets.is_mito does not know (decision 0020)
        bundle = verified(ref)
        if str(bundle.get('validation', {}).get('qc_summary', {}).get('n_mito_genes')) == '0':
            items.append(Item('upstream_review', 0, 'per-sample', bundle['sample'],
                note='no mitochondrial gene recognised: pct_counts_mt is 0 and the mitochondrial QC filters saw nothing',
                link=f"{L.GEN2_PERSAMPLE}/{bundle['sample']}/report.html"))
    for (number, stage), rows in exclusions.groupby(['round', 'release_stage'], sort=False):
        uncertain, fragments, excluded = [], {}, {}  # excluded: whole samples the inclusion agent kept out (#46)
        for row in rows.itertuples():
            # OSP rule reasons are plain strings; later reasons contain decision evidence.
            try:
                reason = json.loads(row.reason)
            except json.JSONDecodeError:
                continue
            for r in reason if isinstance(reason, list) else []:
                if isinstance(r, dict) and r.get('code') == 'fragment_qc':  # #27: removed without an agent, by test
                    tests = ', '.join((r.get('detail') or {}).get('tests', [])) or 'tests not recorded'
                    fragments[tests] = fragments.get(tests, 0) + 1
                elif isinstance(r, dict) and r.get('code') == 'sample_excluded':
                    sample = str(getattr(row, 'sample_id', '') or '')
                    excluded[sample] = (excluded.get(sample, (0, ''))[0] + 1, str(r.get('detail') or ''))
            def low(value):
                if isinstance(value, dict):
                    return value.get('confidence') in {'low', 'medium'} or any(low(v) for v in value.values())
                return isinstance(value, list) and any(low(v) for v in value)
            if low(reason):
                uncertain.append(row.cell_uid)
        if uncertain:
            items.append(Item('removed', int(number), stage, n_cells=len(uncertain), action='remove',
                note='Removal evidence includes medium or low confidence; see cell_exclusions.csv.gz.',
                link=exclusions_link))
        for tests, n in sorted(fragments.items()):
            items.append(Item('fragment_removed', int(number), stage, n_cells=n, action='remove',
                note=f'msp minor-sibling fragment QC: {tests}', link=exclusions_link))
        for sample, (n, detail) in sorted(excluded.items()):
            items.append(Item('sample_excluded', int(number), stage, scope=sample, n_cells=n, action='exclude',
                note=detail[:300], link=exclusions_link))
    for entry in decisions:
        if root is not None:
            entry = {**entry, 'source': {**entry['source'], 'link': local_link(root, entry)}}
        prop = entry['value']
        typed = type_proposal(prop)
        if entry['stage'] == 'per-sample':
            typed = {**typed, 'clusters': [{**c, 'cluster_id': c['cluster'],
                'coarse_label': c.get('label_coarse', ''), 'fine_label': c.get('label_fine', ''),
                'action': 'keep'} for c in typed.get('clusters', [])]}
        if typed.get('clusters') and all('cluster_id' in c for c in typed['clusters']):
            items += [item for item in _annotation_items(entry['round'], entry['stage'], entry['scope'],
                typed, {}, {}, entry['source'].get('link', entry['source']['path'])) if item.kind != 'removed']
        quality = prop.get('quality', prop if 'inspection' in Path(entry['source']['path']).name else {})
        items += reassign_items(entry, quality)
        items += flag_items(entry, quality)
        for warning in prop.get('host_warnings', []):
            items.append(Item('plan_warning', entry['round'], entry['stage'], entry['scope'],
                note=str(warning), link=entry['source'].get('link', entry['source']['path'])))
        for line in prop.get('lineages', []):
            if not line['zoom']:
                items.append(Item('lineage_skipped', entry['round'], entry['stage'], line['name'],
                    n_cells=line['n_cells'], note=line.get('reason', ''), link=entry['source'].get('link', entry['source']['path'])))
    if ledger is not None:
        items += stress_retained_items(ledger, decisions)
    _mark_recurring(items)
    return items


def publish(unit_ref):
    """Commit once. A retry validates the existing receipt and never reruns science."""
    from .release_state import publication, recover
    from .ledger import sankey_data
    from ..ui.umapdata import write_umap_json
    from ..review import to_json, to_markdown
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
            items = review_items(unit, excluded, decisions, root, ledger)
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


def round_ledger(packet_path):
    """The cell ledger and Sankey of the rounds finished so far, published by the round
    that just closed. Generation 1 wrote one per round; a reader should not have to wait
    for the release to see where the cells went.

    ponytail: re-read of every earlier round's obs each time, the way release does it once.
    A unit is capped at 15 rounds, and the read is seconds against the round's own compute.
    """
    from .ledger import sankey_data

    packet = read(packet_path)
    unit = {k: packet[k] for k in ('unit', 'per_sample', 'rounds')}
    _, ledger, excluded, stages, decisions, _ = collect(unit, complete=False)
    destination = Path.cwd()
    compression = {'method': 'gzip', 'mtime': 0}
    ledger.to_csv(destination / 'cell_ledger.csv.gz', compression=compression)
    excluded.to_csv(destination / 'cell_exclusions.csv.gz', index=False, compression=compression)
    save(destination / 'sankey.json', sankey_data(ledger, stages))
    sealed(destination, destination / 'ledger.json', state='complete', input=packet['input'],
           n_input=len(ledger), n_survived=int((ledger['final_status'] == 'kept').sum()),
           n_removed=int((ledger['final_status'] == 'removed').sum()))
    return destination / 'ledger.json'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', default='release', choices=['release', 'ledger'])
    parser.add_argument('packet', type=Path)
    args = parser.parse_args()
    packet = read(args.packet)
    if args.operation == 'ledger':
        round_ledger(args.packet)
    else:
        save(Path.cwd() / 'released.json', dict(state='complete', input=packet['input'], release=publish(packet['input'])))
