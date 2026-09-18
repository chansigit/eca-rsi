import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi.agent.session import reference, verified
from ecarsi.stages.release import collect, publish
from ecarsi.run_state import file_identity
from ecarsi.warm_pool.state import save


def test_release_conservation_retry_and_corruption(tmp_path):
    def record(name, value):
        path = tmp_path / name
        save(path, value)
        return reference(path)

    def matrix(name, cells):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        obs = pd.DataFrame(index=cells)
        obs['source_unit'] = 'source'
        obs['eca_source_cell_id'] = cells
        for prefix in ('', 'msp', 'zmip'):
            obs[prefix + '_ann_coarse'] = 'T cell'
            obs[prefix + '_ann_fine'] = 'CD4 T cell'
        data = ad.AnnData(np.ones((len(cells), 2)), obs=obs)
        data.obsm['X_umap'] = np.zeros((len(cells), 2))
        data.write_h5ad(path)
        return reference(path)

    def csv(name, data):
        path = tmp_path / name
        pd.DataFrame(data).to_csv(path, index=False)
        return reference(path)

    def gone(name, cells, reason):
        return csv(name, dict(cell_uid=cells, source_id=['source'] * len(cells), source_cell_id=cells,
                              reason=[reason] * len(cells)))

    original = matrix('organized/input/organized.h5ad', ['001', 'NA', 'c', 'd', 'e'])
    manifest = record('manifest.json', {'identity': file_identity(Path(original['path']))})
    sample = record('sample.json', dict(sample='sample', empty=False, files={
        'clustered.h5ad': matrix('sample.h5ad', ['001', 'NA', 'c']),
        'input_cells.csv.gz': csv('input.csv', dict(cell_id=['001', 'NA', 'c', 'd'])),
        'cell_exclusions.csv.gz': gone('sample-gone.csv', ['d'], 'high_mito')}))
    per = record('per.json', dict(state='complete', failed_samples=[], samples=[sample],
        partition_exclusions=csv('partition.csv', dict(cell_id=['e'], excluded_reason=['confirmed policy'])),
        n_input=5, n_survived=3, n_removed=2))
    cross = record('cross.json', dict(state='complete', input=per, n_input=3, n_survived=2, n_removed=1,
        files={'annotated.h5ad': matrix('cross.h5ad', ['001', 'NA']),
               'cell_exclusions.csv.gz': gone('cross-gone.csv', ['c'], '[{"code":"fragment_qc"}]')}))
    zoom = record('zoom.json', dict(state='complete', input=cross, n_input=2, n_survived=1, n_removed=1,
        files={'annotated_zmip.h5ad': matrix('zoom.h5ad', ['001']),
               'cell_exclusions.csv.gz': gone('zoom-gone.csv', ['NA'], json.dumps([
                   {'code': 'low-quality', 'decision': {'confidence': 'medium'}}]))}))
    round_ref = record('round.json', dict(round=1, cross_sample=cross, zoom_in=zoom,
        stats=dict(n_in=3, n_out=1, removed=2, frac=2/3, reason='fixed rounds')))
    unit = record('publication.json', dict(state='complete', unit=dict(name='test',
        path=str(tmp_path / 'organized'), manifest=manifest), per_sample=per, rounds=[round_ref],
        final=zoom, n_input=5, n_survived=1, n_removed=4, forced_release=False, reason='fixed rounds'))
    _, ledger, exclusions, _, _, _ = collect(unit)
    assert ledger.loc['001', 'final_status'] == 'kept'
    assert ledger.loc['001', 'per-sample_coarse'] == 'T cell'
    assert set(exclusions.cell_uid) == {'NA', 'c', 'd', 'e'}
    assert exclusions.loc[exclusions.cell_uid == 'e', 'reason'].item() == 'confirmed policy'
    receipt = publish(unit)
    assert publish(unit) == receipt
    released = verified(receipt)
    assert released['state'] == 'complete' and released['input'] == unit
    assert (tmp_path / 'release/final.h5ad').stat().st_ino != (tmp_path / 'zoom.h5ad').stat().st_ino
    review = json.loads((tmp_path / 'release/needs_review.json').read_text())
    assert [item['n_cells'] for item in review if item['kind'] == 'removed'] == [1]
    (tmp_path / 'release/summary.json').write_text('{}')
    with pytest.raises(ValueError, match='Released artifact changed'):
        publish(unit)
    # A count-preserving ledger swap must still fail the exact cell partition check.
    gone('cross-gone.csv', ['d'], '[{"code":"fragment_qc"}]')
    cross_value = verified(cross)
    cross_value['files']['cell_exclusions.csv.gz'] = reference(tmp_path / 'cross-gone.csv')
    cross = record('cross.json', cross_value)
    round_ref = record('round.json', dict(round=1, cross_sample=cross, zoom_in=zoom))
    value = verified(unit)
    value['rounds'] = [round_ref]
    unit = record('publication.json', value)
    with pytest.raises(ValueError, match='conservation failed'):
        collect(unit)
