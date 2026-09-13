"""Manual overrides for the round loop: <unit>/loop_control.json is re-read at
every round boundary (cap / rounds / extra rounds past convergence / pause)."""
import json
from pathlib import Path

import pytest

from ecarsi import layout as L
from ecarsi import loop


def _st(frac, removed=1000, reason=""):
    return {"n_in": 10000, "n_out": 10000 - removed, "removed": removed, "frac": frac, "reason": reason}


def test_decide_extra_rounds_past_convergence_then_release():
    stats = [_st(0.10), _st(0.005, 50)]
    assert loop.decide(2, stats, None, 10) == ("release", "removed 0.50% < 1%")
    d, r = loop.decide(2, stats, None, 10, extra=2)
    assert d == "continue" and r.startswith("converged") and r.endswith("extra round 1/2")
    stats[-1]["reason"] = r
    stats.append(_st(0.004, 40))
    d, r = loop.decide(3, stats, None, 10, extra=2)
    assert d == "continue" and r.endswith("extra round 2/2")
    stats[-1]["reason"] = r
    stats.append(_st(0.003, 30))
    d, r = loop.decide(4, stats, None, 10, extra=2)
    assert d == "release" and "+2 extra round(s) done" in r
    # the cap still wins over extra rounds
    assert loop.decide(4, stats, None, 4, extra=5)[1].startswith("FORCED")


def test_decide_absolute_floor_blocks_both_convergence_paths():
    # E12.5: 0.81% of 245k cells is still 1,989 cells — the "< 1%" path alone would release
    big = {"n_in": 245_499, "n_out": 243_510, "removed": 1_989, "frac": 1_989 / 245_499}
    d, r = loop.decide(3, [_st(0.05), _st(0.03), big], None, 10)
    assert d == "continue" and r == "removed 0.81% but 1,989 cells >= 1,000 floor"
    # the plateau path (E13.5: 1.38, 1.44, 1.10 %) is subject to the same floor
    plateau = [_st(0.10), _st(0.0138, 3000), _st(0.0144, 3100), _st(0.0110, 2476)]
    d, r = loop.decide(4, plateau, None, 10)
    assert d == "continue" and "2,476 cells >= 1,000 floor" in r
    # below the floor both paths release exactly as before
    assert loop.decide(4, plateau[:-1] + [_st(0.0110, 900)], None, 10)[0] == "release"
    assert loop.decide(3, [_st(0.05), _st(0.03), {**big, "removed": 999}], None, 10)[0] == "release"
    # the floor is tunable (loop_control max_removed) and the cap / --rounds still win over it
    assert loop.decide(3, [_st(0.05), _st(0.03), big], None, 10, max_removed=2000)[0] == "release"
    assert loop.decide(3, [_st(0.05), _st(0.03), big], None, 3)[1].startswith("FORCED")
    assert loop.decide(3, [_st(0.05), _st(0.03), big], 3, 10)[1] == "fixed --rounds 3"


def test_read_control_accepts_max_removed(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / L.LOOP_CONTROL).write_text(json.dumps({"max_removed": 2000}))
    assert loop.read_control(unit) == {"max_removed": 2000}
    (unit / L.LOOP_CONTROL).write_text(json.dumps({"max_removed": 0}))
    assert loop.read_control(unit) == {}  # < 1 is rejected like the other limits


