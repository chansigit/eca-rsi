"""Read-only end-to-end acceptance: python tests/check_dataset_publication.py publication.json."""
import argparse
import json
from pathlib import Path

import anndata as an
import pandas as pd

from ecarsi.agent_session import reference, verified
from ecarsi.crosssample_v2 import artifact
from ecarsi.persample_v2 import check_bundle
from ecarsi.run_state import file_identity


def obs(path):
    data = an.read_h5ad(path, backed='r')
    try:
        assert data.obs_names.is_unique
        return data.obs.copy()
    finally:
        data.file.close()


def frame(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def check(path):
    dataset = verified(reference(path))
    assert dataset['state'] == 'complete' and not dataset['failed_units']
    result = dict(dataset_id=dataset['dataset_id'], publication=reference(path), units=[])
    for unit_ref in dataset['units']:
        unit = verified(unit_ref)
        assert unit['state'] == 'complete'
        input_path = Path(unit['unit']['path']) / 'input/organized.h5ad'
        assert file_identity(input_path) == verified(unit['unit']['manifest'])['identity']
        original = obs(input_path)
        per_sample = verified(unit['per_sample'])
        assert per_sample['state'] == 'complete' and not per_sample['failed_samples']
        omitted = frame(per_sample['partition_exclusions']['path'])
        assert reference(per_sample['partition_exclusions']['path']) == per_sample['partition_exclusions']
        excluded = set(omitted.cell_id)
        assert len(excluded) == len(omitted)
        if len(omitted):
            assert omitted.excluded_reason.str.strip().ne('').all()
        surviving = set()
        for sample_ref in per_sample['samples']:
            sample = check_bundle(sample_ref)
            kept = set() if sample['empty'] else set(obs(artifact(sample, 'clustered.h5ad')).index)
            ledger = frame(artifact(sample, 'cell_exclusions.csv.gz'))
            inputs = frame(artifact(sample, 'input_cells.csv.gz'))
            gone = set(ledger.cell_uid)
            assert len(gone) == len(ledger) and ledger.reason.str.strip().ne('').all()
            assert kept.isdisjoint(gone) and kept | gone == set(inputs.cell_id)
            assert (surviving | excluded).isdisjoint(kept | gone)
            for source, column in (('source_id', 'source_unit'), ('source_cell_id', 'eca_source_cell_id')):
                assert ledger[source].tolist() == original.loc[ledger.cell_uid, column].astype(str).tolist()
            surviving |= kept
            excluded |= gone
        assert surviving.isdisjoint(excluded) and surviving | excluded == set(original.index)
        assert per_sample['n_input'] == len(original)
        assert per_sample['n_survived'] == len(surviving) and per_sample['n_removed'] == len(excluded)
        rounds = []
        previous_zoom = None
        for round_ref in unit['rounds']:
            record = verified(round_ref)
            row = dict(round=record['round'], stats=record['stats'], stages=[])
            n_before = len(surviving)
            for stage, name in (('cross_sample', 'annotated.h5ad'), ('zoom_in', 'annotated_zmip.h5ad')):
                bundle = check_bundle(record[stage])
                current = obs(artifact(bundle, name))
                ledger = frame(artifact(bundle, 'cell_exclusions.csv.gz'))
                gone, kept = set(ledger.cell_uid), set(current.index)
                assert len(gone) == len(ledger) and kept.isdisjoint(gone)
                assert kept | gone == surviving and gone.isdisjoint(excluded)
                assert bundle['n_input'] == len(surviving) == len(kept) + len(gone)
                assert bundle['n_removed'] == len(gone) and bundle['n_survived'] == len(kept)
                assert all(isinstance(json.loads(value), list) and json.loads(value) for value in ledger.reason)
                for source, column in (('source_id', 'source_unit'), ('source_cell_id', 'eca_source_cell_id')):
                    assert ledger[source].tolist() == original.loc[ledger.cell_uid, column].astype(str).tolist()
                    assert current[column].astype(str).tolist() == original.loc[current.index, column].astype(str).tolist()
                if stage == 'cross_sample' and previous_zoom is not None:
                    for column in ('msp_ann_coarse', 'zmip_ann_fine'):
                        archived = f"r{record['round']-1:02d}_" + column
                        assert current[archived].astype(str).tolist() == previous_zoom.loc[current.index, column].astype(str).tolist()
                if stage == 'zoom_in':
                    plan = verified(bundle['decision'])['proposal']
                    skipped = [label for line in plan['lineages'] if not line['zoom'] for label in line['coarse_labels']]
                    retained = cross_obs.index[cross_obs.msp_ann_coarse.astype(str).isin(skipped)]
                    assert set(retained) <= kept
                    assert current.loc[retained, 'zmip_ann_fine'].astype(str).tolist() == cross_obs.loc[retained, 'msp_ann_fine'].astype(str).tolist()
                    for column in ('zmip_ann_coarse', 'zmip_ann_fine'):
                        assert current[column].notna().all() and current[column].astype(str).str.strip().ne('').all()
                    for lineage_ref in bundle['lineages']:
                        lineage = check_bundle(lineage_ref)
                        decision = verified(lineage['decision'])
                        assert decision['types']['cluster_key'] == 'msp_leiden_r1.0'
                        assert decision['quality']['cluster_key'] == 'msp_leiden_r2.0'
                        moved = frame(artifact(lineage, 'annotation_reassigned.csv'))
                        assert set(moved.cell) <= kept and set(moved.cell).isdisjoint(gone)
                    previous_zoom = current
                else:
                    cross_obs = current
                excluded |= gone
                surviving = kept
                row['stages'].append(dict(stage=stage, n_input=bundle['n_input'], n_survived=len(kept), n_removed=len(gone)))
            assert record['stats']['n_in'] == n_before and record['stats']['n_out'] == len(surviving)
            rounds.append(row)
        assert unit['n_input'] == len(original) == len(surviving) + len(excluded)
        assert unit['n_survived'] == len(surviving) and unit['n_removed'] == len(excluded)
        assert unit['final'] == verified(unit['rounds'][-1])['zoom_in']
        release = Path(unit_ref['path']).parent / 'release'
        if release.exists():
            receipt = verified(reference(release / 'receipt.json'))
            assert receipt['state'] == 'complete' and receipt['input'] == unit_ref
            for name, checksum in receipt['files'].items():
                assert reference(release / name)['sha256'] == checksum
            assert reference(release / 'final.h5ad')['sha256'] == verified(unit['final'])['files']['annotated_zmip.h5ad']['sha256']
            ledger = frame(release / 'cell_ledger.csv.gz')
            gone = frame(release / 'cell_exclusions.csv.gz')
            assert len(ledger) == len(original) and set(ledger.cell_uid) == set(original.index)
            assert set(ledger.loc[ledger.final_status == 'kept', 'cell_uid']) == surviving
            assert len(gone) == len(excluded) and set(gone.cell_uid) == excluded
            assert gone.reason.str.strip().ne('').all()
            for source, column in (('source_id', 'source_unit'), ('source_cell_id', 'eca_source_cell_id')):
                assert ledger[source].tolist() == original.loc[ledger.cell_uid, column].astype(str).tolist()
        result['units'].append(dict(name=unit['unit']['name'], n_input=len(original),
            n_survived=len(surviving), n_removed=len(excluded), rounds=rounds))
    for key in ('n_input', 'n_survived', 'n_removed'):
        result[key] = sum(u[key] for u in result['units'])
        assert result[key] == dataset[key]
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('publication', type=Path)
    print(json.dumps(check(parser.parse_args().publication), indent=2))
