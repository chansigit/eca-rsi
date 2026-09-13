"""Driver adapter: small admission requests first, payloads only after a grant."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import math
import os
import sys
import time
import uuid
from concurrent.futures import Future
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def runtime():
    result = {"python": f"{sys.version_info.major}.{sys.version_info.minor}"}
    for module in ("ecarsi", "msp", "osp"):
        spec = importlib.util.find_spec(module)
        if spec and spec.origin:
            root = Path(spec.origin).parent
            h = hashlib.sha256()
            for p in sorted(root.rglob("*.py")):
                h.update(str(p.relative_to(root)).encode())
                h.update(p.read_bytes())
            result[module] = h.hexdigest()
    for dist in ("numpy", "scipy", "scanpy", "anndata", "rapids-singlecell", "cupy-cuda12x"):
        try:
            result[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            pass
    return result


def connect(target=None, **kwargs):
    from distributed import Client
    target = target or os.environ.get("ECA_POOL_SCHEDULER")
    if not target:
        raise ValueError("pool mode needs ECA_POOL_SCHEDULER (address or scheduler.json)")
    return Client(**({"address": target} if "://" in target else {"scheduler_file": target}), **kwargs)


def execute(grant, fn, args, kwargs):
    """Fence re-execution and retain the reservation until Python actually exits."""
    import resource

    from distributed import get_client, get_worker

    from .scheduler import dispatch
    worker = get_worker()
    client = get_client()
    if any(runtime().get(k) != v for k, v in grant["runtime"].items()):
        raise RuntimeError("worker runtime changed after registration; restart this worker")
    token = {"id": grant["id"], "epoch": grant["epoch"], "worker": worker.address}
    client.run_on_scheduler(dispatch, "claim", token)
    ok = False
    try:
        value = fn(*args, **kwargs)
        ok = True
        return value
    finally:
        # Failure to acknowledge completion must fail the future, never publish
        # an unconfirmed result. ru_maxrss is a process high-water mark, not task RAM.
        client.run_on_scheduler(dispatch, "finish", dict(token, ok=ok,
                                peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024))


def local_fits(needs):
    from ecarsi.resources import (
        available_cpus,
        available_memory_bytes,
        current_rss_bytes,
    )
    host = __import__("socket").gethostname().split(".")[0]
    login = "-ln" in host or "login" in host
    if login and needs["seconds"] > 2:
        return False
    if needs["gpus"]:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        if not visible or visible in {"-1", "NoDevFiles"} or len(visible.split(",")) < needs["gpus"]:
            return False
    return (needs["cpus"] <= available_cpus() and
            needs["memory"] <= available_memory_bytes() * .8 - current_rss_bytes())


class PoolEndpoint:
    """submit waits for admission; result uses the ordinary Future interface.

    There is no automatic retry/migration after execution starts. Drivers resume
    through their own validated checkpoints. Closing a driver never stops a pool.
    """

    def __init__(self, scheduler=None, mode="pool"):
        if mode not in {"local", "pool", "auto"}:
            raise ValueError(f"unknown compute mode: {mode}")
        self.target, self.mode, self.client = scheduler, mode, None

    def __enter__(self):
        return self

    def _connect(self):
        if self.client is None:
            self.client = connect(self.target, timeout=15)
        return self.client

    def submit(self, fn, *args, tier="cpu", needs=None, **kwargs):
        if tier not in {"cpu", "gpu"}:
            raise ValueError(f"unknown tier: {tier}")
        # Explicit estimates override these intentionally coarse defaults.
        size = sum(getattr(x, "nbytes", sum(getattr(getattr(x, part, None), "nbytes", 0)
                                           for part in ("data", "indices", "indptr"))) for x in args)
        cells = max((getattr(x, "shape", (0,))[0] for x in args if getattr(x, "shape", ())), default=0)
        n = {"cpus": int(os.environ.get("ECA_POOL_TASK_CPUS", "1")),
             "memory": int(float(os.environ.get("ECA_POOL_TASK_MEMORY_GB", "0")) * 2**30) or max(512 * 2**20, size * 8),
             "gpus": int(tier == "gpu"),
             "seconds": float(os.environ.get("ECA_POOL_TASK_SECONDS", "0")) or max(1, cells / 20),
             "roots": [], "modules": [fn.__module__.split(".")[0]]}
        n.update(needs or {})
        for key in ("cpus", "memory", "gpus", "seconds"):
            if not isinstance(n[key], (int, float)) or not math.isfinite(n[key]) or n[key] < (0 if key == "gpus" else 1):
                raise ValueError(f"invalid requested {key}")
        if tier == "gpu" and n["gpus"] < 1:
            raise ValueError("GPU tasks must request at least one GPU")
        here = runtime()
        modules = set(n.pop("modules")) | {"python", "ecarsi", "numpy", "scipy", "scanpy", "anndata"}
        if tier == "gpu":
            modules |= {"rapids-singlecell", "cupy-cuda12x"}
        n["runtime"] = {k: v for k, v in here.items() if k in modules}
        can_local = local_fits(n)
        use_local = self.mode == "local" or (self.mode == "auto" and can_local and n["seconds"] <= 5)
        if self.mode == "auto" and not use_local and can_local:
            if not (self.target or os.environ.get("ECA_POOL_SCHEDULER")):
                use_local = True
            else:
                from .scheduler import dispatch
                status = self._connect().run_on_scheduler(dispatch, "status")
                from .scheduler import PoolScheduler
                busy = {t.get("worker") for t in status["tasks"].values() if t["state"] in {"granted", "running"}}
                now = time.time()
                ready = any(address not in busy and not w["draining"] and now - w["observed_at"] <= 90
                            and not PoolScheduler.fits(n, w, now) for address, w in status["workers"].items())
                # ponytail: no speculative queue-time model; run locally when all compatible slots are busy.
                use_local = not ready
        if use_local:
            if not can_local:
                raise RuntimeError("local resources are insufficient for this task; select pool or allocate resources")
            print(f"[pool] local: {fn.__name__}", flush=True)
            future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future
        c = self._connect()
        from .scheduler import dispatch
        request = dict(n, id=uuid.uuid4().hex, owner=c.id, label=f"{fn.__module__}.{fn.__name__}")
        task = c.run_on_scheduler(dispatch, "enqueue", request)
        token = {"id": task["id"], "epoch": task["epoch"]}
        timeout = float(os.environ.get("ECA_POOL_QUEUE_TIMEOUT", "3600"))
        start = time.monotonic()
        reason = None
        try:
            while task["state"] == "queued":
                if task["reason"] != reason:
                    reason = task["reason"]
                    print(f"[pool] waiting {request['label']}: {reason}", flush=True)
                if time.monotonic() - start > timeout:
                    raise TimeoutError(f"pool admission timed out: {reason}")
                time.sleep(.5)
                task = c.run_on_scheduler(dispatch, "poll", token)
            if task["state"] != "granted":
                raise RuntimeError(f"pool request {task['state']}: {task.get('reason', '')}")
            print(f"[pool] {request['label']} -> {task['worker']}", flush=True)
            future = c.submit(execute, task, fn, args, kwargs, key=f"eca-pool-{task['id']}",
                              workers=[task["worker"]], allow_other_workers=False,
                              resources={"pool_slot": 1}, retries=0, pure=False)
            return PoolFuture(c, future, token)
        except BaseException:
            c.run_on_scheduler(dispatch, "cancel", token)
            raise

    def __exit__(self, *exc):
        if self.client is not None:
            self.client.close()
            self.client = None


class PoolFuture:
    """A lost pinned worker must fail visibly instead of waiting for its return."""

    def __init__(self, client, future, token):
        self.client, self.future, self.token = client, future, token

    def result(self, timeout=None):
        from .scheduler import TERMINAL, dispatch
        deadline = float("inf") if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self.future.result(timeout=max(0, min(2, deadline - time.monotonic())))
            except TimeoutError:
                if self.future.done() or time.monotonic() >= deadline:
                    raise
                task = self.client.run_on_scheduler(dispatch, "poll", self.token)
                if task.get("worker_lost") or task["state"] in TERMINAL - {"done", "failed"}:
                    self.future.cancel()
                    raise ConnectionError(f"pool task {task['state']}: {task.get('reason', '')}")

    def cancel(self):
        from .scheduler import dispatch
        task = self.client.run_on_scheduler(dispatch, "cancel", self.token)
        if task["state"] == "running":
            return False
        self.future.cancel()
        return task["state"] == "cancelled"

    def done(self):
        return self.future.done()
