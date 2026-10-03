"""Degraded results (ecarsi.degraded, decision 0013): kept with the run, listed in needs_review, shown by
Periscope; strict mode turns them back into failures."""
import json

import pandas as pd
import pytest

from ecarsi import degraded
from ecarsi.ui import index


def test_strict_mode_reraises_and_lenient_mode_keeps_the_record(tmp_path, monkeypatch):
    with pytest.raises(OSError):
        try:
            raise OSError("disk gone")
        except OSError as exc:
            degraded.note("report of lineage T cells", exc)
    monkeypatch.delenv(degraded.STRICT)
    try:
        raise OSError("disk gone")
    except OSError as exc:
        record = degraded.note("report of lineage T cells", exc)
    run = tmp_path / "run"
    stage = run / "units" / "lung" / "rounds" / "round02" / "03-zoom-in"
    stage.mkdir(parents=True)
    degraded.save(stage, [record], stage="round02/zoom-in", scope="T cells")
    degraded.save(stage, [record], stage="round02/zoom-in", scope="T cells")  # a retried copy does not double it
    degraded.save(run, [{**record, "id": "x"}], stage="display sync after release")
    records = degraded.read(run)
    assert [(r.get("unit"), r["stage"]) for r in records] == [("lung", "round02/zoom-in"), (None, "display sync after release")]
    assert records[0]["error"] == "OSError: disk gone" and records[0]["scope"] == "T cells"


def test_release_and_periscope_show_the_degradations(tmp_path, monkeypatch):
    from ecarsi.stages.release import review_items
    monkeypatch.delenv(degraded.STRICT)
    run = tmp_path / "run"
    unit = run / "units" / "lung"
    unit.mkdir(parents=True)
    (run / "units" / "heart").mkdir()
    record = dict(what="report of lineage B cells", error="TypeError: x", at=1, id="a")
    degraded.save(unit / "rounds" / "round03" / "03-zoom-in", [record], stage="round03/zoom-in", scope="B cells")
    degraded.save(run / "units" / "heart", [{**record, "id": "b"}], stage="per-sample")
    unit_pub = dict(rounds=[], forced_release=False, per_sample=None)
    monkeypatch.setattr("ecarsi.stages.release.verified", lambda ref: {"skipped_samples": []})
    items = review_items(unit_pub, pd.DataFrame(columns=["round", "release_stage"]), [], unit)
    assert [(i.kind, i.round, i.step, i.scope) for i in items] == [("degraded", 3, "round03/zoom-in", "B cells")]
    state = index.dataset_state(run, states=[])
    assert len(state["degraded"]) == 2
    assert json.loads((run / "degraded" / "1-a.json").read_text())["unit"] == "lung"
