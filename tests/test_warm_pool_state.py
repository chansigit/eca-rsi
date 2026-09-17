import time
import socket

import pytest

from ecarsi.warm_pool.backend import check_runtime
from ecarsi.warm_pool.worker import registered_worker_id
from ecarsi.warm_pool.state import cancel, file_digest, read, save, status, submit, validate_trace


def test_recovery_cache_skips_terminal_history_but_rechecks_retry_and_live_owner(tmp_path, monkeypatch):
    import os
    from ecarsi.warm_pool import worker
    from ecarsi.warm_pool.state import retry
    tmp_path.chmod(0o700)
    (tmp_path / 'requests').mkdir()
    save(tmp_path / 'config.json', dict(runtime=dict(version='test')))
    cpu = min(os.sched_getaffinity(0))
    requests = {}
    for name in ('finished', 'live'):
        submit(tmp_path, dict(request_id=name, operation_id='compute', args=['-c', 'pass'],
                             cpus=1, memory_mb=64, timeout_seconds=10, outputs=['result.json']))
        requests[name] = read(tmp_path / 'requests' / name / 'request.json')
    old = requests['finished']
    folder = tmp_path / 'requests/finished'
    save(folder / old['attempt_id'] / 'receipt.json', dict(state='failed', finished_at=1,
        attempt_id=old['attempt_id'], request_digest=old['digest'], runtime_digest=old['runtime_digest']))
    live = tmp_path / 'requests/live' / requests['live']['attempt_id']
    accepted = dict(host=socket.gethostname().split('.')[0], cpu_ids=[cpu],
                    identity=worker.identity(os.getpid()), started_at=time.time())
    save(live / 'accepted.json', accepted)
    assert worker.reconcile_local(tmp_path, [cpu]) == ['live']
    original = worker.read
    touched = []
    def track(path, *args):
        touched.append(path)
        return original(path, *args)
    monkeypatch.setattr(worker, 'read', track)
    assert worker.reconcile_local(tmp_path, [cpu]) == ['live']
    assert not any(p.is_relative_to(folder) for p in touched)
    retry(tmp_path, 'finished', reason='confirmed failure')
    current = read(folder / 'request.json')
    save(folder / current['attempt_id'] / 'accepted.json', accepted)
    worker.mark_active(tmp_path, 'finished')  # run() marks every acceptance on this host
    assert set(worker.reconcile_local(tmp_path, [cpu])) == {'live', 'finished'}


