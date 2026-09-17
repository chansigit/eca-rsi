"""Operator stage-concurrency floors come from the environment, apply to every new stage, and never break a resume."""
import json

import pytest

from ecarsi.control.dataset import same_stage_spec, stage_limit_floors, with_limit_floors


def test_floors_default_to_nothing_and_come_from_the_environment(monkeypatch):
    monkeypatch.delenv('ECA_RSI_STAGE_LIMIT_FLOORS', raising=False)
    assert stage_limit_floors() == {}
    zoom = dict(max_in_flight_deg=6, max_in_flight_lineages=2, config={'x': 1})
    assert with_limit_floors(zoom) == zoom
    monkeypatch.setenv('ECA_RSI_STAGE_LIMIT_FLOORS', json.dumps({'max_in_flight_deg': 12, 'max_in_flight_lineages': 6}))
    assert with_limit_floors(zoom) == dict(max_in_flight_deg=12, max_in_flight_lineages=6, config={'x': 1})
    assert with_limit_floors(dict(max_in_flight_deg=32))['max_in_flight_deg'] == 32  # never lowered
    assert with_limit_floors(dict(max_in_flight_samples=4)) == dict(max_in_flight_samples=4)  # other stages untouched
    monkeypatch.setenv('ECA_RSI_STAGE_LIMIT_FLOORS', '{"max_in_flight_deg": 0}')
    with pytest.raises(ValueError):
        stage_limit_floors()


def test_resume_comparison_ignores_the_floored_keys_only():
    floors = {'max_in_flight_deg': 12, 'max_in_flight_lineages': 6}
    saved = dict(input='a', max_in_flight_deg=6, max_in_flight_lineages=2)
    assert same_stage_spec(saved, dict(input='a', max_in_flight_deg=12, max_in_flight_lineages=6), floors)
    assert not same_stage_spec(saved, dict(input='b', max_in_flight_deg=12, max_in_flight_lineages=6), floors)
    assert not same_stage_spec(saved, dict(input='a', max_in_flight_deg=12, max_in_flight_lineages=6), {})  # no floors: exact
    assert not same_stage_spec(None, dict(input='a'), floors)


def test_completed_stage_check_tolerates_floored_keys(tmp_path, monkeypatch):
    # Resume re-validates every completed stage; with floors set, a spec saved before the floors
    # differs only in the floored key and must still be accepted (Eye, 2026-09-17 00:00 PDT).
    from ecarsi.control.dataset import dataset_step
    from ecarsi.warm_pool.state import save
    monkeypatch.setenv('ECA_RSI_STAGE_LIMIT_FLOORS', json.dumps({'max_in_flight_deg': 12}))
    root = tmp_path / 'stage'
    root.mkdir()
    saved = dict(output_root=str(root), pool_root=str(tmp_path), input='in', max_in_flight_deg=6)
    save(root / 'spec.json', saved)
    save(root / 'publication.json', dict(state='complete', input='other'))
    with pytest.raises(ValueError, match='Cached stage input changed'):
        dataset_step('completed', ['cross_sample', dict(saved, max_in_flight_deg=12)])
    with pytest.raises(ValueError, match='Saved stage specification changed'):
        dataset_step('completed', ['cross_sample', dict(saved, input='changed')])


def test_resumed_stage_keeps_the_saved_spec(tmp_path, monkeypatch):
    from ecarsi.control.dataset import stage_spec_on_disk
    from ecarsi.warm_pool.state import save
    monkeypatch.setenv('ECA_RSI_STAGE_LIMIT_FLOORS', json.dumps({'max_in_flight_deg': 12}))
    root = tmp_path / 'stage'
    fresh = dict(input='a', max_in_flight_deg=12)
    assert stage_spec_on_disk(root, fresh, True) == fresh  # nothing saved yet
    root.mkdir()
    save(root / 'spec.json', dict(input='a', max_in_flight_deg=6))
    assert stage_spec_on_disk(root, fresh, True) == dict(input='a', max_in_flight_deg=6)
    assert stage_spec_on_disk(root, fresh, False) == fresh
    with pytest.raises(ValueError, match='Saved stage specification changed'):
        stage_spec_on_disk(root, dict(input='b', max_in_flight_deg=12), True)
