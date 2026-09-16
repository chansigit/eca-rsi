"""An attempt accepted by a node whose Slurm grant ended gets a retryable WorkerLost receipt."""
import time

from ecarsi.warm_pool.backend import allocation_ended, worker_lost_receipt
from ecarsi.warm_pool.state import save


def _identity(root, name, host, expires_at):
    (root / "workers" / name).mkdir(parents=True)
    save(root / "workers" / name / "identity.json", dict(host=host, worker_id=name, expires_at=expires_at))


def test_ended_grant_with_no_connected_worker_is_over(tmp_path):
    now = time.time()
    _identity(tmp_path, "h1-a", "h1", now - 1000)
    accepted = dict(host="h1", started_at=now - 2000)
    assert allocation_ended(tmp_path, accepted, live_hosts=[], now=now)
    assert not allocation_ended(tmp_path, accepted, live_hosts=["h1.int"], now=now)  # host owns its orphans
    assert not allocation_ended(tmp_path, accepted, live_hosts=[], now=now - 900)  # inside the grace window


def test_future_or_unknown_grants_are_not_over(tmp_path):
    now = time.time()
    _identity(tmp_path, "h1-a", "h1", now - 1000)
    _identity(tmp_path, "h1-b", "h1", now + 1000)  # a newer registration on the same host
    assert not allocation_ended(tmp_path, dict(host="h1"), live_hosts=[], now=now)
    assert not allocation_ended(tmp_path, dict(host="h9"), live_hosts=[], now=now)


def test_worker_lost_receipt_is_retryable_and_keeps_identity():
    request = dict(spec=dict(request_id="r"), attempt_id="a", digest="d", runtime_digest="rt")
    receipt = worker_lost_receipt(request, dict(started_at=1.0), "WorkerLost: test")
    assert receipt["state"] == "failed" and receipt["retryable"] is True
    assert (receipt["request_id"], receipt["attempt_id"], receipt["request_digest"], receipt["runtime_digest"]) == ("r", "a", "d", "rt")