def test_recovery_cache_keeps_live_orphan_resources_reserved(tmp_path):
    import os
    import subprocess
    import sys
    from ecarsi.warm_pool import worker
    tmp_path.chmod(0o700)
    (tmp_path / 'requests').mkdir()
    runtime = dict(command=[sys.executable], version='Python ' + sys.version.split()[0], files={})
    save(tmp_path / 'config.json', dict(runtime=runtime))
    cpu = min(os.sched_getaffinity(0))
    def launch(name, code):
        submit(tmp_path, dict(request_id=name, operation_id='test', args=['-c', code],
            cpus=1, memory_mb=128, timeout_seconds=30, outputs=['result.json']))
        request = read(tmp_path / 'requests' / name / 'request.json')
        proc = subprocess.Popen([sys.executable, '-m', 'ecarsi.warm_pool.worker', 'execute',
            str(tmp_path), name, request['attempt_id']], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: os.sched_setaffinity(0, {cpu}))
        return proc, tmp_path / 'requests' / name / request['attempt_id']
    completed, folder = launch('completed', "from pathlib import Path;Path('result.json').write_text('{}')")
    assert completed.wait(timeout=10) == 0
    assert worker.reconcile_local(tmp_path, [cpu]) == []
    assert read(folder / 'receipt.json')['cpu_seconds'] >= 0  # integrated group CPU time, for CPU right-sizing
    phases = read(folder / 'receipt.json')['preflight_seconds']
    assert set(phases) == {'local_recovery', 'runtime_validation', 'input_validation'}
    assert all(seconds >= 0 for seconds in phases.values())
    child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    code = ("import subprocess,sys,time;from pathlib import Path;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            "Path('ready').touch();time.sleep(60)")
    proc, attempt = launch('orphan', code)
    accepted = None
    try:
        deadline = time.monotonic() + 10
        while not (attempt / 'outputs/ready').exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        accepted = read(attempt / 'accepted.json')
        proc.kill()
        proc.wait(timeout=5)
        assert worker.reconcile_local(tmp_path, [cpu]) == ['orphan']
        assert read(attempt / 'receipt.json') is None
        worker.stop_group(accepted['pgid'])
        assert worker.reconcile_local(tmp_path, [cpu]) == []
        receipt = read(attempt / 'receipt.json')
        assert receipt['state'] == 'failed' and receipt['retryable']
    finally:
        if accepted:
            worker.stop_group(accepted['pgid'])
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_dispatch_cache_revisits_retries_cancellation_and_replayed_jobs(tmp_path, monkeypatch):
    from ecarsi.warm_pool.backend import HyperQueue
    from ecarsi.warm_pool.state import retry
    import ecarsi.warm_pool.backend as module
    tmp_path.chmod(0o700)
    (tmp_path / 'requests').mkdir()
    save(tmp_path / 'config.json', {'hq': '/test/hq', 'executor': '/test/python', 'runtime': {'version': 'test'}})
    submit(tmp_path, dict(request_id='r', operation_id='compute', args=['-c', 'pass'],
                         cpus=1, memory_mb=64, timeout_seconds=10, outputs=['result.json']))
    folder = tmp_path / 'requests/r'
    request = read(folder / 'request.json')
    def finish(request):
        save(folder / request['attempt_id'] / 'receipt.json', dict(state='failed', finished_at=1,
            attempt_id=request['attempt_id'], request_digest=request['digest'], runtime_digest=request['runtime_digest']))
    finish(request)
    backend, calls = HyperQueue(tmp_path), []
    def call(*args):
        calls.append(args)
        if args[:2] == ('job', 'list'):
            return [dict(name='rsi.r.' + request['attempt_id'], id=1, task_stats=dict(waiting=1, running=0))]
        return {'id': 2}
    monkeypatch.setattr(backend, 'call', call)
    info = dict(server_uid='one', pid=1, start_date='one')
    backend.dispatch(info)
    assert ('job', 'cancel', '1') in calls
    calls.clear()
    # Hot ticks need no reads or locks for immutable terminal attempts.
    with monkeypatch.context() as check:
        check.setattr(module, 'read', lambda *args: pytest.fail('terminal history reread'))
        backend.dispatch(info)
    assert calls == [('job', 'list', '--all')]
    calls.clear()
    backend.dispatch({**info, 'server_uid': 'two'})
    assert ('job', 'cancel', '1') in calls
    retry(tmp_path, 'r', reason='confirmed local failure')
    calls.clear()
    backend.dispatch(info)
    assert any(args[0] == 'submit' for args in calls)
    finish(read(folder / 'request.json'))
    backend.dispatch(info)
    cancel(tmp_path, 'r')
    backend.dispatch(info)
    assert read(folder / 'backend.json')['state'] == 'cancelled'


def test_retry_preserves_receipts_and_pins_runtime_until_explicit_upgrade(tmp_path):
    from ecarsi.warm_pool.state import retry
    tmp_path.chmod(0o700);(tmp_path/'requests').mkdir()
    save(tmp_path/'config.json',{'runtime':{'version':'old'}})
    source=tmp_path/'source';source.write_text('unchanged')
    spec=dict(request_id='r',operation_id='compute',args=['-c','pass'],cpus=1,memory_mb=64,
              timeout_seconds=10,outputs=['result.json'],inputs=[dict(path=str(source),sha256=file_digest(source))])
    submit(tmp_path,spec);folder=tmp_path/'requests/r'
    with pytest.raises(ValueError,match='confirmed failed'):retry(tmp_path,'r',reason='no terminal receipt')
    original=read(folder/'request.json')
    def failed(request):
        receipt=dict(state='failed',finished_at=1,attempt_id=request['attempt_id'],
                     request_digest=request['digest'],runtime_digest=request['runtime_digest'])
        save(folder/request['attempt_id']/'receipt.json',receipt)
        return receipt
    receipt=failed(original);save(tmp_path/'config.json',{'runtime':{'version':'new'}})
    retry(tmp_path,'r',reason='known local failure')
    repeated=read(folder/'request.json')
    assert repeated['runtime']==original['runtime'] and repeated['attempt_id']!=original['attempt_id']
    assert read(folder/original['attempt_id']/'receipt.json')==receipt
    assert read(folder/original['attempt_id']/'request.json')==original
    assert status(tmp_path,'r')['state']=='queued'
    with pytest.raises(ValueError,match='confirmed failed'):retry(tmp_path,'r',reason='double retry')
    failed(repeated);retry(tmp_path,'r',reason='validated runtime fix',use_current_runtime=True)
    upgraded=read(folder/'request.json');assert upgraded['runtime']['version']=='new'
    failed(upgraded);source.write_text('different')
    with pytest.raises(ValueError,match='input changed'):retry(tmp_path,'r',reason='cannot reuse changed input')


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


def test_dispatch_submits_concurrently_and_resubmits_a_failed_submission(tmp_path, monkeypatch):
    from ecarsi.warm_pool.backend import HyperQueue
    tmp_path.chmod(0o700)
    (tmp_path / 'requests').mkdir()
    save(tmp_path / 'config.json', {'hq': '/test/hq', 'executor': '/test/python', 'runtime': {'version': 'test'}})
    for name in ('a', 'b'):
        submit(tmp_path, dict(request_id=name, operation_id='compute', args=['-c', 'pass'],
                             cpus=1, memory_mb=64, timeout_seconds=10, outputs=['result.json']))
    backend, calls, broken, known = HyperQueue(tmp_path), [], {'a'}, []
    def call(*args):
        calls.append(args)
        if args[:2] == ('job', 'list'):
            return [dict(name=name, id=7, task_stats=dict(waiting=1, running=0)) for name in known]
        if args[0] == 'submit':
            name = args[args.index('--name') + 1]
            if name.split('.')[1] in broken:
                raise RuntimeError('hq client lost the server')
            known.append(name)
            return {'id': 7}
        return None
    monkeypatch.setattr(backend, 'call', call)
    info = dict(server_uid='one', pid=1, start_date='one')
    backend.dispatch(info)
    assert calls.count(('journal', 'flush')) == 1  # one flush per tick, not per submission
    assert read(tmp_path / 'requests/a/backend.json')['state'] == 'submit_failed'
    assert read(tmp_path / 'requests/b/backend.json') == dict(state='queued', job_id=7, generation=read(tmp_path / 'requests/b/backend.json')['generation'], observed_at=read(tmp_path / 'requests/b/backend.json')['observed_at'])
    broken.clear()
    calls.clear()
    backend.dispatch(info)
    assert sum(args[0] == 'submit' for args in calls) == 1  # only the failed one is submitted again
    assert read(tmp_path / 'requests/a/backend.json')['state'] == 'queued'
    # An unchanged live job is not rewritten every tick (each save is an fsync on shared storage).
    before = (tmp_path / 'requests/b/backend.json').stat().st_mtime_ns
    backend.dispatch(info)
    assert (tmp_path / 'requests/b/backend.json').stat().st_mtime_ns == before
    # A succeeded request is settled: later ticks do not even stat it.
    for name in ('a', 'b'):
        request = read(tmp_path / f'requests/{name}/request.json')
        save(tmp_path / f'requests/{name}' / request['attempt_id'] / 'receipt.json', dict(state='succeeded', finished_at=1,
             attempt_id=request['attempt_id'], request_digest=request['digest'], runtime_digest=request['runtime_digest']))
    backend.dispatch(info)
    assert backend.settled == {'a', 'b'}
    with monkeypatch.context() as check:
        check.setattr(module_path := __import__('pathlib').Path, 'stat', lambda self, *a, **k: pytest.fail(f'settled folder stat: {self}'))
        backend.dispatch(info)
