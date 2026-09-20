"""Periscope reads both generations: gen-2 state comes from publications, not progress.log."""
import json
import os
from pathlib import Path

import pytest

from ecarsi import index, layout as L, serve


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def gen2_run(root: Path, *, released=True, failed=False) -> Path:
    save(root / 'spec.json', {'run_id': 'r', 'dataset_id': 'D'})
    save(root / '00-organize' / 'publication.json', {'state': 'complete', 'units': [{'name': 'u'}]})
    save(root / '00-organize' / 'organize' / 'manifest.json', {'species': 'human', 'n_cells': 100, 'warnings': []})
    save(root / '00-organize' / 'units' / 'u' / 'input' / 'manifest.json', {'species': 'human', 'n_cells': 100})
    unit = root / 'units' / 'u'
    save(unit / '01-per-sample' / 'publication.json',
         {'state': 'complete', 'samples': [{'path': 'a'}, {'path': 'b'}], 'failed_samples': [],
          'skipped_samples': [{'sample': 'b', 'n_cells': 4, 'error': 'session died twice'}],
          'n_input': 100, 'n_survived': 90, 'n_removed': 10})
    save(unit / 'rounds' / 'round01' / 'publication.json',
         {'round': 1, 'stats': {'n_in': 90, 'n_out': 70, 'removed': 20, 'frac': 0.22,
                                'decision': 'continue', 'reason': 'round 1 never releases'}})
    if released:
        save(unit / 'publication.json', {'state': 'complete', 'n_input': 100, 'n_survived': 70, 'n_removed': 30,
                                         'forced_release': False, 'reason': 'removed 0.5% < 1%', 'rounds': [{}],
                                         'unit': {'name': 'u'}})
        save(unit / 'release' / 'receipt.json', {'state': 'complete'})
        save(unit / 'release' / 'needs_review.json', [])
        (unit / 'release' / 'needs_review.md').write_text('# review\n')
        save(root / 'publication.json', {'state': 'complete', 'dataset_id': 'D', 'units': [], 'failed_units': [],
                                         'n_input': 100, 'n_survived': 70, 'n_removed': 30, 'forced_release': False})
    else:
        save(unit / 'rounds' / 'round02' / '02-cross-sample' / 'publication.json', {'state': 'complete', 'n_input': 70})
        save(root / 'publication.json',
             {'state': 'incomplete', 'dataset_id': 'D', 'units': [],
              'failed_units': [{'unit': 'u', 'error': 'Child Workflow execution failed'}] if failed else [],
              'n_input': 100, 'n_survived': 0, 'n_removed': 0, 'forced_release': False})
    return root


def test_layout_recognises_a_gen2_run_without_mistaking_a_gen1_one(tmp_path):
    root = gen2_run(tmp_path / 'g2')
    assert L.is_gen2_root(root) and L.is_root(root)
    assert L.units(root) == [root / 'units' / 'u'] and L.is_gen2_unit(root / 'units' / 'u')
    assert not L.is_unit(root / 'units' / 'u')      # gen-1 markers (input/manifest.json) are absent
    gen1 = tmp_path / 'g1' / 'units' / 'u'
    save(gen1 / 'input' / 'manifest.json', {'species': 'mouse', 'n_cells': 5})
    assert L.is_unit(gen1) and not L.is_gen2_unit(gen1) and not L.is_gen2_root(tmp_path / 'g1')
    assert serve._check_dataset(root) == root and serve._check_dataset(root / 'units' / 'u')


def test_released_gen2_state_and_page_come_from_the_publications(tmp_path):
    root = gen2_run(tmp_path / 'g2')
    state = index.dataset_state(root)
    assert (state['stage'], state['cls'], state['units'], state['released']) == ('released', 'released', 1, 1)
    assert (state['n_input'], state['final_cells'], state['rounds'], state['species']) == (100, 70, 1, 'human')
    assert state['events']['organize'] and state['events']['release']       # the fleet curve has both ends
    page = index.render_root(root, 'D')
    assert 'round 1 never releases' in page and 'session died twice' in page  # decision reason and the skipped sample
    assert 'release/needs_review.md' in page and '00-organize/organize/manifest.json' in page
    assert index.render_unit(root / 'units' / 'u', 'D')


@pytest.mark.parametrize('failed,expected', [(False, 'running'), (True, 'failed')])
def test_unfinished_gen2_unit_reports_its_round_or_the_datasets_failure(tmp_path, failed, expected):
    root = gen2_run(tmp_path / 'g2', released=False, failed=failed)
    state = index.dataset_state(root)
    assert state['cls'] == expected and state['final_cells'] == 70   # survivors of the last finished round
    unit = index.unit_state(root / 'units' / 'u')   # the aggregate says "failed"; the reason is the unit's
    assert ('Child Workflow execution failed' in unit['stage']) == failed
    if not failed:
        # round 2 has an accepted cross-sample publication, so zoom-in is what is running
        assert state['stage'] == 'round 2 · zoom-in'
        (root / 'units/u/rounds/round03/02-cross-sample').mkdir(parents=True)
        assert index.dataset_state(root)['stage'] == 'round 3 · cross-sample'
    assert index.render_root(root, 'D')


def test_a_unit_still_in_per_sample_is_listed_before_anything_publishes(tmp_path):
    """The control plane creates the stage directory long before it publishes; the page
    renders mid-run the way generation 1 does, instead of claiming nothing was planned."""
    root = tmp_path / 'run'
    save(root / 'spec.json', {'run_id': 'r', 'dataset_id': 'D'})
    save(root / '00-organize' / 'publication.json', {'state': 'complete', 'units': [{'name': 'u'}]})
    save(root / '00-organize' / 'organize' / 'manifest.json', {'species': 'human', 'n_cells': 100, 'warnings': []})
    save(root / '00-organize' / 'units' / 'u' / 'input' / 'manifest.json', {'species': 'human', 'n_cells': 100})
    unit = root / 'units' / 'u'
    (unit / '01-per-sample' / 'agent-abc').mkdir(parents=True)

    assert L.is_gen2_unit(unit) and [u.name for u in L.units(root)] == ['u']
    state = index.unit_state(unit)
    assert state['generation'] == 2 and state['stage'] == 'per-sample running'
    assert state['n_input'] == 100
    assert index.dataset_state(root)['units'] == 1
    assert 'No analysis unit has been planned' not in index.render_root(root)


def test_a_run_whose_files_stopped_moving_is_reported_stopped_not_running(tmp_path, monkeypatch):
    """Nothing on disk records that a driver was killed or a workflow terminated, so a
    'running' state that has not moved for half a day is reported as stopped."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    fresh = index.dataset_state(root)
    assert fresh['cls'] == 'running' and not fresh['stage'].startswith('stopped')

    old = fresh['updated'] - index.STALE_AFTER - 60
    for path in root.rglob('*'):
        if path.is_file():
            os.utime(path, (old, old))
    stale = index.dataset_state(root)
    assert stale['cls'] == 'failed' and stale['stage'] == 'stopped · ' + fresh['stage']
