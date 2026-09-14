import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from ecarsi.pool.client import PoolEndpoint, runtime
from ecarsi.pool.scheduler import PoolScheduler, dispatch
from tests.test_pool import profile, request


def test_resource_packing_keeps_reservations_and_runtime_checks():
    pool = PoolScheduler()
    pool.scheduler = SimpleNamespace(workers={'a': None}, clients={'driver': None})
    p = dict(profile(), cpus=4, cpu_ids=[0, 1, 2, 3], memory=4*2**30,
             task_slots=4, execution_protocol=2, runtimes={'cpu': runtime()})
    pool.handle('register', dict(address='a', profile=p))
    first = pool.handle('enqueue', request('first', cpus=2, memory=3*2**30))
    second = pool.handle('enqueue', request('second', cpus=1, memory=2**30))
    assert first['state'] == second['state'] == 'granted'
    assert set(first['cpu_ids']).isdisjoint(second['cpu_ids'])
    assert pool.handle('enqueue', request('third'))['reason'] == 'reserved_capacity'
    assert pool.handle('enqueue', request('wrong', runtime={'numpy': 'wrong'}))['reason'] == 'runtime'
    pool.handle('claim', dict(id='first', worker='a'))
    pool.handle('finish', dict(id='first', worker='a', ok=True))
    assert pool.tasks['third']['state'] == 'granted'
    # A timed-out task retains its budget until the host fences its worker.
    pool.handle('claim', dict(id='second', worker='a'))
    pool.stop_task(pool.tasks['second'], 'execution deadline exceeded')
    assert pool.handle('enqueue', request('fourth'))['state'] == 'queued'


def isolated_work(marker):
    import os
    import time
    from pathlib import Path
    start = time.time()
    os.environ['POOL_TEST_MARKER'] = marker
    time.sleep(2)
    return dict(pid=os.getpid(), cpus=sorted(os.sched_getaffinity(0)), marker=os.environ['POOL_TEST_MARKER'],
                start=start, end=time.time(), threads=os.environ['OMP_NUM_THREADS'])


def test_one_worker_runs_two_isolated_tasks_at_once(monkeypatch):
    from distributed import Client, LocalCluster
    cpus = sorted(os.sched_getaffinity(0))[:2]
    assert len(cpus) == 2
    monkeypatch.setenv('ECA_POOL_EXECUTORS', json.dumps({'default': sys.executable}))
    p = dict(profile(), cpus=2, cpu_ids=cpus, memory=2*2**30,
             task_slots=2, execution_protocol=2, runtimes={'default': runtime()})
    with LocalCluster(n_workers=1, threads_per_worker=2, processes=True, dashboard_address=None,
                      preload=['ecarsi.pool.executor'], resources={'pool_slot': 2}, memory_limit=2*2**30) as cluster, Client(cluster) as c:
        c.register_plugin(PoolScheduler())
        address = next(iter(c.scheduler_info()['workers']))
        c.run_on_scheduler(dispatch, 'register', dict(address=address, profile=p))
        def driver(marker):
            with PoolEndpoint(cluster.scheduler_address) as pool:
                return pool.submit(isolated_work, marker, needs=dict(cpus=1, memory=2**28, seconds=10)).result(timeout=45)
        with ThreadPoolExecutor(2) as threads:
            a,b = threads.map(driver, ['a','b'])
        assert a['pid'] != b['pid']
        assert a['marker'] == 'a' and b['marker'] == 'b'
        assert a['threads'] == b['threads'] == '1'
        assert set(a['cpus']).isdisjoint(b['cpus'])
        assert max(a['start'], b['start']) < min(a['end'], b['end'])
        state = c.run_on_scheduler(dispatch, 'status')
        assert len(state['tasks']) == 2 and all(t['state'] == 'done' for t in state['tasks'].values())