def test_read_control_validates_and_never_raises(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    assert loop.read_control(unit) == {}
    (unit / L.LOOP_CONTROL).write_text(json.dumps({"cap": 12, "rounds": None, "extra_rounds_after_convergence": 0}))
    assert loop.read_control(unit) == {"cap": 12, "rounds": None, "extra_rounds_after_convergence": 0}
    for bad in ('{"cap": 0}', '{"cap": true}', '{"nope": 1}', '[1]', 'not json', '{"stop_after_round": "9"}'):
        (unit / L.LOOP_CONTROL).write_text(bad)
        assert loop.read_control(unit) == {}
    assert any("loop_control.json ignored" in ev for _, ev in L.read_log(unit))


@pytest.fixture
def driven(tmp_path, monkeypatch):
    """A unit whose rounds are simulated: every round removes 3 % (never
    converges), so only the limits decide when it stops."""
    unit = tmp_path / "unit"
    L.input_h5ad(unit).parent.mkdir(parents=True)
    L.input_h5ad(unit).write_text("h5")
    L.rounds_root(unit).mkdir(parents=True, exist_ok=True)
    (L.round_dir(unit, 1)).mkdir(parents=True, exist_ok=True)
    (L.round_dir(unit, 1) / L.MANIFEST).write_text(json.dumps({"batch_col": "eca_sample_id", "species": "mm"}))
    ran = []

    def fake_round(_argv):
        rdir = Path(_argv[1])
        L.zoomin_dir(rdir).mkdir(parents=True, exist_ok=True)
        (L.zoomin_dir(rdir) / "annotated_zmip.h5ad").write_text(f"survivors {rdir.name}")
        ran.append(L.round_number(rdir))
        return 0

    cells = {}

    def n_obs(p):
        p = Path(p)
        rdir = next(q for q in p.parents if q.name.startswith("round") and q.name[5:].isdigit())
        n = L.round_number(rdir)
        base = 10000 - (n - 1) * 300  # cells entering round n
        return base - 300 if p.parent.name == L.ZOOMIN else base

    monkeypatch.setattr(loop.crosssample, "main", fake_round)
    monkeypatch.setattr(loop.zoomin, "main", fake_round)
    monkeypatch.setattr(loop, "_run_msp_from_h5ad", lambda *a, **k: 0)
    monkeypatch.setattr(loop, "_prepare_input", lambda src, inp, n: inp.write_text(f"input {n}"))
    monkeypatch.setattr(loop, "_n_obs", n_obs)
    monkeypatch.setattr(loop, "run_ledger", lambda *a, **k: None)
    monkeypatch.setattr(loop, "write_all", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_release", lambda *a, **k: (L.release_dir(unit).mkdir(exist_ok=True),
                                                            (L.release_dir(unit) / "summary.md").write_text("released")))
    monkeypatch.setattr(loop.D, "seal_round", lambda *a, **k: None)
    monkeypatch.setattr(loop.D, "check_round", lambda *a, **k: None)
    monkeypatch.setattr(loop.D, "seal_release", lambda *a, **k: None)
    monkeypatch.setattr(loop.R, "recover", lambda *a, **k: None)
    monkeypatch.setattr(loop.prune, "prune_unit", lambda *a, **k: None)
    monkeypatch.setattr(loop.mirror, "sync", lambda *a, **k: None)
    monkeypatch.setattr("ecarsi.check_agent_config", lambda *a, **k: None)
    monkeypatch.setattr("ecarsi.design.design_text", lambda *a, **k: "")
    monkeypatch.setattr("ecarsi.model", lambda: "test-model")
    return unit, ran


def test_cap_raised_mid_run_extends_the_loop(driven):
    unit, ran = driven
    (unit / L.LOOP_CONTROL).write_text(json.dumps({"cap": 6}))
    assert loop.main([str(unit), "--cap", "4"]) == 0
    assert max(ran) == 6
    reasons = [loop.read_stats(L.round_dir(unit, n) / L.STATS)["reason"] for n in range(1, 7)]
    assert reasons[-1].startswith("FORCED: safety cap 6")
    assert any("loop_control: cap 4 -> 6" in ev for _, ev in L.read_log(unit))


@pytest.mark.parametrize("control,expected", [({"pause": True}, []),
                                              ({"pause_after_stage": "crosssample"}, [1]),
                                              ({"pause_after_stage": "zoomin"}, [1, 1])])
def test_pause_safe_points_never_publish_a_release(driven, control, expected):
    unit, ran = driven
    (unit / L.LOOP_CONTROL).write_text(json.dumps(control))
    assert loop.read_control(unit) == control
    assert loop.main([str(unit), "--cap", "2"]) == 3
    assert ran == expected and not L.release_dir(unit).exists()
    (unit / L.LOOP_CONTROL).unlink()
    assert loop.main([str(unit), "--cap", "2"]) == 0
    assert (L.release_dir(unit) / "summary.md").exists()


def test_pause_after_round_exits_3_without_release_and_resumes(driven):
    unit, ran = driven
    (unit / L.LOOP_CONTROL).write_text(json.dumps({"stop_after_round": 2}))
    assert loop.main([str(unit), "--cap", "5"]) == 3
    assert ran == [1, 1, 2] and not (L.release_dir(unit) / "summary.md").exists()  # round 1 = crosssample + zoomin
    assert any(ev.startswith("paused by loop_control after round 2") for _, ev in L.read_log(unit))
    # a second run with the pause still in place goes straight back to sleep
    assert loop.main([str(unit), "--cap", "5"]) == 3 and max(ran) == 2
    (unit / L.LOOP_CONTROL).unlink()
    assert loop.main([str(unit), "--cap", "5"]) == 0 and max(ran) == 5
    assert (L.release_dir(unit) / "summary.md").exists()
