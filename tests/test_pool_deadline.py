"""A hung real Dask worker must lose its process group before replacement."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
pytest.importorskip('distributed')


def hang(child_file):
    child = subprocess.Popen(['sleep', '120'])
    Path(child_file).write_text(str(child.pid))
    while True:
        time.sleep(.1)


def healthy(old_child):
    path = Path('/proc') / str(old_child) / 'stat'
    return not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z'


def test_hung_task_recycles_worker_and_next_request_completes(tmp_path):
    from ecarsi.pool.client import PoolEndpoint, connect
    from ecarsi.pool.scheduler import dispatch
    cpu = min(os.sched_getaffinity(0))
    scheduler_file = tmp_path / 'scheduler.json'
    logs = (tmp_path / 'services.log').open('w')
    scheduler = subprocess.Popen([sys.executable, '-m', 'ecarsi.pool', 'scheduler',
                                   '--scheduler-file', str(scheduler_file), '--host', '127.0.0.1'],
                                  stdout=logs, stderr=logs, start_new_session=True)
    supervisor = None
    try:
        end = time.monotonic()+30
        while not scheduler_file.exists():
            assert scheduler.poll() is None
            assert time.monotonic() < end
            time.sleep(.1)
        address = json.loads(scheduler_file.read_text())['address']
        profile = dict(host='isolated', job_id='1', cpu_ids=[cpu], cpus=1, gpu_ids=[], gpus=0,
                       memory=2**30, allocation_memory=2**30, end_time=time.time()+180)
        code = f'''
import os,time
from pathlib import Path
from types import SimpleNamespace
from ecarsi.pool import slurm
os.sched_setaffinity(0, {{{cpu}}})
profile = {profile!r}
slurm.inventory = lambda *_: dict(profile, observed_at=time.time())
a = SimpleNamespace(directory=Path({str(tmp_path)!r}), root=[], python={sys.executable!r},
                    scheduler={address!r}, cpus=1, gpu=False)
slurm.supervise(a, slurm.inventory(), [], lambda: (a.directory/'stop').exists())
'''
        supervisor = subprocess.Popen([sys.executable, '-c', code], stdout=logs, stderr=logs,
                                      start_new_session=True)
        with connect(address) as c:
            end = time.monotonic()+40
            while not c.run_on_scheduler(dispatch, 'status')['workers']:
                assert supervisor.poll() is None
                assert time.monotonic()<end
                time.sleep(.2)
            with PoolEndpoint(address) as ep:
                future = ep.submit(hang, str(tmp_path/'old-child'), needs=dict(memory=2**20, seconds=1, execution_timeout=1))
                with pytest.raises(ConnectionError, match='execution deadline exceeded'):
                    future.result(timeout=40)
                old_child = int((tmp_path/'old-child').read_text())
                assert ep.submit(healthy, old_child, needs=dict(memory=2**20, seconds=1)).result(timeout=30)
            status = c.run_on_scheduler(dispatch, 'status')
            assert any(t.get('reason') == 'execution deadline exceeded' and t['state']=='lost' for t in status['tasks'].values())
            assert json.loads((tmp_path/'launcher.json').read_text())['restarts'] >= 1
    finally:
        (tmp_path/'stop').touch()
        if supervisor:
            try:
                supervisor.wait(timeout=25)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait()
        scheduler.terminate()
        try:
            scheduler.wait(timeout=10)
        except subprocess.TimeoutExpired:
            scheduler.kill()
            scheduler.wait()
        logs.close()
        if sys.exc_info()[0]:
            print((tmp_path/'services.log').read_text()[-12000:])


def test_supervised_scheduler_restarts_on_same_endpoint(tmp_path):
    from ecarsi.pool.client import connect
    from ecarsi.pool.scheduler import dispatch
    sf = tmp_path/'scheduler.json'
    health = tmp_path/'service.json'
    with (tmp_path/'scheduler.log').open('w') as log:
        service = subprocess.Popen([sys.executable, '-m', 'ecarsi.service', '--state', str(health), '--',
                                    sys.executable, '-m', 'ecarsi.pool', 'scheduler', '--scheduler-file', str(sf),
                                    '--host', '127.0.0.1'], stdout=log, stderr=log, start_new_session=True)
        try:
            end = time.monotonic()+30
            while not sf.exists():
                assert time.monotonic()<end and service.poll() is None
                time.sleep(.1)
            address = json.loads(sf.read_text())['address']
            with connect(address) as c:
                epoch = c.run_on_scheduler(dispatch, 'status')['epoch']
            import signal
            first = json.loads(health.read_text())['child_pid']
            os.kill(first, signal.SIGKILL)
            end = time.monotonic()+30
            while True:
                assert time.monotonic()<end and service.poll() is None
                status = json.loads(health.read_text())
                if status.get('child_pid') not in (None, first) and sf.exists():
                    try:
                        with connect(address, timeout=1) as c:
                            new_epoch = c.run_on_scheduler(dispatch, 'status')['epoch']
                        break
                    except (OSError, TimeoutError):
                        pass
                time.sleep(.2)
            assert epoch != new_epoch
            assert json.loads(sf.read_text())['address'] == address
        finally:
            service.terminate()
            try:
                service.wait(timeout=20)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
