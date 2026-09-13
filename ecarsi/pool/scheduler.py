"""One admission ledger in the existing Dask scheduler event loop."""
from __future__ import annotations

import math
import time
import uuid

from distributed.diagnostics.plugin import SchedulerPlugin
from tornado.ioloop import PeriodicCallback

NAME = "ecarsi-pool"
TERMINAL = {"done", "failed", "cancelled", "lost", "expired"}


def dispatch(action, payload=None, dask_scheduler=None):
    """Called through Dask's existing trusted-cluster RPC."""
    pool = dask_scheduler.extensions.get(NAME)
    if pool is None:
        raise RuntimeError("This is not an eca-rsi pool scheduler")
    return pool.handle(action, payload or {})


class PoolScheduler(SchedulerPlugin):
    name = NAME

    def __init__(self):
        self.epoch = uuid.uuid4().hex
        self.workers = {}
        self.tasks = {}  # insertion order is the shared FIFO

    async def start(self, scheduler):
        self.scheduler = scheduler
        scheduler.extensions[NAME] = self
        self.timer = PeriodicCallback(self.tick, 1000)
        self.timer.start()

    async def close(self):
        self.timer.stop()

    def remove_worker(self, scheduler, worker, **kwargs):
        self.workers.pop(worker, None)
        for task in self.tasks.values():
            if task.get("worker") == worker:
                # A finished computation's result can still be on that worker,
                # awaiting transfer. Do not let its pinned future wait forever.
                task["worker_lost"] = True
                if task["state"] not in TERMINAL:
                    task.update(state="lost", reason="worker disconnected", finished=time.time())
        self.tick()

    def remove_client(self, scheduler, client):
        for task in self.tasks.values():
            if task["owner"] == client and task["state"] in {"queued", "granted"}:
                task.update(state="cancelled", finished=time.time())
        # Running Python cannot be cancelled by dropping a Dask future.
        self.tick()

    @staticmethod
    def fits(task, worker, now):
        for key in ("cpus", "memory", "gpus"):
            if task[key] > worker[key]:
                return key
        if now + task["seconds"] + 60 > worker["end_time"]:
            return "remaining_time"
        if any(worker["runtime"].get(k) != v for k, v in task["runtime"].items()):
            return "runtime"
        if not set(task.get("roots", ())) <= set(worker.get("roots", ())):
            return "shared_paths"
        return ""

    def tick(self):
        now = time.time()
        for task in self.tasks.values():
            if task["state"] in {"queued", "granted"} and now > task["lease"]:
                task.update(state="expired", reason="driver/staging lease expired", finished=now)
        occupied = {t.get("worker") for t in self.tasks.values() if t["state"] in {"granted", "running"}}
        # ponytail: O(queue * workers), replace only if measured queue sizes warrant it.
        for task in self.tasks.values():
            if task["state"] != "queued":
                continue
            reasons = set()
            for address, worker in self.workers.items():
                if worker.get("draining") or now - worker["observed_at"] > 90:
                    reasons.add("draining_or_stale")
                    continue
                reason = self.fits(task, worker, now)
                if reason or address in occupied:
                    reasons.add(reason or "busy")
                    continue
                task.update(state="granted", worker=address, lease=now + 300, reason="")
                occupied.add(address)
                break
            else:
                task["reason"] = ",".join(sorted(reasons)) or "no_workers"
        # Keep a bounded recent status window; drivers own durable run receipts.
        for key, task in list(self.tasks.items()):
            if task["state"] in TERMINAL and now - task["finished"] > 3600:
                del self.tasks[key]

    def handle(self, action, p):
        now = time.time()
        if action == "register":
            address, w = p["address"], dict(p["profile"])
            if address not in self.scheduler.workers:
                raise ValueError("worker has not joined Dask")
            if (not w["cpu_ids"] or len(set(w["cpu_ids"])) != len(w["cpu_ids"]) or
                    len(w["cpu_ids"]) != w["cpus"] or len(set(w["gpu_ids"])) != len(w["gpu_ids"]) or
                    len(w["gpu_ids"]) != w["gpus"] or w["memory"] <= 0 or
                    w["memory"] > w["allocation_memory"] or
                    any(not math.isfinite(w[k]) for k in ("memory", "allocation_memory", "end_time", "observed_at"))):
                raise ValueError("invalid worker resource inventory")
            old = self.workers.get(address, {})
            if old and any(old[k] != w[k] for k in ("host", "job_id", "cpu_ids", "gpu_ids", "memory", "runtime")):
                raise ValueError("worker resource/runtime profile changed; drain and restart the worker")
            peers = [v for k, v in self.workers.items() if k != address and v["host"] == w["host"]]
            for v in peers:
                if set(v["cpu_ids"]) & set(w["cpu_ids"]) or set(v["gpu_ids"]) & set(w["gpu_ids"]):
                    raise ValueError("overlapping CPU/GPU inventory on this node")
            used = sum(v["memory"] for v in peers if v["job_id"] == w["job_id"])
            if used + w["memory"] > w["allocation_memory"]:
                raise ValueError("workers exceed Slurm allocation memory")
            w["draining"] = old.get("draining", False)
            self.workers[address] = w
        elif action == "enqueue":
            if p["id"] in self.tasks:
                raise ValueError("duplicate request id")
            for k in ("cpus", "memory", "gpus", "seconds"):
                if not isinstance(p[k], (int, float)) or not math.isfinite(p[k]) or p[k] < (0 if k == "gpus" else 1):
                    raise ValueError(f"invalid requested {k}")
            if p["owner"] not in self.scheduler.clients:
                raise ValueError("driver disconnected")
            self.tasks[p["id"]] = dict(p, state="queued", submitted=now, lease=now + 60, epoch=self.epoch)
        elif action in {"poll", "claim", "finish", "cancel"}:
            task = self.tasks.get(p["id"])
            if task is None or p.get("epoch", self.epoch) != self.epoch:
                raise RuntimeError("pool restarted or request expired; resume through the driver")
            if action == "poll" and task["state"] == "queued":
                task["lease"] = now + 60
            elif action == "claim":
                w = self.workers.get(p["worker"])
                if (task["state"] != "granted" or task["worker"] != p["worker"] or
                        now > task["lease"] or w is None or now - w["observed_at"] > 90 or
                        w.get("draining") or self.fits(task, w, now)):
                    raise RuntimeError("grant is no longer valid; computation did not start")
                task.update(state="running", started=now)
            elif action == "finish" and task["state"] == "running":
                if task["worker"] != p["worker"]:
                    raise ValueError("completion from the wrong worker")
                task.update(state="done" if p["ok"] else "failed", finished=now,
                            seconds_actual=now - task["started"], peak_rss=p.get("peak_rss"))
            elif action == "cancel" and task["state"] in {"queued", "granted"}:
                task.update(state="cancelled", finished=now)
            self.tick()
            return dict(task)
        elif action == "drain":
            self.workers[p["address"]]["draining"] = True
        elif action != "status":
            raise ValueError(f"unknown pool action: {action}")
        self.tick()
        if action == "enqueue":
            return dict(self.tasks[p["id"]])
        return {"epoch": self.epoch, "workers": self.workers, "tasks": self.tasks}
