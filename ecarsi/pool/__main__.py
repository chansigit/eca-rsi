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
        await scheduler.finished()


async def worker(a):
    from distributed import Nanny

    from .client import connect, runtime
    from .scheduler import dispatch
    path = Path(a.inventory)
    profile = json.loads(path.read_text())
    if set(profile["cpu_ids"]) != set(os.sched_getaffinity(0)):
        raise ValueError("worker affinity differs from host Slurm inventory")
    if time.time() - profile["observed_at"] > 90:
        raise ValueError("stale Slurm inventory")
    target = {"scheduler_ip": a.scheduler} if "://" in a.scheduler else {"scheduler_file": a.scheduler}
    async with Nanny(**target, nthreads=1, memory_limit=profile["memory"],
                     local_directory=str(path.parent / "dask"), resources={"pool_slot": 1},
                     death_timeout=30) as nanny:
        async with connect(a.scheduler, asynchronous=True) as client:
            fingerprint = runtime()
            while nanny.status.name == "running":
                profile = json.loads(path.read_text())
                profile["runtime"] = fingerprint
                if nanny.worker_address and nanny.worker_address in (await client.scheduler.identity())["workers"]:
                    await client.run_on_scheduler(dispatch, "register",
                                                  {"address": nanny.worker_address, "profile": profile})
                if time.time() >= profile["end_time"]:
                    return
                await asyncio.sleep(10)


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
    a = p.parse_args(argv)
    if a.command == "scheduler":
        if Path(a.scheduler_file).exists():
            p.error("scheduler file already exists; use a fresh path for a new pool")
        asyncio.run(serve(a))
    elif a.command == "worker":
        asyncio.run(worker(a))
    else:
        from .client import connect
        from .scheduler import dispatch
        with connect(a.scheduler) as c:
            print(json.dumps(c.run_on_scheduler(dispatch, a.command,
                             {"address": a.address} if a.command == "drain" else {}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
