"""ops/gate.py: the release gate names what a finished run is missing."""
import importlib.util
import json
from pathlib import Path


def load_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("BASE", str(tmp_path / "control"))
    monkeypatch.setenv("CONTROL", str(tmp_path / "control" / "durable-control"))
    spec = importlib.util.spec_from_file_location("gate", Path(__file__).resolve().parents[1] / "ops" / "gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def test_the_gate_names_every_missing_piece(monkeypatch, tmp_path):
    gate = load_gate(monkeypatch, tmp_path)
    run = tmp_path / "run"
    lineage = run / "units/lung/rounds/round01/03-zoom-in/T cells"
    lineage.mkdir(parents=True)
    (lineage / "annotation_proposal.json").write_text("{}")
    (run / "publication.json").write_text(json.dumps(dict(state="complete", dataset_id="d", units=[], failed_units=[],
        forced_release=False, n_input=2, n_survived=1, n_removed=1)))
    (run / "spec.json").write_text("{}")
    (run / "units/lung/release").mkdir()
    (run / "degraded").mkdir()
    (run / "degraded/1-a.json").write_text(json.dumps(dict(what="display sync after release", error="OSError: x", at=1, id="a")))
    found = gate.problems(run)
    assert any("lung: not released" in p for p in found)
    assert any("T cells: no report.html" in p for p in found)
    assert any("degraded: dataset" in p for p in found)
    (lineage / "report.html").write_text("<html>")
    (run / "units/lung/release/receipt.json").write_text(json.dumps(dict(state="complete")))
    (run / "units/lung/release/needs_review.json").write_text("[]")
    (run / "degraded/1-a.json").unlink()
    assert gate.problems(run) == []
