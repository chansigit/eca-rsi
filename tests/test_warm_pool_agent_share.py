"""Model turns take a slice of a core; live neighbours on that core are not orphans."""
import fcntl
import os
import socket
import time

from ecarsi.warm_pool.backend import AGENT_CALL_SHARE, hq_shares
from ecarsi.warm_pool.state import save
from ecarsi.warm_pool.worker import identity, reconcile_local


def test_model_turns_ask_for_a_slice_and_compute_for_whole_cores():
    assert hq_shares({"operation_id": "agent.call", "cpus": 1}) == (AGENT_CALL_SHARE, AGENT_CALL_SHARE)
    assert hq_shares({"operation_id": "zoom-in.deg", "cpus": 2}) == ("2", "1")
    assert 0 < float(AGENT_CALL_SHARE) < 1


class _hold:
    """A live executor holds the attempt directory's flock; flock conflicts across descriptors."""
    def __init__(self, attempt):
        self.fd = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        os.close(self.fd)


def _accepted_attempt(root, name, operation, cpu, ident=None):
    # Two worker directories: this host ran here before, so its index is seeded from the
    # requests rather than assumed empty (a host joining for the first time skips that).
    for previous in ("old-job", "this-job"):
        (root / "worker-state" / f"{socket.gethostname().split('.')[0]}-{previous}").mkdir(parents=True, exist_ok=True)
    folder = root / "requests" / name
    attempt = folder / "attempt"
    attempt.mkdir(parents=True)
    save(folder / "request.json", dict(spec=dict(request_id=name, operation_id=operation),
                                       attempt_id="attempt", digest="d", runtime_digest="r"))
    save(attempt / "accepted.json", dict(host=socket.gethostname().split(".")[0], cpu_ids=[cpu],
                                         gpu_ids=[], identity=ident or identity(os.getpid()), started_at=time.time()))
    return attempt


def test_live_model_turn_neighbour_is_not_pending_for_another_model_turn(tmp_path):
    cpu = sorted(os.sched_getaffinity(0))[0]
    attempt = _accepted_attempt(tmp_path, "turn-a", "agent.call", cpu)
    # A live executor holds the attempt's execution lock; flock conflicts across descriptors.
    holder = _hold(attempt)
    try:
        assert reconcile_local(tmp_path, [cpu], shared=True) == []
        assert reconcile_local(tmp_path, [cpu]) == ["turn-a"]
    finally:
        holder.close()


def test_live_compute_neighbour_still_blocks_a_model_turn(tmp_path):
    cpu = sorted(os.sched_getaffinity(0))[0]
    attempt = _accepted_attempt(tmp_path, "deg-a", "zoom-in.deg", cpu)
    holder = _hold(attempt)
    try:
        assert reconcile_local(tmp_path, [cpu], shared=True) == ["deg-a"]
    finally:
        holder.close()


def test_dead_model_turn_on_the_core_still_gets_a_worker_lost_receipt(tmp_path):
    cpu = sorted(os.sched_getaffinity(0))[0]
    dead = dict(identity(os.getpid()), pid=1, start_ticks=0)  # same boot, no such process start
    attempt = _accepted_attempt(tmp_path, "turn-dead", "agent.call", cpu, ident=dead)
    assert reconcile_local(tmp_path, [cpu], shared=True) == []
    assert (attempt / "receipt.json").is_file()
    assert "WorkerLost" in (attempt / "receipt.json").read_text()


def test_a_host_joining_for_the_first_time_does_not_walk_every_request(tmp_path):
    """85k request folders on Lustre; a node that never ran here holds none of them."""
    cpu = sorted(os.sched_getaffinity(0))[0]
    attempt = _accepted_attempt(tmp_path, "turn-a", "agent.call", cpu)
    host = socket.gethostname().split(".")[0]
    for stale in (tmp_path / "worker-state").iterdir():
        stale.rmdir()
    (tmp_path / "worker-state" / f"{host}-fresh-job").mkdir(parents=True)
    (tmp_path / "cache" / "active" / host).mkdir(parents=True, exist_ok=True)

    holder = _hold(attempt)
    try:
        assert reconcile_local(tmp_path, [cpu]) == []  # not seeded from the walk
    finally:
        holder.close()
