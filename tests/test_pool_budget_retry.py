"""Budget-class pool failures are retried once larger, or on CPUs for a preferred GPU."""
import pytest

from ecarsi.control.coordinator import check_pool_once
from ecarsi.warm_pool import state


def pool_with_failure(tmp_path, spec, error, retryable=False):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700, parents=True)
    (pool / "requests").mkdir()
    state.save(pool / "config.json", {"runtime": {"command": ["/usr/bin/python3"], "files": {}, "version": "test"}})
    state.submit(str(pool), spec)
    request = state.read(pool / "requests" / spec["request_id"] / "request.json")
    state.save(pool / "requests" / spec["request_id"] / request["attempt_id"] / "receipt.json",
               dict(state="failed", finished_at=1.0, attempt_id=request["attempt_id"], request_digest=request["digest"],
                    runtime_digest=request["runtime_digest"], error=error, retryable=retryable, outputs=[]))
    return pool, request


SPEC = dict(request_id="compute-1", operation_id="cross-sample.compute-round", args=["-c", "pass"], cpus=4,
            memory_mb=4096, timeout_seconds=100, inputs=[], outputs=["done.json"])


def test_gpu_budget_failure_of_a_preferred_gpu_is_retried_on_cpus(tmp_path):
    spec = dict(SPEC, gpu={"mode": "preferred", "memory_mb": 4096})
    pool, first = pool_with_failure(tmp_path, spec, "MemoryError: attempt exceeded its GPU memory budget")
    assert check_pool_once(str(pool), "compute-1", "done.json") == {"state": "waiting"}
    again = state.read(pool / "requests" / "compute-1" / "request.json")
    assert "gpu" not in again["spec"] and again["retry_count"] == 1 and again["retry"]["without_gpu"] is True
    assert again["attempt_id"] != first["attempt_id"] and again["digest"] == state.digest(again["spec"])
    assert again["original_digest"] == first["digest"]


def test_gpu_budget_failure_of_a_required_gpu_stays_failed(tmp_path):
    spec = dict(SPEC, gpu={"mode": "required", "memory_mb": 4096})
    pool, _ = pool_with_failure(tmp_path, spec, "MemoryError: attempt exceeded its GPU memory budget")
    assert check_pool_once(str(pool), "compute-1", "done.json")["state"] == "failed"
    with pytest.raises(ValueError, match="preferred"):
        state.retry(str(pool), "compute-1", reason="no", without_gpu=True)


def test_time_limit_failure_is_retried_at_twice_the_limit(tmp_path):
    pool, _ = pool_with_failure(tmp_path, SPEC, "TimeoutError: execution time limit reached")
    with pytest.raises(ValueError, match="exceed"):
        state.retry(str(pool), "compute-1", reason="no", timeout_seconds=100)
    assert check_pool_once(str(pool), "compute-1", "done.json") == {"state": "waiting"}
    again = state.read(pool / "requests" / "compute-1" / "request.json")
    assert again["spec"]["timeout_seconds"] == 200 and again["spec"]["time_request_seconds"] == 230


def test_other_failures_keep_the_worker_verdict(tmp_path):
    pool, _ = pool_with_failure(tmp_path, SPEC, "RuntimeError: command exited with status 1")
    assert check_pool_once(str(pool), "compute-1", "done.json") == {"state": "failed", "detail": "RuntimeError: command exited with status 1"}
    pool2, _ = pool_with_failure(tmp_path / "b", SPEC, "InterruptedError: worker execution was interrupted", retryable=True)
    assert check_pool_once(str(pool2), "compute-1", "done.json") == {"state": "waiting"}
    assert state.read(pool2 / "requests" / "compute-1" / "request.json")["retry_count"] == 1
