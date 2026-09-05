"""Postflight mutations must not be sealed as a completed downstream run."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from ecarsi import downstream as D
from ecarsi import layout as L
from ecarsi.run_state import file_identity, read_json, write_json


@pytest.fixture
def stable(monkeypatch):
    runtime = {"source": "before"}
    monkeypatch.setattr(D, "kernel_runtime", lambda *a: copy.deepcopy(runtime))
    monkeypatch.setattr(D, "options", lambda kernel: [])
    monkeypatch.delenv("MSP_BATCH_COL", raising=False)
    return runtime


@pytest.mark.parametrize("mutation", ["input", "runtime"])
def test_postflight_rejects_mutation_during_validation_without_sealing(tmp_path, monkeypatch, stable, mutation):
    source = tmp_path / "source.h5ad"
    source.write_bytes(b"original input")
    out = tmp_path / "msp"
    identity = D.prepare(sys.executable, "msp", [source], out, {"batch_col": "sample", "options": []})
    before = (out / D.STATE).read_bytes()

    def validating_child(*args, **kwargs):
        # The child's validation can succeed for the files it just saw while
        # the parent-visible input/runtime changes before the receipt is sealed.
        if mutation == "input":
            source.write_bytes(b"input modified during child validation")
        else:
            stable["source"] = "updated implementation"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"outputs": {"report.html": {"size": 1}}}), stderr="")

    monkeypatch.setattr(D.subprocess, "run", validating_child)
    with pytest.raises(ValueError, match=f"{mutation} changed during computation"):
        D.verify(sys.executable, "msp", [source], out, identity)
    assert (out / D.STATE).read_bytes() == before
    assert read_json(out / D.STATE)["state"] == "running"


def test_postflight_unchanged_success_seals_the_validated_result(tmp_path, monkeypatch, stable):
    source = tmp_path / "source.h5ad"
    source.write_bytes(b"stable input")
    out = tmp_path / "msp"
    identity = D.prepare(sys.executable, "msp", [source], out, {"batch_col": "sample", "options": []})
    validation = {"n_input": 2, "n_output": 1, "n_removed": 1, "outputs": {"report.html": {"size": 1}}}
    monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(validation), stderr=""))
    assert D.verify(sys.executable, "msp", [source], out, identity) == validation
    result = read_json(out / D.STATE)
    assert result["state"] == "complete"
    assert result["identity"] == identity and result["validation"] == validation


@pytest.fixture
def completed_round(tmp_path, stable):
    rdir = L.round_dir(tmp_path, 1)
    source = tmp_path / "source.h5ad"
    source.write_bytes(b"stable input")
    for kernel, stage in [("msp", L.crosssample_dir(rdir)), ("zmip", L.zoomin_dir(rdir))]:
        identity = D.prepare(sys.executable, kernel, [source], stage, {"batch_col": "sample", "options": []})
        output = stage / "report.html"
        output.write_text("<html>validated result</html>")
        record = read_json(stage / D.STATE)
        write_json(stage / D.STATE, {**record, "identity": identity, "state": "complete", "validation": {
            "outputs": {"report.html": file_identity(output)}}})
    (rdir / L.STATS).write_text('{"n_in": 2, "n_out": 1}')
    (rdir / L.DECISION).write_text("release")
    D.seal_round(rdir)
    D.check_round(rdir)
    return rdir


def test_completed_round_rejects_explicit_changed_batch_column(completed_round, monkeypatch):
    monkeypatch.setenv("MSP_BATCH_COL", "condition")
    with pytest.raises(ValueError, match="MSP_BATCH_COL"):
        D.check_round(completed_round)


@pytest.mark.parametrize("requested", [None, "sample"])
def test_completed_round_accepts_unchanged_or_inherited_batch_column(completed_round, monkeypatch, requested):
    if requested is not None:
        monkeypatch.setenv("MSP_BATCH_COL", requested)
    D.check_round(completed_round)
