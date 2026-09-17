"""Measured memory ceilings at submission, RSS kills retryable at 2x, legacy identity kept."""
import os
import subprocess
import sys

import pytest

from ecarsi.operation_budget import MEASURED_CEILING_MB, measured_ceiling
from ecarsi.warm_pool.state import digest, read, save, status, submit, validate


def _spec(name, operation, memory_mb, code="pass"):
    return dict(request_id=name, operation_id=operation, args=["-c", code], cpus=1,
                memory_mb=memory_mb, timeout_seconds=30, outputs=["result.json"])


def _pool(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "requests").mkdir()
    runtime = dict(command=[sys.executable], version="Python " + sys.version.split()[0], files={})
    save(tmp_path / "config.json", dict(runtime=runtime))
    return tmp_path


def test_cpu_counts_are_capped_from_measurements_too():
    from ecarsi.operation_budget import MEASURED_CPUS
    capped = measured_ceiling(dict(_spec("d", "zoom-in.deg", 2560), cpus=2))
    assert capped["cpus"] == MEASURED_CPUS["zoom-in.deg"] == 1 and capped["memory_mb"] == 2560
    assert measured_ceiling(dict(_spec("d", "zoom-in.deg", 2560), cpus=1))["cpus"] == 1
    assert measured_ceiling(dict(_spec("c", "cross-sample.compute", 12288), cpus=8))["cpus"] == 8


def test_ceiling_caps_listed_operations_only_and_never_raises():
    assert measured_ceiling(_spec("a", "submit_quality", 24576))["memory_mb"] == MEASURED_CEILING_MB["submit_quality"]
    assert measured_ceiling(_spec("a", "submit_quality", 1024))["memory_mb"] == 1024
    assert measured_ceiling(_spec("a", "cross-sample.compute", 24576))["memory_mb"] == 24576
    assert all(v % 256 == 0 and 1024 <= v <= 12288 for v in MEASURED_CEILING_MB.values())


def test_submit_applies_ceiling_and_still_accepts_the_uncapped_identity(tmp_path):
    root = _pool(tmp_path)
    spec = _spec("q", "submit_quality", 24576)
    assert submit(root, spec)["state"] == "queued"
    saved = read(root / "requests" / "q" / "request.json")
    assert saved["spec"]["memory_mb"] == MEASURED_CEILING_MB["submit_quality"]
    assert saved["digest"] == digest(saved["spec"])
    submit(root, spec)  # an activity retry re-sends the same request
    # A request saved before ceilings existed keeps answering to its uncapped digest.
    legacy = dict(saved, spec=validate(spec), digest=digest(validate(spec)))
    save(root / "requests" / "q" / "request.json", legacy)
    assert submit(root, spec)["state"] == "queued"
    with pytest.raises(ValueError):
        submit(root, dict(spec, args=["-c", "print(1)"]))


def test_rss_kill_is_retryable_and_check_pool_doubles_the_budget(tmp_path):
    root = _pool(tmp_path)
    grab = "x = bytearray(300 * 2**20); import time; time.sleep(5)"
    submit(root, _spec("big", "test.oom", 64, grab))
    request = read(root / "requests" / "big" / "request.json")
    cpu = min(os.sched_getaffinity(0))
    proc = subprocess.run([sys.executable, "-m", "ecarsi.warm_pool.worker", "execute", str(root), "big",
                           request["attempt_id"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, preexec_fn=lambda: os.sched_setaffinity(0, {cpu}), timeout=60)
    assert proc.returncode == 1
    receipt = read(root / "requests" / "big" / request["attempt_id"] / "receipt.json")
    assert receipt["state"] == "failed" and receipt["retryable"] is True
    assert receipt["error"].startswith("MemoryError") and "RSS" in receipt["error"]
    from ecarsi.work_coordinator import check_pool
    assert check_pool(str(root), "big", "result.json") == {"state": "waiting"}
    retried = read(root / "requests" / "big" / "request.json")
    assert retried["spec"]["memory_mb"] == 128 and retried["retry_count"] == 1
    assert retried["original_digest"] == request["digest"]
    assert status(root, "big")["state"] == "queued"
