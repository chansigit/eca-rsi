"""Stage concurrency floors apply to every new stage and never break a resume of an older one."""
from ecarsi.dataset_workflow import STAGE_LIMIT_FLOORS, same_stage_spec, with_limit_floors


def test_floors_raise_but_never_lower_and_leave_other_stages_alone():
    zoom = dict(max_in_flight_deg=6, max_in_flight_lineages=2, config={'x': 1})
    assert with_limit_floors(zoom) == dict(max_in_flight_deg=12, max_in_flight_lineages=6, config={'x': 1})
    assert with_limit_floors(dict(max_in_flight_deg=32))['max_in_flight_deg'] == 32
    assert with_limit_floors(dict(max_in_flight_samples=4)) == dict(max_in_flight_samples=4)
    assert all(isinstance(v, int) and v >= 1 for v in STAGE_LIMIT_FLOORS.values())


def test_resume_comparison_ignores_the_floored_keys_only():
    saved = dict(input='a', max_in_flight_deg=6, max_in_flight_lineages=2)
    assert same_stage_spec(saved, dict(input='a', max_in_flight_deg=12, max_in_flight_lineages=6))
    assert not same_stage_spec(saved, dict(input='b', max_in_flight_deg=12, max_in_flight_lineages=6))
    assert not same_stage_spec(None, dict(input='a'))
