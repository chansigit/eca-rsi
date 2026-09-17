"""Operator stage-concurrency floors come from the environment, apply to every new stage, and never break a resume."""
import json

import pytest

from ecarsi.dataset_workflow import same_stage_spec, stage_limit_floors, with_limit_floors


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
