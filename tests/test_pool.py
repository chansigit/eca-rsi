"""Admission safety plus two actual Dask drivers; no Slurm allocation required."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

pytest.importorskip("distributed")
from distributed import Client, LocalCluster

from ecarsi.pool.client import PoolEndpoint, runtime
from ecarsi.pool.scheduler import PoolScheduler, dispatch
from ecarsi.pool.slurm import memory_bytes


def profile(cpu=0):
    return {"cpus": 1, "cpu_ids": [cpu], "memory": 2**30, "allocation_memory": 4 * 2**30,
            "gpus": 0, "gpu_ids": [], "host": "test", "job_id": "1", "runtime": runtime(),
            "roots": ["/data"], "end_time": time.time() + 7200, "observed_at": time.time()}


def request(key, **kwargs):
    return dict({"id": key, "owner": "driver", "cpus": 1, "memory": 2**20, "gpus": 0,
                 "seconds": 10, "runtime": runtime(), "roots": []}, **kwargs)


def test_admission_lifecycle(monkeypatch):
    pool = PoolScheduler()
    pool.scheduler = SimpleNamespace(workers={"a": None, "b": None}, clients={"driver": None})
    pool.handle("register", {"address": "a", "profile": profile()})
    with pytest.raises(ValueError, match="overlapping"):
        pool.handle("register", {"address": "b", "profile": profile()})
    first = pool.handle("enqueue", request("first"))
    assert first["state"] == "granted"
    pool.handle("claim", {"id": "first", "epoch": pool.epoch, "worker": "a"})
    assert pool.handle("cancel", {"id": "first"})["state"] == "running"
    # A too-large old request keeps its place; fitting requests may pass it.
    assert pool.handle("enqueue", request("big", cpus=2))["state"] == "queued"
    assert pool.handle("enqueue", request("second"))["state"] == "queued"
    pool.handle("enqueue", request("third"))
    pool.handle("finish", {"id": "first", "worker": "a", "ok": True})
    assert pool.tasks["second"]["state"] == "granted" and pool.tasks["third"]["state"] == "queued"
    with pytest.raises(RuntimeError, match="no longer valid"):
        pool.handle("claim", {"id": "first", "worker": "a"})  # Dask recomputation is fenced
    pool.remove_worker(pool.scheduler, "a")
    assert pool.tasks["second"]["state"] == "lost"
    pool.handle("register", {"address": "b", "profile": profile(1)})
    assert pool.tasks["third"]["state"] == "granted"
    pool.remove_client(pool.scheduler, "driver")
    assert pool.tasks["third"]["state"] == pool.tasks["big"]["state"] == "cancelled"
    pool.handle("drain", {"address": "b"})
    waiting = pool.handle("enqueue", request("draining"))
    assert waiting["reason"] == "draining_or_stale"
    pool.tasks["draining"]["lease"] = 0
    pool.tick()
    assert pool.tasks["draining"]["state"] == "expired"
    with pytest.raises(RuntimeError, match="restarted"):
        pool.handle("poll", {"id": "draining", "epoch": "old"})
    now = time.time()
    w = profile()
    for field, value, reason in (("gpus", 1, "gpus"), ("memory", 10**12, "memory"),
                                ("seconds", 10000, "remaining_time"), ("runtime", {"ecarsi": "old"}, "runtime"),
                                ("roots", ["/unshared"], "shared_paths")):
        assert pool.fits(request("x", **{field: value}), w, now) == reason
    assert memory_bytes("64G") == 64 * 2**30 and memory_bytes("1024") == 2**30
    with pytest.raises(ValueError):
        memory_bytes("unknown")


def work(value):
    from distributed import get_worker
    time.sleep(.1)
    return value + 1, get_worker().address


def test_two_drivers_and_auto_local(monkeypatch):
    with LocalCluster(n_workers=2, threads_per_worker=1, processes=False, dashboard_address=None,
                      resources={"pool_slot": 1}, memory_limit=2 * 2**30) as cluster, Client(cluster) as c:
        c.register_plugin(PoolScheduler())
        for i, address in enumerate(c.scheduler_info()["workers"]):
            c.run_on_scheduler(dispatch, "register", {"address": address, "profile": profile(i)})

        def driver(value):
            with PoolEndpoint(cluster.scheduler_address) as ep:
                return ep.submit(work, value, needs={"memory": 2**20, "seconds": 10}).result(timeout=20)

        with ThreadPoolExecutor(2) as threads:
            results = list(threads.map(driver, [1, 2]))
        assert [v for v, _ in results] == [2, 3]
        assert len({w for _, w in results}) == 2
        status = c.run_on_scheduler(dispatch, "status")
        assert all(t["state"] == "done" for t in status["tasks"].values())
        assert all(t["seconds_actual"] > 0 for t in status["tasks"].values())
    monkeypatch.setattr("ecarsi.pool.client.local_fits", lambda _: True)
    with PoolEndpoint(mode="auto") as ep:
        assert ep.submit(sum, [1, 2], needs={"seconds": 1}).result() == 3
        assert ep.client is None
