"""One admission ledger in the existing Dask scheduler event loop."""
from __future__ import annotations

import math
import os
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
        reserve_gb = float(os.environ.get('ECA_POOL_GPU_HOST_RESERVE_GB', '0'))
        if not math.isfinite(reserve_gb) or reserve_gb < 0:
            raise ValueError('ECA_POOL_GPU_HOST_RESERVE_GB must be finite and nonnegative')
        self.gpu_host_reserve = int(reserve_gb * 2**30)
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
                    reason = task.get("reason") if task["state"] == "stopping" else "worker disconnected"
                    task.update(state="lost", reason=reason, finished=time.time())
        self.tick()

    def remove_client(self, scheduler, client):
        for task in self.tasks.values():
            if task["owner"] == client and task["state"] in {"queued", "granted"}:
                task.update(state="cancelled", finished=time.time())
            elif task["owner"] == client and task["state"] == "running":
                self.stop_task(task, "driver disconnected")
        self.tick()

    def stop_task(self, task, reason):
        # Retain the slot and quarantine the worker until the host supervisor
        # has fenced its old process tree. Expiring the task here is unsafe.
        task.update(state="stopping", reason=reason)
        worker = self.workers.get(task["worker"])
        if worker is not None:
            worker["recycle_reason"] = reason

    @staticmethod
    def fits(task, worker, now):
        for key in ("cpus", "memory", "gpus"):
            if task[key] > worker[key]:
                return key
        if now + task["seconds"] + 60 > worker["end_time"]:
            return "remaining_time"
        runtimes = worker.get('runtimes', {'default': worker['runtime']})
        if not any(all(r.get(k) == v for k, v in task['runtime'].items()) and
                   all(k in r for k in task.get('required_runtime', ())) for r in runtimes.values()):
            return "runtime"
        if not set(task.get("roots", ())) <= set(worker.get("roots", ())):
            return "shared_paths"
        return ""

    def tick(self):
        now = time.time()
        for task in self.tasks.values():
            if task["state"] in {"queued", "granted"} and now > task["lease"]:
                task.update(state="expired", reason="driver/staging lease expired", finished=now)
            elif task["state"] == "running" and now >= task["execution_deadline"]:
                self.stop_task(task, "execution deadline exceeded")
        occupied = {}
        for t in self.tasks.values():
            if t['state'] in {'granted', 'running', 'stopping'}:
                occupied.setdefault(t.get('worker'), []).append(t)
        # ponytail: O(queue * workers), replace only if measured queue sizes warrant it.
        # Admit GPU requests first when both tiers are waiting for the same host.
        for task in sorted(self.tasks.values(), key=lambda t: not bool(t['gpus'])):
            if task["state"] != "queued":
                continue
            reasons = set()
            def load(item):
                address, worker = item
                active = occupied.get(address, [])
                pressure = max(sum(t[k] for t in active)/worker[k] for k in ('cpus', 'memory'))
                # Keep GPU capacity available for GPU work when CPU workers fit.
                return (bool(not task['gpus'] and worker['gpus']), pressure)
            for address, worker in sorted(self.workers.items(), key=load):
                if worker.get("draining") or worker.get("recycle_reason") or now - worker["observed_at"] > 90:
                    reasons.add("draining_or_stale")
                    continue
                reason = self.fits(task, worker, now)
                active = occupied.get(address, [])
                if reason or len(active) >= worker.get('task_slots', 1):
                    reasons.add(reason or "busy")
                    continue
                if any(task[k] + sum(t[k] for t in active) > worker[k] for k in ('cpus', 'memory', 'gpus')):
                    reasons.add('reserved_capacity')
                    continue
                if not task['gpus'] and worker['gpus']:
                    if task['memory'] + sum(t['memory'] for t in active) > worker['memory'] - self.gpu_host_reserve:
                        reasons.add('reserved_capacity')
                        continue
                if (now-worker.get('usage_observed_at', 0) <= 10
                        and task['memory'] + max(worker.get('rss_bytes', 0), sum(t['memory'] for t in active)) > worker['memory']):
                    reasons.add('observed_memory')
                    continue
                task.update(state="granted", worker=address, lease=now + 300, reason="")
                if worker.get('execution_protocol') == 2:
                    used_cpus = {cpu for t in active for cpu in t['cpu_ids']}
                    used_gpus = {gpu for t in active for gpu in t['gpu_ids']}
                    runtimes = worker.get('runtimes', {'default': worker['runtime']})
                    task.update(execution_protocol=2,
                                cpu_ids=[c for c in worker['cpu_ids'] if c not in used_cpus][:task['cpus']],
                                gpu_ids=[g for g in worker['gpu_ids'] if g not in used_gpus][:task['gpus']],
                                runtime_id=next(k for k, r in runtimes.items()
                                                if all(r.get(n) == v for n,v in task['runtime'].items())))
                occupied.setdefault(address, []).append(task)
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
            if (type(w.get('task_slots', 1)) is not int or not 1 <= w.get('task_slots', 1) <= w['cpus']
                    or w.get('task_slots', 1) > 1 and w.get('execution_protocol') != 2):
                raise ValueError('multiple task slots require isolated execution')
            if w.get('execution_protocol') == 2 and (not isinstance(w.get('runtimes'), dict)
                    or not w['runtimes'] or not all(isinstance(v, dict) for v in w['runtimes'].values())):
                raise ValueError('isolated execution needs verified runtimes')
            if old and any(old[k] != w[k] for k in ("host", "job_id", "cpu_ids", "gpu_ids", "memory", "runtime")):
                raise ValueError("worker resource/runtime profile changed; drain and restart the worker")
            if old and any(old.get(k) != w.get(k) for k in ('task_slots', 'execution_protocol', 'runtimes')):
                raise ValueError('worker execution profile changed; drain and restart the worker')
            peers = [v for k, v in self.workers.items() if k != address and v["host"] == w["host"]]
            for v in peers:
                if set(v["cpu_ids"]) & set(w["cpu_ids"]) or set(v["gpu_ids"]) & set(w["gpu_ids"]):
                    raise ValueError("overlapping CPU/GPU inventory on this node")
            used = sum(v["memory"] for v in peers if v["job_id"] == w["job_id"])
            if used + w["memory"] > w["allocation_memory"]:
                raise ValueError("workers exceed Slurm allocation memory")
            w["draining"] = old.get("draining", False)
            if old.get("recycle_reason"):
                w["recycle_reason"] = old["recycle_reason"]
            self.workers[address] = w
            if w.get('recycle_reason'):
                for task in self.tasks.values():
                    if task.get('worker') == address and task['state'] == 'running':
                        self.stop_task(task, w['recycle_reason'])
        elif action == "enqueue":
            if p["id"] in self.tasks:
                raise ValueError("duplicate request id")
            for k in ("cpus", "memory", "gpus", "seconds"):
                if not isinstance(p[k], (int, float)) or not math.isfinite(p[k]) or p[k] < (0 if k == "gpus" else 1):
                    raise ValueError(f"invalid requested {k}")
            if any(type(p[k]) is not int for k in ('cpus', 'gpus')):
                raise ValueError('CPU and GPU requests must be integers')
            if p["owner"] not in self.scheduler.clients:
                raise ValueError("driver disconnected")
            # Admission estimates are not reliable execution deadlines. Keep
            # the hard bound independent, capped again by allocation expiry.
            limit = p.get("execution_timeout", float(os.environ.get("ECA_POOL_EXECUTION_TIMEOUT", "21600")))
            if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
                raise ValueError("execution_timeout must be positive and finite")
            self.tasks[p["id"]] = dict(p, execution_timeout=limit, state="queued", submitted=now, lease=now + 60, epoch=self.epoch)
        elif action in {"poll", "claim", "finish", "cancel"}:
            task = self.tasks.get(p["id"])
            if task is None or p.get("epoch", self.epoch) != self.epoch:
                raise ConnectionError("pool restarted or request expired; resume through the driver")
            if action == "poll" and task["state"] == "queued":
                task["lease"] = now + 60
            elif action == "claim":
                w = self.workers.get(p["worker"])
                if (task["state"] != "granted" or task["worker"] != p["worker"] or
                        now > task["lease"] or w is None or now - w["observed_at"] > 90 or
                        w.get("draining") or w.get("recycle_reason") or self.fits(task, w, now)):
                    raise RuntimeError("grant is no longer valid; computation did not start")
                task.update(state="running", started=now,
                            execution_deadline=min(now + task["execution_timeout"], w["end_time"] - 30))
            elif action == "finish" and task["state"] == "stopping":
                raise ConnectionError(f"pool execution retired: {task.get('reason', '')}")
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
