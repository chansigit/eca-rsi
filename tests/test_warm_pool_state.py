import time
import socket

import pytest

from ecarsi.warm_pool.backend import check_runtime
from ecarsi.warm_pool.worker import registered_worker_id
from ecarsi.warm_pool.state import cancel, file_digest, read, save, status, submit, validate_trace


def test_runtime_paths_override_launcher_and_imports_are_checked(tmp_path, monkeypatch):
    import sys
    from ecarsi.warm_pool.backend import runtime_environment
    (tmp_path / "pool_probe.py").write_text("VALUE = 'configured runtime'\n")
    monkeypatch.setenv("PYTHONPATH", "/missing/launcher/path")
    runtime = {"command": [sys.executable], "version": "Python " + sys.version.split()[0],
               "pythonpath": [str(tmp_path)], "imports": ["pool_probe"]}
    assert runtime_environment(runtime)["PYTHONPATH"] == str(tmp_path)
    check_runtime(runtime, imports=True)
    with pytest.raises(ValueError, match="absolute directories"):
        runtime_environment({**runtime, "pythonpath": ["relative"]})


def test_trace_dependencies_are_explicit_and_bounded():
    trace = {"workflow_id": "organize/run-a", "dataset_id": "dataset-a",
             "unit_id": "organize.plan", "depends_on": ["run-a.prepare"]}
    assert validate_trace(trace) == trace
    for parents in (["run-a.prepare", "run-a.prepare"], ["../escape"], "run-a.prepare", list(range(33))):
        with pytest.raises(ValueError):
            validate_trace(dict(trace, depends_on=parents))


def test_durable_idempotent_submission_and_read_only_status(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "requests").mkdir()
    save(tmp_path / "config.json", {"runtime": {"command": ["/usr/bin/python3"], "version": "test"}})
    spec = dict(request_id="sample-a", operation_id="osp-a", args=["-c", "pass"],
                cpus=1, memory_mb=128, timeout_seconds=10, outputs=["result.json"],
                trace={"workflow_id": "osp/run-a", "dataset_id": "dataset-a", "unit_id": "osp-a"})
    first = submit(tmp_path, spec)
    path = tmp_path / "requests/sample-a/request.json"
    before = path.read_bytes()
    assert read(path)["spec"]["trace"]["dataset_id"] == "dataset-a"
    assert submit(tmp_path, spec)["attempt_id"] == first["attempt_id"]
    assert status(tmp_path, "sample-a")["state"] == "queued"
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="different content"):
        submit(tmp_path, dict(spec, memory_mb=256))
    assert cancel(tmp_path, "sample-a")["state"] == "cancel_requested"
    assert submit(tmp_path, spec)["state"] == "cancel_requested"
    assert path.read_bytes() == before
    for change in [dict(request_id="../escape"), dict(outputs=["../escape"]), dict(cpus=0),
                   dict(memory_mb=True), dict(outputs=["a//b"]), dict(outputs=["bad\0file"])]:
        with pytest.raises(ValueError):
            submit(tmp_path, dict(spec, **change))
    assert len(status(tmp_path)) == 1
    attempt = tmp_path / "requests/sample-a" / first["attempt_id"]
    # Stale executor observations cannot be advertised as currently running.
    (tmp_path / "requests/sample-a/cancel.json").unlink()
    save(attempt / "accepted.json", dict(started_at=time.time() - 30))
    assert status(tmp_path, "sample-a")["state"] == "unknown_external_result"
    save(attempt / "usage.json", dict(observed_at=time.time()))
    assert status(tmp_path, "sample-a")["state"] == "running"


def test_partial_json_never_overwrites_an_acknowledged_record(tmp_path):
    path = tmp_path / "state.json"
    save(path, {"acknowledged": True})
    with pytest.raises(ValueError):
        save(path, {"bad": float("nan")})
    assert read(path) == {"acknowledged": True}


def test_worker_identity_resolves_only_an_unambiguous_cpu_grant(tmp_path):
    host = socket.gethostname().split(".")[0]
    for name, cpus in (("worker-a", [1, 2]), ("worker-b", [3, 4])):
        folder = tmp_path / "workers" / name
        folder.mkdir(parents=True)
        save(folder / "identity.json", {"worker_id": name, "host": host, "cpu_ids": cpus})
    assert registered_worker_id(tmp_path, [2]) == "worker-a"
    assert registered_worker_id(tmp_path, [5]) is None
    save(tmp_path / "workers/worker-b/identity.json",
         {"worker_id": "worker-b", "host": host, "cpu_ids": [2, 3]})
    assert registered_worker_id(tmp_path, [2]) is None


def test_changed_runtime_rejected_before_its_probe_can_execute(tmp_path):
    binary = tmp_path / "runtime"
    binary.write_bytes(b"original")
    runtime = dict(command=[str(binary)], version="unused", files={str(binary): file_digest(binary)})
    binary.write_bytes(b"changed")
    with pytest.raises(ValueError, match="runtime file identity mismatch"):
        check_runtime(runtime)


def test_cancellation_wins_over_a_late_compute_receipt(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "requests").mkdir()
    save(tmp_path / "config.json", {"runtime": {"command": ["/usr/bin/python3"]}})
    spec = dict(request_id="a", operation_id="a", args=["-c", "pass"], cpus=1,
                memory_mb=64, timeout_seconds=10, outputs=["x"])
    request = submit(tmp_path, spec)
    cancel(tmp_path, "a")
    path = tmp_path / "requests/a" / request["attempt_id"] / "receipt.json"
    save(path, dict(state="succeeded", outputs=[]))
    result = status(tmp_path, "a")
    assert result["state"] == "cancelled"
    assert result["receipt"]["state"] == "succeeded"  # retained for audit, not accepted downstream


def test_runtime_update_preserves_accepted_request_identity(tmp_path):
    import subprocess
    import sys
    from ecarsi.warm_pool.__main__ import configure_runtime
    tmp_path.chmod(0o700)
    (tmp_path / "requests").mkdir()
    old = {"command": [sys.executable], "version": subprocess.check_output([sys.executable, "--version"], text=True).strip(), "files": {}}
    save(tmp_path / "config.json", {"runtime": old})
    spec = dict(request_id="old", operation_id="a", args=["-c", "pass"], cpus=1,
                memory_mb=64, timeout_seconds=10, outputs=["x"])
    submit(tmp_path, spec)
    new = {**old, "pythonpath": [str(tmp_path)], "imports": ["json"]}
    configure_runtime(tmp_path, new)
    submit(tmp_path, {**spec, "request_id": "new"})
    assert read(tmp_path / "requests/old/request.json")["runtime"] == old
    assert read(tmp_path / "requests/new/request.json")["runtime"] == new
    with pytest.raises(ValueError):
        configure_runtime(tmp_path, {**new, "pythonpath": ["relative"]})
    assert read(tmp_path / "config.json")["runtime"] == new
