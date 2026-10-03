"""The cell-count convergence policy (round_policy.decide) and the manual overrides in
<unit>/loop_control.json (round_policy.read_control), re-read at every round boundary."""
import json


from ecarsi import round_policy


def _st(frac, removed=1000, reason=""):
    return {"n_in": 10000, "n_out": 10000 - removed, "removed": removed, "frac": frac, "reason": reason}


def test_decide_extra_rounds_past_convergence_then_release():
    stats = [_st(0.10), _st(0.005, 50)]
    assert round_policy.decide(2, stats, None, 10) == ("release", "removed 0.50% < 1%")
    d, r = round_policy.decide(2, stats, None, 10, extra=2)
    assert d == "continue" and r.startswith("converged") and r.endswith("extra round 1/2")
    stats[-1]["reason"] = r
    stats.append(_st(0.004, 40))
    d, r = round_policy.decide(3, stats, None, 10, extra=2)
    assert d == "continue" and r.endswith("extra round 2/2")
    stats[-1]["reason"] = r
    stats.append(_st(0.003, 30))
    d, r = round_policy.decide(4, stats, None, 10, extra=2)
    assert d == "release" and "+2 extra round(s) done" in r
    # the cap still wins over extra rounds
    assert round_policy.decide(4, stats, None, 4, extra=5)[1].startswith("FORCED")


def test_decide_absolute_floor_blocks_both_convergence_paths():
    # E12.5: 0.81% of 245k cells is still 1,989 cells — the "< 1%" path alone would release
    big = {"n_in": 245_499, "n_out": 243_510, "removed": 1_989, "frac": 1_989 / 245_499}
    d, r = round_policy.decide(3, [_st(0.05), _st(0.03), big], None, 10)
    assert d == "continue" and r == "removed 0.81% but 1,989 cells >= 1,000 floor"
    # the plateau path (E13.5: 1.38, 1.44, 1.10 %) is subject to the same floor
    plateau = [_st(0.10), _st(0.0138, 3000), _st(0.0144, 3100), _st(0.0110, 2476)]
    d, r = round_policy.decide(4, plateau, None, 10)
    assert d == "continue" and "2,476 cells >= 1,000 floor" in r
    # below the floor both paths release exactly as before
    assert round_policy.decide(4, plateau[:-1] + [_st(0.0110, 900)], None, 10)[0] == "release"
    assert round_policy.decide(3, [_st(0.05), _st(0.03), {**big, "removed": 999}], None, 10)[0] == "release"
    # the floor is tunable (loop_control max_removed) and the cap / --rounds still win over it
    assert round_policy.decide(3, [_st(0.05), _st(0.03), big], None, 10, max_removed=2000)[0] == "release"
    assert round_policy.decide(3, [_st(0.05), _st(0.03), big], None, 3)[1].startswith("FORCED")
    assert round_policy.decide(3, [_st(0.05), _st(0.03), big], 3, 10)[1] == "fixed --rounds 3"


def test_read_control_accepts_max_removed(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / round_policy.CONTROL_FILE).write_text(json.dumps({"max_removed": 2000}))
    assert round_policy.read_control(unit) == {"max_removed": 2000}
    (unit / round_policy.CONTROL_FILE).write_text(json.dumps({"max_removed": 0}))
    assert round_policy.read_control(unit) == {}  # < 1 is rejected like the other limits


def test_read_control_validates_and_never_raises(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    assert round_policy.read_control(unit) == {}
    (unit / round_policy.CONTROL_FILE).write_text(json.dumps({"cap": 12, "rounds": None, "extra_rounds_after_convergence": 0}))
    assert round_policy.read_control(unit) == {"cap": 12, "rounds": None, "extra_rounds_after_convergence": 0}
    for bad in ('{"cap": 0}', '{"cap": true}', '{"nope": 1}', '[1]', 'not json', '{"stop_after_round": "9"}'):
        (unit / round_policy.CONTROL_FILE).write_text(bad)
        assert round_policy.read_control(unit) == {}
