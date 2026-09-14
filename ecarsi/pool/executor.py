"""Isolated task processes inside a worker's existing Slurm allocation.

The host supervisor fences the entire worker group on timeout/disconnection,
including these children. No task starts a detached process group.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def runtimes():
    """Probe each configured interpreter, rather than trusting an image name."""
    commands = {'default': sys.executable}
    configured = Path(os.environ.get('ECA_POOL_RUNTIMES') or Path.home() / '.config/ecarsi/pool-runtimes.json')
    if configured.exists() or os.environ.get('ECA_POOL_RUNTIMES'):
        commands = json.loads(configured.read_text())
    if not isinstance(commands, dict) or not commands:
        raise ValueError('runtime configuration must map names to Python executables')
    profiles = {}
    for name, python in commands.items():
        if not isinstance(name, str) or not isinstance(python, str) or not Path(python).is_absolute():
            raise ValueError('runtime executable paths must be absolute')
        probe = subprocess.run([python, '-P', '-c',
            'import json; from ecarsi.pool.client import runtime; print(json.dumps(runtime()))'],
            check=True, capture_output=True, text=True, timeout=60)
        profiles[name] = json.loads(probe.stdout)
    return commands, profiles


def dask_setup(worker):
    # Older pinned drivers pickle this public entry by reference. Keep their
    # wire entry while replacing only the worker execution service, not kernels.
    from ecarsi.pool import client
    client.execute = execute


def execute(grant, fn, args, kwargs):
    import cloudpickle
    from distributed import get_client, get_worker
    from ecarsi.pool.scheduler import dispatch

    if grant.get('execution_protocol') != 2:
        raise RuntimeError('isolated worker needs a protocol 2 grant')
    worker, client = get_worker(), get_client()
    commands = json.loads(os.environ['ECA_POOL_EXECUTORS'])
    python = commands[grant['runtime_id']]
    token = dict(id=grant['id'], epoch=grant['epoch'], worker=worker.address)
    client.run_on_scheduler(dispatch, 'claim', token)
    ok, peak = False, None
    try:
        with tempfile.TemporaryDirectory(prefix='pool-task-', dir=worker.local_directory) as folder:
            root = Path(folder)
            (root/'grant.json').write_text(json.dumps(grant))
            (root/'input.pkl').write_bytes(cloudpickle.dumps((grant, fn, args, kwargs)))
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=','.join(grant['gpu_ids']),
                       MSP_COMPUTE_ENDPOINT='local', OSP_COMPUTE_ENDPOINT='local')
            # A worker must never borrow the submitting driver's memory lease.
            for name in ('ECA_DRIVER_LEASE_DIRECTORY', 'ECA_DRIVER_BUDGET_MODULE'):
                env.pop(name, None)
            for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS', 'MSP_MAX_THREADS'):
                env[name] = str(grant['cpus'])
            # Inherit the host-fenced worker process group. stdout/stderr remain
            # in the worker log, with task identity at both boundaries.
            print(f"[pool task {grant['id']}] start {grant.get('label')} runtime={grant['runtime_id']} cpus={grant['cpu_ids']}", flush=True)
            proc = subprocess.Popen([python, '-P', '-m', __name__, str(root)], env=env)
            rc = proc.wait()
            if rc:
                raise RuntimeError(f"pool task subprocess exited {rc}: {grant.get('label')}")
            with (root/'result.pkl').open('rb') as stream:
                result = cloudpickle.load(stream)
            peak = result['peak_rss']
            if not result['ok']:
                raise result['error']
            ok = True
            return result['value']
    finally:
        print(f"[pool task {grant['id']}] {'completed' if ok else 'failed'}", flush=True)
        ack = client.run_on_scheduler(dispatch, 'finish', dict(token, ok=ok, peak_rss=peak))
        if ack['state'] not in {'done', 'failed'}:
            raise ConnectionError(f"pool execution retired: {ack.get('reason', '')}")


def main():
    import resource
    import traceback
    import cloudpickle
    from ecarsi.pool.client import runtime

    root = Path(sys.argv[1])
    # Read the grant before importing numerical modules from the payload.
    grant = json.loads((root/'grant.json').read_text())
    if not set(grant['cpu_ids']) <= os.sched_getaffinity(0):
        raise ValueError('task CPUs are outside the worker allocation')
    os.sched_setaffinity(0, grant['cpu_ids'])
    if any(runtime().get(k) != v for k,v in grant['runtime'].items()):
        raise RuntimeError('task runtime differs from its grant; refusing computation')
    with (root/'input.pkl').open('rb') as stream:
        _, fn, args, kwargs = cloudpickle.load(stream)
    try:
        result = dict(ok=True, value=fn(*args, **kwargs))
    except Exception as exc:
        traceback.print_exc()
        exc.add_note('Worker subprocess traceback:\n'+traceback.format_exc())
        result = dict(ok=False, error=exc)
    result['peak_rss'] = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                             resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)*1024
    with (root/'result.pkl').open('wb') as stream:
        cloudpickle.dump(result, stream)


if __name__ == '__main__':
    main()
