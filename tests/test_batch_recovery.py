"""Real process/lock recovery, with isolated local commands and no Slurm jobs."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from ecarsi.batch import process_identity, queue, receipt_path, submit


def wait_for(check, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError("condition did not become true before timeout")


@pytest.mark.parametrize('cpu_count', [1, 2])
def test_agent_restart_adopts_driver_and_uses_free_cpu(tmp_path, cpu_count):
    cpus = sorted(os.sched_getaffinity(0))[:cpu_count]
    if len(cpus) < cpu_count:
        pytest.skip("needs two permitted CPUs")
    inp = tmp_path / "input"
    inp.mkdir()
    runner = tmp_path / "fake-python"
    runner.write_text(f'''#!{sys.executable}
import pathlib, sys, time, os
root = pathlib.Path({str(tmp_path)!r})
name = pathlib.Path(sys.argv[6]).name
(root / (name + '-started')).write_text(str(os.getpid()))
while name == 'first' and not (root / 'finish').exists():
    time.sleep(.1)
''')
    runner.chmod(0o700)
    c = dict(directory=str(tmp_path / "queue"), python=str(runner), scheduler="unused",
             env={}, max_cpu_percent=90, max_attempts=2, driver_cpu_slots_per_core=2)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(c))
    first = submit(c, [dict(input=str(inp), output=str(tmp_path / "first"), cpus=1, memory_gb=1, hours=1)])[0]
    profile = dict(host="isolated", job_id=None, cpu_ids=cpus, cpus=cpu_count, memory=2*2**30,
                   memory_headroom=2*2**30, end_time=time.time()+7200, allocated_tres="cpu=2")
    # A previous admission process died after claiming an oversized launch but
    # before creating its supervisor. Recovery must precede budget validation.
    orphan = submit(c, [dict(input=str(inp), output=str(tmp_path / "never-started"), cpus=1, memory_gb=3, hours=1)])[0]
    identity = "isolated/None/" + ",".join(map(str, cpus))
    with queue(c['directory']) as initial:
        initial['nodes'][identity] = dict(profile, id=identity, observed_at=time.time())
        claimed = next(r for r in initial['datasets'] if r['id'] == orphan['id'])
        claimed.update(state='assigned', node=identity, cpu_ids=cpus[:1], attempt=1,
                       supervisor_protocol=1, launch_requested=time.time(),
                       launch_owner=dict(pid=987654321, start_ticks=0, boot_id='dead'), execution=c)
    code = f'''
import time
from pathlib import Path
import ecarsi.batch as b
import ecarsi.pool.slurm as s
Path.home = staticmethod(lambda: Path({str(tmp_path / 'home')!r}))
s.inventory = lambda *_: dict({profile!r}, observed_at=time.time()-45)
b.memory_headroom = lambda: 4*2**30
b.node_agent(Path({str(config)!r}), 2)
'''
    def state():
        return json.loads((tmp_path / "queue/status.json").read_text())
    def start_agent():
        return subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    old = start_agent()
    new = None
    try:
        wait_for(lambda: (tmp_path / "first-started").exists())
        offer = state()['nodes'][identity]
        assert offer['observed_at'] - offer['inventory_observed_at'] >= 45
        orphan_row = next(r for r in state()['datasets'] if r['id'] == orphan['id'])
        assert orphan_row['state'] == 'retry_wait' and orphan_row['unstarted_attempts'] == 1
        original_driver = int((tmp_path / "first-started").read_text())
        original_identity = process_identity(original_driver)
        old.kill()
        old.wait(timeout=5)
        second = submit(c, [dict(input=str(inp), output=str(tmp_path / "second"), cpus=1, memory_gb=1, hours=1)])[0]
        new = start_agent()
        wait_for(lambda: (tmp_path / "second-started").exists())
        assert process_identity(original_driver) == original_identity
        first_row = next(r for r in state()["datasets"] if r["id"] == first["id"])
        assert first_row["state"] == "running" and first_row["attempt"] == 1
        assert not first_row.get("reservation_held")
        # Now lose only the supervisor: the new agent must terminate the orphan
        # child and retire the attempt before it can enter retry backoff.
        os.kill(first_row["pid"], signal.SIGKILL)
        def retired():
            row = next(r for r in state()["datasets"] if r["id"] == first["id"])
            return row if row["state"] == "retry_wait" else None
        recovered = wait_for(retired)
        assert not process_identity(original_driver)
        assert not recovered.get("reservation_held")
        receipt = json.loads(receipt_path(c["directory"], dict(first, attempt=1)).read_text())
        assert receipt["termination_reason"] == "execution_lost" and receipt["finished_at"]
        wait_for(lambda: next(r for r in state()["datasets"] if r["id"] == second["id"])["state"] == "completed")
    finally:
        (tmp_path / "finish").touch()
        for agent in (old, new):
            if agent and agent.poll() is None:
                agent.terminate()
                try:
                    agent.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    agent.kill()
                    agent.wait()
            if agent and agent.stderr:
                errors = agent.stderr.read().decode()
                if errors:
                    print(errors)


def test_named_slurm_step_query_never_signals_allocation(monkeypatch):
    import ecarsi.batch as b
    from types import SimpleNamespace
    row = dict(id='abc', attempt=2)
    def query(cmd, **kw):
        assert cmd == ['scontrol', '-o', 'show', 'step', '42']
        return SimpleNamespace(stdout='StepId=42.3 State=RUNNING Name=rsi-abc-2\n'
                               'StepId=42.extern Name=extern\nStepId=42.4 Name=unrelated\n'
                               'StepId=42.TBD Name=unrelated-pending\nStepId=42+1.0 Name=\n')
    monkeypatch.setattr(b.subprocess, 'run', query)
    assert b.attempt_steps(row, dict(job_id=42)) == ['42.3']
    monkeypatch.setattr(b.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='StepId=42 Name=rsi-abc-2\n'))
    with pytest.raises(ValueError, match='allocation'):
        b.attempt_steps(row, dict(job_id=42))


def test_missing_step_query_needs_terminal_accounting_and_errors_stay_errors(monkeypatch):
    import ecarsi.batch as b
    def missing(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr='Invalid job id specified')
    monkeypatch.setattr(b.subprocess, 'run', missing)
    with pytest.raises(subprocess.CalledProcessError):
        b.allocation_steps(42)
    assert b.allocation_steps(42, allow_missing=True) == {}
    def offline(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr='Unable to contact slurm controller')
    monkeypatch.setattr(b.subprocess, 'run', offline)
    with pytest.raises(subprocess.CalledProcessError):
        b.allocation_steps(42, allow_missing=True)


def test_recovery_query_errors_hold_budget_and_signal_race_rechecks_absence(tmp_path, monkeypatch):
    import ecarsi.batch as b
    def unavailable(*args):
        raise subprocess.TimeoutExpired('scontrol', 15)
    monkeypatch.setattr(b, '_recover_execution', unavailable)
    row={}
    b.recover_execution(row, {}, tmp_path)
    assert row['reservation_held'] and 'unavailable' in row['recovery_status']
    def gone(cmd, **kwargs):
        raise subprocess.CalledProcessError(1,cmd,stderr='step already finished')
    monkeypatch.setattr(b.subprocess, 'run', gone)
    monkeypatch.setattr(b, 'allocation_steps', lambda _: {})
    b.signal_step(42,'42.3','SIGTERM')
    monkeypatch.setattr(b, 'allocation_steps', lambda _: {'42.3': 'still alive'})
    with pytest.raises(subprocess.CalledProcessError):
        b.signal_step(42,'42.3','SIGTERM')


def test_ended_allocation_requires_accounting_steps_and_writer_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ecarsi.batch as b
    c = dict(directory=str(tmp_path/'q'), max_attempts=2)
    row = dict(id='old', attempt=1, state='running', node='old-node', launch_requested=1,
               output=str(tmp_path/'out'), cpu_ids=[0], memory_gb=1)
    with b.queue(c['directory']) as s:
        s['nodes']['old-node'] = dict(job_id=42, observed_at=0, end_time=1)
        s['nodes']['unavailable'] = dict(job_id=43, observed_at=0, end_time=1)
        s['datasets'] = [row, dict(row, id='unavailable', node='unavailable', output=str(tmp_path/'other'))]
    path = b.receipt_path(c['directory'], row)
    path.parent.mkdir()
    path.write_text(json.dumps(dict(node='old-node', state='running')))
    accounting, steps = '', ''
    def query(cmd, **kwargs):
        if cmd[0]=='scontrol' and cmd[-1]=='43':
            raise subprocess.TimeoutExpired(cmd, 15)
        return SimpleNamespace(stdout=accounting if cmd[0] == 'sacct' else steps)
    monkeypatch.setattr(b.subprocess, 'run', query)
    b.recover_ended_allocations(c['directory'])
    assert 'finished_at' not in json.loads(path.read_text())  # absence is not death
    accounting, steps = '42|TIMEOUT|\n43|TIMEOUT|\n', 'StepId=42.7 Name=unrelated\n'
    b.recover_ended_allocations(c['directory'])
    assert 'finished_at' not in json.loads(path.read_text())
    steps = ''
    with b.writer_lock(tmp_path/'out/units/u/.rsi-downstream.lock'):
        b.recover_ended_allocations(c['directory'])
        assert 'finished_at' not in json.loads(path.read_text())
    b.recover_ended_allocations(c['directory'])
    assert json.loads(path.read_text())['termination_reason'] == 'allocation_end'
    with b.queue(c['directory']) as s:
        b.reconcile(s, c['directory'])
        b.retry_finished(s, c)
        assert s['datasets'][0]['state'] == 'retry_wait'
        assert s['datasets'][1]['state'] == 'running'


def test_scanned_launcher_disappearing_does_not_authorize_group_signal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ecarsi.batch as b
    row=dict(id='scan',attempt=1,node='n',state='running',supervisor_protocol=1,output=str(tmp_path/'out'))
    proc=SimpleNamespace(parent=SimpleNamespace(name='987654321'),
                         read_bytes=lambda: ('--job-name='+b.step_name(row)).encode()+b'\0')
    monkeypatch.setattr(b.Path,'glob',lambda self,pattern: [proc] if pattern=='[0-9]*/cmdline' else [])
    identities=iter([dict(pid=987654321,start_ticks=1,boot_id='test'),None])
    monkeypatch.setattr(b,'process_identity',lambda _: next(identities))
    monkeypatch.setattr(b,'allocation_steps',lambda _: {})
    signals=[]
    monkeypatch.setattr(b.os,'killpg',lambda *args: signals.append(args))
    b.recover_execution(row,dict(job_id=42),tmp_path)
    assert not signals and row['reservation_held']


def test_supervisor_start_grace_does_not_publish_false_terminal_receipt(tmp_path):
    import time
    import ecarsi.batch as b
    row = dict(id='slow-launch', attempt=1, node='n', state='assigned',
               supervisor_protocol=1, launch_requested=time.time())
    b.recover_execution(row, dict(job_id=42), tmp_path)
    assert row['reservation_held']
    assert row['recovery_status'] == 'Waiting for supervisor start'
    assert not b.receipt_path(tmp_path, row).exists()


def test_receipt_snapshot_does_not_update_reassigned_attempt(tmp_path):
    import ecarsi.batch as b
    old = dict(id='reassigned', attempt=1, node='old', state='running')
    path = b.receipt_path(tmp_path, old)
    path.parent.mkdir()
    path.write_text(json.dumps(dict(node='old', state='completed', finished_at=1)))
    observations = b.receipt_observations(dict(datasets=[old]), tmp_path)
    current = dict(id='reassigned', attempt=2, node='new', state='running')
    b.reconcile(dict(datasets=[current]), tmp_path, observations)
    assert current['state'] == 'running' and current['attempt'] == 2


def test_unstarted_attempt_resumes_after_earlier_scientific_failure(tmp_path):
    import ecarsi.batch as b
    row = dict(id='old-failure', attempt=3, node='n', state='paused',
               output=str(tmp_path/'run'))
    sample = tmp_path/'run/units/u/persample/s'
    sample.mkdir(parents=True)
    (sample/'run_state.json').write_text(json.dumps(dict(state='failed', retryable=False)))
    receipt = b.receipt_path(tmp_path, row)
    receipt.parent.mkdir()
    receipt.write_text(json.dumps(dict(node='n', state='paused', finished_at=1,
                                       termination_reason='launch_interrupted')))
    b.retry_finished(dict(datasets=[row]), dict(directory=str(tmp_path), max_attempts=2), now=100)
    assert row['state'] == 'retry_wait' and row['unstarted_attempts'] == 1


def test_combined_worker_driver_memory_budget(tmp_path, monkeypatch):
    import fcntl
    from ecarsi.pool.budget import reserve
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))
    root = tmp_path/'.cache/ecarsi-pool/test'
    root.mkdir(parents=True)
    with (root/'cpu-0.lock').open('a') as worker, (root/'node.lock').open('a') as driver:
        fcntl.flock(worker, fcntl.LOCK_EX)
        fcntl.flock(driver, fcntl.LOCK_EX)
        p = dict(host='test', job_id=1, memory=6, allocation_memory=10, cpu_ids=[0])
        reserve(p, 'worker', root/'cpu-0.lock', role='worker')
        with pytest.raises(ValueError, match='combined'):
            reserve(dict(p, memory=5, cpu_ids=[1]), 'driver', root/'node.lock', role='driver')
        reserve(dict(p, memory=4, cpu_ids=[1]), 'driver', root/'node.lock', role='driver')
        # Merge only the driver reservation, without charging its predecessor twice.
        reserve(dict(p, memory=4, cpu_ids=[1, 2]), 'merged', root/'node.lock', role='driver', replace=['driver'])
        entries = json.loads((root/'budget-1.json').read_text())
        assert sum(e['memory'] for e in entries.values()) == 10 and 'driver' not in entries


def test_legacy_orphan_retires_only_after_enrolled_process_exits(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ecarsi.batch as b
    child = subprocess.Popen(['sleep', '60'], start_new_session=True)
    row = dict(id='legacy', attempt=1, node='old', state='running', output=str(tmp_path/'out'))
    path = receipt_path(tmp_path, row)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(node='old', state='running', pid=os.getpid(), child_pid=child.pid)))
    snapshot = dict(supervisor=process_identity(os.getpid()), child=process_identity(child.pid), steps=['42.3'])
    path.with_suffix('.processes.json').write_text(json.dumps(snapshot))
    monkeypatch.setattr(b.subprocess, 'run', lambda cmd, **kw: SimpleNamespace(stdout='StepId=42.9 Name=unrelated\n'))
    try:
        b.recover_execution(row, dict(job_id=42), tmp_path)
        assert row['reservation_held']
        child.wait(timeout=5)
        b.recover_execution(row, dict(job_id=42), tmp_path)
        assert json.loads(path.read_text())['termination_reason'] == 'execution_lost'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_crashed_worker_budget_retires_after_entire_group_is_gone(tmp_path, monkeypatch):
    import ecarsi.pool.budget as b
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))
    root = tmp_path/'.cache/ecarsi-pool/test'
    root.mkdir(parents=True)
    worker = tmp_path/'worker'
    worker.mkdir()
    profile = dict(host='test', job_id=1, memory=6, allocation_memory=10, cpu_ids=[0])
    b.reserve(profile, 'old', root/'cpu-0.lock', role='worker', worker_directory=worker)
    child = subprocess.Popen(['sleep', '60'], start_new_session=True)
    birth = process_identity(child.pid)
    (worker/'launcher.json').write_text(json.dumps(dict(state='running', identity=birth,
        child_identity=birth, updated_at=time.time())))
    try:
        with pytest.raises(ValueError, match='combined'):
            b.reserve(dict(profile, cpu_ids=[1]), 'new', root/'cpu-1.lock', role='worker')
        child.kill()
        child.wait()
        b.reserve(dict(profile, cpu_ids=[1]), 'new', root/'cpu-1.lock', role='worker')
        assert set(json.loads((root/'budget-1.json').read_text())) == {'new'}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
