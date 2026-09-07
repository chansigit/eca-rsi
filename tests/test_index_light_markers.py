"""Landing-page state must come from light step markers only: a --mirror copy
carries no h5ad, and the page there has to say the same thing as at the run root."""
import json

from ecarsi import layout as L
from ecarsi.index import _round_step, rounds_state, unit_state


def _touch(d, *names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("x")


def test_round_step_advances_on_light_markers_without_h5ad(tmp_path):
    unit = tmp_path / "unit"
    rdir = L.round_dir(unit, 3)
    cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    cdir.mkdir(parents=True)
    assert _round_step(rdir) == "crosssample · integrate"
    _touch(cdir, "integration_summary.csv")
    assert _round_step(rdir) == "crosssample · inspect"
    _touch(cdir, "inspection_proposal.json")
    assert _round_step(rdir) == "crosssample · annotate"
    _touch(cdir, "annotation_proposal.json", "annotation_removed.csv", "report.html")
    assert _round_step(rdir) == "zoomin · plan"
    zdir.mkdir(parents=True)
    (zdir / "zmip_plan.json").write_text(json.dumps({"lineages": [
        {"name": "A", "zoom": True}, {"name": "B", "zoom": True}, {"name": "C", "zoom": False}]}))
    assert _round_step(rdir) == "zoomin · lineages 0/2"
    _touch(L.lineage_dir(zdir, "A"), "annotation_proposal.json", "report.html")
    assert _round_step(rdir) == "zoomin · lineages 1/2"
    _touch(L.lineage_dir(zdir, "B"), "annotation_proposal.json", "report.html")
    _touch(zdir, "zmip_removed.csv", "report.html")
    assert _round_step(rdir) == "ledger"


def test_round_input_cells_come_from_the_progress_log(tmp_path):
    unit = tmp_path / "unit"
    rdir = L.round_dir(unit, 2)
    L.crosssample_dir(rdir).mkdir(parents=True)
    (unit / L.PROGRESS).write_text(
        "2026-09-06 21:22:40 round 1 stats removed=1/10 (10.00%) decision=continue [x]\n"
        "2026-09-06 21:24:24 round 2 start\n"
        "2026-09-06 21:24:30 round 2 input prepared from round 1 (34684 cells)\n")
    (r,) = rounds_state(unit)
    assert r["step"] == "crosssample · integrate" and r["n_in"] == 34684


def test_persample_success_line_is_not_a_failure(tmp_path):
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / L.PROGRESS).write_text("2026-09-07 12:38:29 persample complete: 2 experiments; 0 failed\n")
    assert unit_state(unit)["stage_class"] != "failed"
    (unit / L.PROGRESS).write_text("2026-09-07 12:38:29 persample failed: 2 experiments; 1 failed\n")
    assert unit_state(unit)["stage_class"] == "failed"
