"""Shared memory reservations inside one user-owned Slurm allocation.

CPU locks still enforce ownership. This ledger prevents separate driver and
worker launchers each promising the same allocation memory to their workloads.
"""
import fcntl
import json
from pathlib import Path
import time

from ecarsi.run_state import write_json
from .slurm import group_alive, process_identity


def held(path):
    with Path(path).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def reserve(profile, key, owner_lock, *, role, replace=(), queue_directory=None, worker_directory=None,
            owned_cpu_locks=False):
    if not profile.get('job_id'):
        return  # local process tests do not own a Slurm allocation
    root = Path.home() / '.cache/ecarsi-pool' / profile['host']
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"budget-{profile['job_id']}.json"
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = json.loads(path.read_text()) if path.exists() else {}
        for name, old in list(entries.items()):
            # The new worker already holds these exclusive locks. They cannot
            # also be evidence that the retired driver is still alive.
            replacing_cpu_locks = (owned_cpu_locks and role == 'worker' and old['role'] == 'driver'
                                   and set(old['cpu_ids']) <= set(profile['cpu_ids']))
            if name == key:
                continue
            if name in replace:
                if role != 'driver' or old['role'] != 'driver' or not set(old['cpu_ids']) <= set(profile['cpu_ids']):
                    raise ValueError('budget handoff must cover the previous driver CPU slice')
                del entries[name]
            elif not held(old['owner_lock']) and (replacing_cpu_locks or
                  not any(held(root / f'cpu-{cpu}.lock') for cpu in old['cpu_ids'])):
                # A killed srun can leave a remote Slurm step; an exited worker
                # leader can leave native children. Free locks alone prove neither.
                if old.get('queue_directory'):
                    state = json.loads((Path(old['queue_directory'])/'status.json').read_text())
                    if not any(r.get('node') in old['nodes'] and
                               (r['state'] in {'assigned', 'running'} or r.get('reservation_held'))
                               for r in state['datasets']):
                        del entries[name]
                elif old.get('worker_directory'):
                    receipt = Path(old['worker_directory'])/'launcher.json'
                    if receipt.exists():
                        status = json.loads(receipt.read_text())
                        if status.get('state') == 'stopped' and status.get('updated_at', 0) >= old['observed_at']:
                            del entries[name]
                        elif status.get('identity') and status.get('child_identity') and status.get('updated_at', 0) >= old['observed_at']:
                            owner, child = status['identity'], status['child_identity']
                            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                            if (owner['boot_id'] != boot or
                                (process_identity(owner['pid']) != owner and
                                 process_identity(child['pid']) != child and not group_alive(child['pid']))):
                                del entries[name]
        requested = int(profile['memory'])
        if any(set(profile['cpu_ids']) & set(e['cpu_ids']) for name,e in entries.items() if name != key):
            raise ValueError('driver and worker CPU reservations overlap')
        if requested <= 0 or requested + sum(e['memory'] for name, e in entries.items() if name != key) > profile['allocation_memory']:
            raise ValueError('combined driver and worker budgets exceed Slurm allocation memory')
        entries[key] = dict(role=role, memory=requested, cpu_ids=profile['cpu_ids'],
                            owner_lock=str(owner_lock), observed_at=time.time(),
                            queue_directory=str(queue_directory) if queue_directory else None,
                            nodes=[profile.get('id'), *profile.get('predecessors', [])],
                            worker_directory=str(worker_directory) if worker_directory else None)
        write_json(path, entries)
