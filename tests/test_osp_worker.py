"""Exercise the actual worker entry point with deterministic kernel submissions."""
from __future__ import annotations

import pandas as pd
import pytest

from ecarsi import layout as L, osp_worker, persample
from ecarsi.osp_contract import INPUT_CELLS, is_done
from ecarsi.run_state import file_identity, read_json, write_json
from tests.test_front_integration import matrix, publish


def request(tmp_path, annotate=True):
    a = matrix(7)
    a.obs_names = [f"cell{i}" for i in range(6)] + ["removed"]
    a.obs["eca_sample_id"] = "A"
    a.write_h5ad(tmp_path / "subset.h5ad")
    pd.DataFrame({"cell_id": a.obs_names}).to_csv(tmp_path / INPUT_CELLS, index=False)
    req = {"identity": "test", "value": "A", "n_cells": 7, "runtime": {},
           "subset_identity": file_identity(tmp_path / "subset.h5ad"),
           "config": {"annotate": annotate, "scrublet": False, "decontx": False,
                      "resolution": 0.7, "species": "mouse", "tissue": "spleen",
                      "language": "Chinese", "model": "test", "effort": "high"}}
    path = tmp_path / "request.json"
    write_json(path, req)
    return path


def test_worker_options_and_annotation_only_resume(tmp_path, monkeypatch):
    import osp
    from osp import annotate
    path = request(tmp_path)
    calls = []

    def compute(data, **kwargs):
        calls.append(kwargs)
        # Model-free completion fixtures, preserving the real worker's state.
        state = read_json(tmp_path / L.RUN_STATE)
        publish(tmp_path, False)
        write_json(tmp_path / L.RUN_STATE, state)

    attempts = []
    def annotation(outdir, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise TimeoutError("transient service outage")

    monkeypatch.setattr(osp, "run_one_sample_pipeline", compute)
    monkeypatch.setattr(osp, "generate_report", lambda *_: None)
    monkeypatch.setattr(annotate, "propose_annotation", annotation)
    assert osp_worker.run(path) == 1
    state = read_json(tmp_path / L.RUN_STATE)
    assert state["stage"] == "annotation" and state["retryable"] is True
    assert osp_worker.run(path) == 0
    assert len(calls) == 1 and len(attempts) == 2
    assert calls[0]["qc_kwargs"] == {"run_scrublet": False, "run_decontx": False}
    assert calls[0]["cluster_kwargs"] == {"resolutions": (0.7,), "primary_resolution": 0.7}
    assert calls[0]["sample_col"] == "eca_sample_id"
    assert attempts[0]["language"] == "Chinese" and attempts[0]["effort"] == "high"
    assert is_done(tmp_path, True, "test")
    assert read_json(tmp_path / L.RUN_STATE)["attempt"] == 2


def test_deterministic_failure_is_not_retryable(tmp_path, monkeypatch):
    import osp
    path = request(tmp_path, False)
    def fail(*args, **kwargs):
        raise ValueError("primary clustering has fewer than two clusters")
    monkeypatch.setattr(osp, "run_one_sample_pipeline", fail)
    assert osp_worker.run(path) == 1
    state = read_json(tmp_path / L.RUN_STATE)
    assert state["failure_kind"] == "input_or_compute" and not state["retryable"]
    assert not is_done(tmp_path)


@pytest.mark.parametrize("survived,kind", [(0, "qc_zero_survivors"), (2, "qc_too_few_survivors")])
def test_zero_and_insufficient_qc_survivors_are_distinct(tmp_path, monkeypatch, survived, kind):
    import osp
    path = request(tmp_path, False)
    def fail(*args, **kwargs):
        pd.Series({"n_cells": 7, "n_low_quality": 7 - survived}).to_csv(tmp_path / "qc_summary.csv")
        raise ValueError("at least 3 are required")
    monkeypatch.setattr(osp, "run_one_sample_pipeline", fail)
    assert osp_worker.run(path) == 1
    assert read_json(tmp_path / L.RUN_STATE)["failure_kind"] == kind


def test_nonzero_process_cannot_use_leftover_success(tmp_path, monkeypatch):
    import sys
    publish(tmp_path, False)
    monkeypatch.setattr(persample, "plan_concurrency", lambda _: (1, 1 << 30, 1))
    monkeypatch.setattr(persample.time, "sleep", lambda _: None)
    entry = {"value": "sample", "n_cells": 7, "outdir": str(tmp_path),
             "command": [sys.executable, "-c", "raise SystemExit(1)"]}
    assert persample.drive([entry], tmp_path, False) == [entry]


def test_subset_rebuild_preserves_checkpoint_cell_identity(tmp_path):
    import pandas as pd
    h5 = tmp_path / "full.h5ad"
    a = matrix()
    a.write_h5ad(h5)
    out = tmp_path / "sample"
    table = pd.DataFrame({"eca_sample_id": "A"}, index=a.obs_names)
    entry = {"value": "A", "outdir": str(out), "request": {}}
    persample.write_subsets(h5, table, [entry])
    identity = file_identity(out / INPUT_CELLS)
    persample.write_subsets(h5, table, [entry])
    assert file_identity(out / INPUT_CELLS) == identity
