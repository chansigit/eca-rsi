"""Admission safety plus two actual Dask drivers; no Slurm allocation required."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

pytest.importorskip("distributed")
from distributed import Client, LocalCluster

from ecarsi.pool.client import PoolEndpoint, runtime
from ecarsi.pool.scheduler import PoolScheduler, dispatch
from ecarsi.pool.slurm import memory_bytes


def test_worker_loads_default_runtime_config(monkeypatch, tmp_path):
    from ecarsi.pool import executor

    config = tmp_path / '.config/ecarsi/pool-runtimes.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'cpu': '/cpu/python', 'gpu': '/gpu/python'}))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('ECA_POOL_RUNTIMES', raising=False)
    seen = []

    def probe(command, **_):
        seen.append(command[0])
        return SimpleNamespace(stdout=json.dumps({'numpy': '2.5.3'}))

    monkeypatch.setattr(executor.subprocess, 'run', probe)
    commands, profiles = executor.runtimes()
    assert list(commands) == ['cpu', 'gpu']
    assert seen == ['/cpu/python', '/gpu/python']
    assert profiles['cpu']['numpy'] == '2.5.3'


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
    assert first["execution_timeout"] == 21600  # independent of a ten-second estimate
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
    with pytest.raises(ConnectionError, match="restarted"):
        pool.handle("poll", {"id": "draining", "epoch": "old"})
    now = time.time()
    w = profile()
    for field, value, reason in (("gpus", 1, "gpus"), ("memory", 10**12, "memory"),
                                ("seconds", 10000, "remaining_time"), ("runtime", {"ecarsi": "old"}, "runtime"),
                                ("roots", ["/unshared"], "shared_paths")):
        assert pool.fits(request("x", **{field: value}), w, now) == reason
    gpu_worker = dict(w, gpus=1, gpu_ids=["GPU-test"])
    gpu_task = request("gpu", gpus=1, required_runtime=["rapids-singlecell-cu12", "cupy-cuda12x"])
    assert pool.fits(gpu_task, gpu_worker, now) == "runtime"
    gpu_worker["runtime"] = dict(w["runtime"], **{"rapids-singlecell-cu12": "0.17.0",
                                                  "cupy-cuda12x": gpu_task["runtime"].get("cupy-cuda12x", "14.2.0")})
    gpu_worker["runtimes"] = {"default": gpu_worker["runtime"]}
    assert pool.fits(gpu_task, gpu_worker, now) == ""
    assert memory_bytes("64G") == 64 * 2**30 and memory_bytes("1024") == 2**30
    with pytest.raises(ValueError):
        memory_bytes("unknown")


def work(value):
    from distributed import get_worker
    time.sleep(.1)
    return value + 1, get_worker().address


def test_running_deadline_and_disconnected_owner_keep_slot_until_worker_removal(monkeypatch):
    pool = PoolScheduler()
    pool.scheduler = SimpleNamespace(workers={"a": None, "b": None}, clients={"driver": None})
    pool.handle("register", {"address": "a", "profile": profile()})
    with pytest.raises(ValueError, match="execution_timeout"):
        pool.handle("enqueue", request("bad", execution_timeout=float("nan")))
    pool.handle("enqueue", request("hung", execution_timeout=1))
    pool.handle("claim", {"id": "hung", "worker": "a"})
    pool.handle("enqueue", request("next"))
    pool.tasks["hung"]["execution_deadline"] = time.time() - 1
    pool.tick()
    assert pool.tasks["hung"]["state"] == "stopping"
    assert pool.tasks["next"]["state"] == "queued"
    pool.handle("register", {"address": "a", "profile": profile()})
    assert pool.workers["a"]["recycle_reason"] == "execution deadline exceeded"
    with pytest.raises(ConnectionError, match="retired"):
        pool.handle("finish", {"id": "hung", "worker": "a", "ok": True})
    pool.remove_worker(pool.scheduler, "a")
    assert pool.tasks["hung"]["state"] == "lost"
    pool.handle("register", {"address": "b", "profile": profile()})
    assert pool.tasks["next"]["state"] == "granted"
    pool.handle("claim", {"id": "next", "worker": "b"})
    pool.remove_client(pool.scheduler, "driver")
    assert pool.tasks["next"]["state"] == "stopping"
    assert pool.workers["b"]["recycle_reason"] == "driver disconnected"


def test_gpu_worker_keeps_host_memory_for_gpu_work(monkeypatch):
    monkeypatch.setenv('ECA_POOL_GPU_HOST_RESERVE_GB', '0.25')
    pool = PoolScheduler()
    pool.scheduler = SimpleNamespace(workers={'gpu': None}, clients={'driver': None})
    worker = dict(profile(), gpus=1, gpu_ids=['GPU-test'])
    pool.handle('register', {'address': 'gpu', 'profile': worker})
    assert pool.handle('enqueue', request('large-cpu', memory=900 * 2**20))['state'] == 'queued'
    assert pool.handle('enqueue', request('gpu-graph', gpus=1, memory=512 * 2**20))['state'] == 'granted'
    assert pool.tasks['gpu-graph']['worker'] == 'gpu'


def test_completed_future_can_outlast_short_poll():
    from ecarsi.pool.client import PoolFuture

    class Transferring:
        calls = 0

        def done(self):
            return True

        def result(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("poll elapsed during result transfer")
            assert timeout is None or timeout > 2
            return 42

    assert PoolFuture(None, Transferring(), {}).result() == 42
    assert PoolFuture(None, Transferring(), {}).result(timeout=30) == 42
    # A TimeoutError raised by the computation must still reach the driver.
    from concurrent.futures import Future
    failed = Future()
    failed.set_exception(TimeoutError("computation timeout"))
    with pytest.raises(TimeoutError, match="computation timeout"):
        PoolFuture(None, failed, {}).result()


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


def test_gpu_device_minors_and_busy_status():
    from ecarsi.pool.slurm import gpu_inventory
    from ecarsi.pool.status import render, summarize
    xml = '<nvidia_smi_log><gpu><uuid>GPU-test</uuid><minor_number>2</minor_number><product_name>RTX</product_name><utilization><gpu_util>37 %</gpu_util></utilization><fb_memory_usage><used>1024 MiB</used><total>24576 MiB</total></fb_memory_usage></gpu></nvidia_smi_log>'
    ids, stats = gpu_inventory(xml, '0', '2')
    assert ids == ['GPU-test'] and stats[0]['utilization_percent'] == 37
    with pytest.raises(ValueError):
        gpu_inventory(xml, '0', '1')
    p = profile()
    p.update(cpus=4, gpu_stats=stats)
    state = {'workers': {'a': p}, 'tasks': {'x': dict(request('x'), worker='a', state='running', label='OSP')}}
    result = summarize(state, {'a': {'metrics': {'cpu': 200, 'memory': 2**29}}})
    assert result['workers'][0]['cpu_percent'] == 50
    assert result['workers'][0]['state'] == 'running'
    text = render(result)
    assert 'OSP' in text and '37.0%' in text and 'Queued: 0' in text
    assert summarize(state, {})['workers'][0]['state'] == 'stale'


def test_process_tree_usage_requires_fresh_matching_worker(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from ecarsi.pool.status import summarize
    monkeypatch.setattr(Path,'home',staticmethod(lambda:tmp_path))
    p = dict(profile(),boot_id='boot',supervisor=dict(pid=123))
    path = tmp_path/'.cache/ecarsi-pool/test/worker-123.usage.json'
    path.parent.mkdir(parents=True)
    usage = dict(pid=123,boot_id='boot',cpu_ids=p['cpu_ids'],observed_at=time.time(),cpu_percent=80,rss_bytes=1000)
    state = dict(workers={'a':p},tasks={})
    metrics = {'a':{'metrics':dict(cpu=1,memory=100)}}
    path.write_text(json.dumps(usage))
    result = summarize(state,metrics)['workers'][0]
    assert result['cpu_percent']==80 and result['rss_bytes']==1000 and result['metrics_source']=='process_tree'
    for changes in (dict(observed_at=time.time()-60),dict(cpu_ids=[999]),dict(boot_id='old')):
        path.write_text(json.dumps(dict(usage,**changes)))
        result = summarize(state,metrics)['workers'][0]
        assert result['cpu_percent']==1 and result['metrics_source']=='dask_process'


def test_worker_reclaims_retired_driver_cpu_locks_only_after_queue_drains(tmp_path, monkeypatch):
    import fcntl
    import json
    from pathlib import Path
    from ecarsi.pool.budget import reserve

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path/'.cache/ecarsi-pool/host'
    root.mkdir(parents=True)
    queue = tmp_path/'queue'
    queue.mkdir()
    old = dict(role='driver', memory=4, cpu_ids=[1], owner_lock=str(root/'driver.lock'),
               queue_directory=str(queue), nodes=['old'], observed_at=1)
    ledger = root/'budget-1.json'
    ledger.write_text(json.dumps({'driver:old': old}))
    (queue/'status.json').write_text(json.dumps({'datasets': []}))
    profile = dict(host='host', job_id='1', cpu_ids=[1], memory=4, allocation_memory=8)
    with (root/'cpu-1.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reserve(profile, 'worker:new', root/'cpu-1.lock', role='worker', owned_cpu_locks=True)
        assert set(json.loads(ledger.read_text())) == {'worker:new'}
        ledger.write_text(json.dumps({'driver:old': old}))
        (queue/'status.json').write_text(json.dumps({'datasets': [{'node': 'old', 'state': 'running'}]}))
        with pytest.raises(ValueError, match='overlap'):
            reserve(profile, 'worker:new', root/'cpu-1.lock', role='worker', owned_cpu_locks=True)


def test_pool_driver_budget_excludes_remote_compute(tmp_path, monkeypatch):
    from ecarsi.osp_dispatch import _driver_estimate_bytes
    from ecarsi.persample import _estimate_bytes, FIXED_BYTES_PER_CHILD
    entry = {'n_cells': 20000, 'outdir': str(tmp_path)}
    (tmp_path / 'subset.h5ad').write_bytes(b'x' * 1024)
    monkeypatch.setenv('OSP_COMPUTE_ENDPOINT', 'pool')
    assert _driver_estimate_bytes(entry) == FIXED_BYTES_PER_CHILD + 4096
    (tmp_path / 'computed.h5ad').write_bytes(b'x' * 8192)
    assert _driver_estimate_bytes(entry) == FIXED_BYTES_PER_CHILD + 32768
    for mode in ('local', 'auto'):
        monkeypatch.setenv('OSP_COMPUTE_ENDPOINT', mode)
        assert _driver_estimate_bytes(entry) == _estimate_bytes(20000)
