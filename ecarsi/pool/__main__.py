"""Start/inspect pool processes. Slurm allocations always belong to the user."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path


async def serve(a):
    from distributed import Scheduler

    from .scheduler import PoolScheduler
    async with Scheduler(host=a.host, port=a.port, scheduler_file=a.scheduler_file,
                         dashboard_address=None, worker_ttl="60s", plugins=[PoolScheduler()]) as scheduler:
        from ecarsi.run_state import write_json
        write_json(Path(a.scheduler_file).with_suffix(".endpoint.json"), {"address": scheduler.address})
        await scheduler.finished()


async def worker(a):
    from distributed import Nanny

    from .client import connect, runtime
    # Keep the wire callable importable by an older scheduler during a
    # rolling service upgrade; its extension dispatch contract is stable.
    from ecarsi.pool.scheduler import dispatch
    path = Path(a.inventory)
    profile = json.loads(path.read_text())
    if set(profile["cpu_ids"]) != set(os.sched_getaffinity(0)):
        raise ValueError("worker affinity differs from host Slurm inventory")
    if time.time() - profile["observed_at"] > 90:
        raise ValueError("stale Slurm inventory")
    target = {"scheduler_ip": a.scheduler} if "://" in a.scheduler else {"scheduler_file": a.scheduler}
    from .executor import runtimes
    commands, fingerprints = runtimes()
    os.environ['ECA_POOL_EXECUTORS'] = json.dumps(commands)
    slots = int(os.environ.get('ECA_POOL_TASK_SLOTS', str(profile['cpus'])))
    if not 1 <= slots <= profile['cpus']:
        raise ValueError('task slots must fit the worker CPU allocation')
    async with Nanny(**target, nthreads=slots, memory_limit=profile["memory"],
                     preload=[__package__+'.executor'],
                     local_directory=str(path.parent / "dask"), resources={"pool_slot": slots},
                     death_timeout=30) as nanny:
        async with connect(a.scheduler, asynchronous=True) as client:
            fingerprint = runtime()
            while nanny.status.name == "running":
                import psutil
                process = psutil.Process()
                rss = 0
                for child in [process, *process.children(recursive=True)]:
                    try:
                        rss += child.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                profile = json.loads(path.read_text())
                profile["runtime"] = fingerprint
                profile.update(execution_protocol=2, task_slots=slots, runtimes=fingerprints,
                               rss_bytes=rss, usage_observed_at=time.time())
                if rss > profile['memory']:
                    profile['recycle_reason'] = 'worker process tree exceeded its memory budget'
                    print('[pool] '+profile['recycle_reason'], flush=True)
                if nanny.worker_address and nanny.worker_address in (await client.scheduler.identity())["workers"]:
                    status = await client.run_on_scheduler(dispatch, "register",
                                                           {"address": nanny.worker_address, "profile": profile})
                    if status["workers"][nanny.worker_address].get("recycle_reason"):
                        # Only the host launcher may replace this worker, after
                        # killing/reaping the entire old container process group.
                        # Nanny.restart alone can leave native/R children alive.
                        os._exit(75)
                if time.time() >= profile["end_time"]:
                    return
                await asyncio.sleep(2)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scheduler")
    s.add_argument("--scheduler-file", required=True)
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=0)
    w = sub.add_parser("worker", help="internal: use the host launcher ecarsi.pool.slurm")
    w.add_argument("--scheduler", required=True)
    w.add_argument("--inventory", required=True)
    for name in ("status", "drain"):
        q = sub.add_parser(name)
        q.add_argument("--scheduler", default=None)
        if name == "drain":
            q.add_argument("address", help="worker address shown by status; finishes active task")
        else:
            q.add_argument("--json", action="store_true", help="machine-readable resource and utilization snapshot")
    a = p.parse_args(argv)
    if a.command == "scheduler":
        from ecarsi.run_state import writer_lock
        from urllib.parse import urlparse
        path = Path(a.scheduler_file)
        with writer_lock(path.with_suffix(".lock")):
            # Preserve the endpoint across a supervised restart. Kernel bind
            # ownership still rejects a live server on the same address.
            endpoint = path.with_suffix(".endpoint.json")
            old = endpoint if endpoint.exists() else path
            if old.exists():
                address = urlparse(json.loads(old.read_text())["address"])
                a.host = a.host or address.hostname
                a.port = a.port or address.port
            elif not a.host or not a.port:
                # Initial ephemeral binding is recorded from the created file.
                pass
            asyncio.run(serve(a))
    elif a.command == "worker":
        asyncio.run(worker(a))
    else:
        from .client import connect
        from .scheduler import dispatch
        with connect(a.scheduler) as c:
            if a.command == "status":
                from .status import render, snapshot
                state = snapshot(c)
                print(json.dumps(state, indent=2) if a.json else render(state))
            else:
                print(json.dumps(c.run_on_scheduler(dispatch, "drain", {"address": a.address}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
