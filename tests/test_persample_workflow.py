import pytest

from ecarsi.persample_workflow import validate_spec
from ecarsi.run_state import file_identity
from ecarsi.warm_pool.state import save


def test_only_published_organize_units_can_start(tmp_path):
    unit = tmp_path / "organize/units/a"
    (unit / "input").mkdir(parents=True)
    manifest = unit / "input/manifest.json"
    save(manifest, {"species": "human", "sample_mapping": {"path": "mapping.csv.gz"}})
    for name in ("pool", "bridge"):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        save(root / "config.json", {})
    budget = {"cpus": 1, "memory_mb": 64, "timeout_seconds": 30}
    spec = dict(run_id="test", dataset_id="A", unit=str(unit), output_root=str(tmp_path / "new"),
                pool_root=str(tmp_path / "pool"), bridge_root=str(tmp_path / "bridge"),
                partition_budget=budget, compute_budget=budget, tool_budget=budget, finalize_budget=budget,
                batch_size=1, max_in_flight_samples=2, max_batch_bytes=1024,
                config=dict(scrublet=True, decontx=True, resolution=1.0, tissue="prostate"))
    with pytest.raises(ValueError, match="accepted Organize"):
        validate_spec(spec)
    save(unit.parent.parent / "publication.json", {"units": [{"name": "a", "manifest": file_identity(manifest)}]})
    assert validate_spec(spec)["config"]["species"] == "human"
    gpu_config = {**spec["config"], "compute_backend": "auto", "gpu_min_cells": 5000, "gpu_memory_mb": 8192}
    assert validate_spec({**spec, "config": gpu_config})["config"]["compute_backend"] == "auto"
    with pytest.raises(ValueError, match="GPU execution"):
        validate_spec({**spec, "config": {**gpu_config, "gpu_memory_mb": 0}})
    save(manifest, {"species": "mouse", "sample_mapping": {"path": "mapping.csv.gz"}})
    with pytest.raises(ValueError, match="accepted Organize"):
        validate_spec(spec)


def test_sample_size_selects_gpu_alternative_before_submission(tmp_path):
    from ecarsi.agent_session import reference
    from ecarsi.persample_workflow import sample_step
    from ecarsi.warm_pool.state import read
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    (pool / "requests").mkdir()
    save(pool / "config.json", {"runtime": {}})
    save(tmp_path / "bundle.json", {})
    spec = dict(run_id="r", dataset_id="D", output_root=str(tmp_path / "run"), pool_root=str(pool),
                compute_budget={"cpus": 1, "memory_mb": 8192, "timeout_seconds": 60},
                config={"compute_backend": "auto", "gpu_min_cells": 5000, "gpu_memory_mb": 8192})
    for sample, cells in (("small", 100), ("large", 10000)):
        task = sample_step("compute", [spec, {"sample_id": sample, "n_cells": cells,
                           "bundle": reference(tmp_path / "bundle.json")}, "prepared"])
        request = read(pool / "requests" / task["id"] / "request.json")["spec"]
        assert bool(request.get("gpu")) == (cells >= 5000)


def test_recovery_requires_resolved_receipts(tmp_path):
    from ecarsi.persample_workflow import sample_step
    from ecarsi.warm_pool.state import submit
    for name in ("pool", "bridge"):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        (root / "requests").mkdir()
        save(root / "config.json", {"runtime": {}})
    spec = dict(run_id="r", pool_root=str(tmp_path / "pool"), bridge_root=str(tmp_path / "bridge"))
    trace = dict(workflow_id="persample/r", dataset_id="D", unit_id="osp.compute", sample_id="s")
    task = submit(spec["pool_root"], dict(request_id="c", operation_id="c", trace=trace,
        args=["-c", "pass"], cpus=1, memory_mb=64, timeout_seconds=10, outputs=["x"]))
    save(tmp_path / "pool/requests/c" / task["attempt_id"] / "receipt.json", {"state": "succeeded"})
    turn = tmp_path / "bridge/requests/a"
    turn.mkdir()
    save(turn / "request.json", {"submitted_at": 0, "spec": {"trace": trace}})
    save(turn / "state.json", {"state": "unknown_external_result"})
    assert not sample_step("recoverable", [spec, "s"])
    save(turn / "result.json", {"state": "reply_saved"})
    assert sample_step("recoverable", [spec, "s"])
    save(tmp_path / "pool/requests/c" / task["attempt_id"] / "receipt.json", {"state": "failed"})
    assert not sample_step("recoverable", [spec, "s"])


def test_resume_retains_incomplete_publication_and_cannot_replace_complete(tmp_path):
    from ecarsi.agent_session import reference
    from ecarsi.persample_workflow import sample_step
    from ecarsi.warm_pool.state import read
    save(tmp_path / "input.json", {})
    spec = {"output_root": str(tmp_path), "input_manifest": reference(tmp_path / "input.json")}
    totals = {"total_samples": 1, "n_input": 2, "n_excluded": 0, "exclusions": reference(tmp_path / "input.json")}
    sample_step("publish", [spec, [], [{"sample": "s", "error": "local restore"}], totals])
    incomplete = read(tmp_path / "publication.json")
    save(tmp_path / "final.json", {"sample": "s", "validation": {"n_input": 2, "n_survived": 2, "n_removed": 0}})
    sample_step("publish", [spec, [str(tmp_path / "final.json")], [], totals])
    assert read(tmp_path / "publication.json")["state"] == "complete"
    assert incomplete in [read(p) for p in tmp_path.glob("publication-*.json")]
    with pytest.raises(ValueError, match="Cannot replace"):
        sample_step("publish", [spec, [], [{"sample": "s", "error": "later"}], totals])


def test_admission_update_cannot_deadlock_an_uninitialized_or_larger_batch():
    from ecarsi.persample_workflow import PersampleWorkflow
    workflow = PersampleWorkflow()
    with pytest.raises(ValueError, match="not initialized"):
        workflow.set_in_flight_limit(1)
    workflow._batch_size = 3
    for value in (2, True, 3.5):
        with pytest.raises(ValueError, match="at least as large as batch_size"):
            workflow.set_in_flight_limit(value)
    assert workflow.set_in_flight_limit(16) == 16
    assert workflow.in_flight_limit() == 16
