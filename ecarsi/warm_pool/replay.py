"""Offline replay of a day of pool load through alternative release rules (D8 step 5, 2026-09-27).

The trace is the workers' task journals (what was submitted when, how long it ran, on what) and their
telemetry (which workers were online with how many cores, how much memory and which cards). Arrivals
keep their causal lag: a task arrives `lag` seconds after the finish of the latest task of its dataset
that had finished before it was submitted, so a policy that finishes work earlier sees the next work
earlier. The simulation runs our release layer (a `policy`) on a fixed tick over an HQ model (a
`placer`: FIFO like 0.26.2, or priority with EASY-style reservation like the patched build), and
reports per-dataset completion times, queue waits per class and idle core time. Compare policies by
dataset completion time, nothing else."""
import bisect
import collections
import heapq
import json
import time
from pathlib import Path

from .measure import quantile

TICK_SECONDS = 2.0        # the live scheduler's dispatch loop under load
START_LATENCY = 12.0      # seconds from HQ placement to the task running: hq client calls, executor start
                          # (median queue wait of the quiet day 2026-09-25, when nothing queued)
GPU_OPERATIONS = ()        # filled from the trace: operations that ran on a card at least once


class Task:
    __slots__ = ("id", "dataset", "workflow", "operation", "klass", "cpus", "memory_mb", "gpu_preferred",
                 "cpu_seconds", "gpu_seconds", "submitted_at", "started_at", "finished_at", "parent", "lag",
                 "arrival", "released", "start", "finish", "worker", "on_gpu", "order")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Worker:
    __slots__ = ("id", "host", "cpus", "memory_mb", "gpus", "online", "offline", "free_cpus", "free_mb", "free_gpus", "busy_until")

    def __init__(self, id, host, cpus, memory_mb, gpus, online, offline):
        self.id, self.host, self.cpus, self.memory_mb, self.gpus = id, host, cpus, memory_mb, gpus
        self.online, self.offline = online, offline
        self.free_cpus, self.free_mb, self.free_gpus = cpus, memory_mb, gpus
        self.busy_until = []  # (finish, cpus, mb, gpu) of running tasks, for reservations


def klass_of(request_id, operation):
    return "agent" if operation == "agent.call" else "tool" if ".tool-" in request_id else "work"


def load_trace(pool_root, day, memory_default_mb=118 * 1024):
    """Tasks and workers of one journal day (UTC date of finish)."""
    root = Path(pool_root) / "workers"
    rows, samples = [], collections.defaultdict(list)
    for path in sorted(root.glob(f"*/tasks-{day}.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("started_at") and r.get("finished_at") and r.get("submitted_at"):
                    rows.append(r)
    for path in sorted(root.glob(f"*/{day}.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                samples[s["worker_id"]].append(s)
    # GPU/CPU medians per operation, to price the mode a task did not run in
    modes = collections.defaultdict(lambda: {"cpu": [], "gpu": []})
    for r in rows:
        modes[r["operation"]]["gpu" if r.get("gpu_ids") else "cpu"].append(r["duration_s"])
    ratio = {}
    for op, m in modes.items():
        if m["cpu"] and m["gpu"]:
            ratio[op] = quantile(m["cpu"], .5) / max(quantile(m["gpu"], .5), 1e-6)
    gpu_ops = {op for op, m in modes.items() if m["gpu"]}
    rows.sort(key=lambda r: r["submitted_at"])
    tasks, by_dataset = [], collections.defaultdict(list)  # dataset -> finished (finished_at, task) sorted by finish
    for n, r in enumerate(rows):
        op = r["operation"]
        on_gpu = bool(r.get("gpu_ids"))
        d = r["duration_s"]
        t = Task(id=r["request_id"] + "/" + r["attempt_id"][:8], dataset=r.get("dataset_id") or "?", workflow=r.get("workflow_id") or "?",
                 operation=op, klass=klass_of(r["request_id"], op), cpus=r.get("cpus") or 1, memory_mb=r.get("memory_mb") or 256,
                 gpu_preferred=op in gpu_ops, submitted_at=r["submitted_at"], started_at=r["started_at"], finished_at=r["finished_at"],
                 cpu_seconds=d if not on_gpu else d * ratio.get(op, 2.0), gpu_seconds=d if on_gpu else d / ratio.get(op, 2.0), order=n)
        tasks.append(t)
    # causal parent: the latest task of the same dataset (same workflow preferred) finished before submission
    finished_by_dataset = collections.defaultdict(list)
    for t in sorted(tasks, key=lambda t: t.finished_at):
        finished_by_dataset[t.dataset].append(t)
    for t in tasks:
        done = finished_by_dataset[t.dataset]
        keys = [x.finished_at for x in done]
        i = bisect.bisect_right(keys, t.submitted_at)
        parent = None
        for x in reversed(done[:i]):
            if x is t:
                continue
            if parent is None:
                parent = x
            if x.workflow == t.workflow:
                parent = x
                break
            if t.submitted_at - x.finished_at > 3600:
                break
        t.parent, t.lag = parent, (t.submitted_at - parent.finished_at if parent else None)
    workers = []
    for wid, ss in samples.items():
        ss.sort(key=lambda s: s["observed_at"])
        cpus = max(len(s.get("cpu_ids") or []) for s in ss)
        mb = max((s.get("job_memory_limit_bytes") or s.get("memory_total_bytes") or 0) for s in ss) // (1024 * 1024) or memory_default_mb
        gpus = max(len(s.get("gpus") or []) for s in ss)
        workers.append(Worker(wid, ss[0]["host"], cpus, mb, gpus, ss[0]["observed_at"] - 30, ss[-1]["observed_at"] + 30))
    seen = {w.id for w in workers}
    inferred = collections.defaultdict(lambda: dict(cpu_ids=set(), first=None, last=None, host=None, gpu=False))
    for r in rows:
        if r.get("worker_id") in seen or r.get("worker_id") in (None, "unassigned"):
            continue
        w = inferred[r["worker_id"]]
        w["cpu_ids"].update(r.get("cpu_ids") or [])
        w["host"] = r.get("host")
        w["gpu"] = w["gpu"] or bool(r.get("gpu_ids"))
        w["first"] = min(w["first"] or r["started_at"], r["started_at"])
        w["last"] = max(w["last"] or r["finished_at"], r["finished_at"])
    for wid, w in inferred.items():
        cpus = (max(w["cpu_ids"]) + 1) if w["cpu_ids"] else 8
        workers.append(Worker(wid, w["host"], cpus, memory_default_mb, int(w["gpu"]), w["first"] - 60, w["last"] + 60))
    return dict(day=day, tasks=tasks, workers=workers, ratio=ratio, inferred_workers=sorted(inferred))


# ----- policies: what our release layer does each tick -------------------------------------------------

HELD_ORDER = {"agent": 0, "tool": 1, "work": 2}


def held_key(t):
    """`held` is kept sorted by this: interactive work first, then batch work FIFO."""
    return (HELD_ORDER[t.klass], t.submitted_at, t.order)


def passthrough(held, state):
    """Release everything at once: HQ orders by priority itself. Policies return how many of the
    sorted `held` prefix to release."""
    return len(held)


def measured(held, state):
    """The live release layer of 38c550a: interactive work first, batch work FIFO up to a backlog cap of
    two ticks of starts plus the idle cores (a drain is not modelled: with priorities it is gone)."""
    idle = sum(w.free_cpus for w in state["workers"] if state["now"] >= w.online and state["now"] < w.offline)
    cap = max(16, int(2 * state["recent_starts"] * TICK_SECONDS / 60)) + idle - state["hq_waiting"]
    n = 0
    for t in held:
        if t.klass != "work":
            n += 1
        elif cap > 0:
            n += 1; cap -= 1
        else:
            break
    return n


POLICIES = {"passthrough": passthrough, "measured": measured}


def priority_cpus(t):
    return t.cpus


def priority_score(t):
    """Class first (a model is waiting on agent turns and session tools), then width, then age in
    minutes: a task that keeps missing its chance outranks fresh ones. Age grows equally for every
    waiting task, so the order is fixed at arrival (arrival earlier = higher)."""
    base = {"agent": 1000, "tool": 800, "work": 0}[t.klass]
    return base + 10 * t.cpus - t.arrival / 60


PRIORITIES = {"none": lambda t: 0, "cpus": priority_cpus, "score": priority_score}
SCAN_LIMIT = 300  # waiting tasks a placement pass examines before giving up, like a bounded scheduler pass


# ----- the HQ model ----------------------------------------------------------------------------------------

def fits(w, t, gpu):
    return w.free_cpus >= t.cpus and w.free_mb >= t.memory_mb and (not gpu or w.free_gpus >= 1)


def place(waiting, workers, now, backfill, running, tick, latency=START_LATENCY):
    """Start what fits. `waiting` is already in priority order (highest first); with `backfill`, the
    first task that fits nowhere reserves the worker where it will fit soonest (finish times are known
    here, HQ estimates them) and only tasks ending before that reservation may take that worker's cores
    meanwhile. A pass examines at most SCAN_LIMIT waiting tasks."""
    live = [w for w in workers if w.online <= now < w.offline]
    if not live or not waiting:
        return []
    started, reserved, examined = [], {}, 0  # worker id -> reservation time
    for t in waiting:
        examined += 1
        if examined > SCAN_LIMIT:
            break
        gpu_first = t.gpu_preferred
        chosen = None
        for w in live:
            if w.id in reserved:
                continue
            if gpu_first and fits(w, t, True):
                chosen = (w, True); break
            if fits(w, t, False):
                chosen = (w, False); break
        if chosen is None and backfill:
            # reserve the worker that frees enough soonest; allow shorter tasks there meanwhile
            best = None
            for w in live:
                if w.cpus < t.cpus or w.memory_mb < t.memory_mb or w.id in reserved:
                    continue
                need_cpus, need_mb, at = t.cpus - w.free_cpus, t.memory_mb - w.free_mb, now
                for finish, cpus, mb, _ in sorted(w.busy_until):
                    if need_cpus <= 0 and need_mb <= 0:
                        break
                    need_cpus, need_mb, at = need_cpus - cpus, need_mb - mb, finish
                if need_cpus <= 0 and need_mb <= 0 and (best is None or at < best[0]):
                    best = (at, w)
            if best:
                reserved[best[1].id] = best[0]
            continue
        if chosen is None:
            continue
        w, on_gpu = chosen
        run = (t.gpu_seconds if on_gpu else t.cpu_seconds) + latency
        if backfill and w.id in reserved and now + run > reserved[w.id]:
            continue
        w.free_cpus -= t.cpus; w.free_mb -= t.memory_mb; w.free_gpus -= int(on_gpu)
        t.start, t.finish, t.worker, t.on_gpu = now, now + run, w.id, on_gpu
        w.busy_until.append((t.finish, t.cpus, t.memory_mb, on_gpu))
        heapq.heappush(running, (t.finish, t.order, t))
        started.append(t)
    return started


def simulate(trace, policy="measured", priority="none", backfill=False, tick=TICK_SECONDS, latency=START_LATENCY):
    tasks, workers = trace["tasks"], [Worker(w.id, w.host, w.cpus, w.memory_mb, w.gpus, w.online, w.offline) for w in trace["workers"]]
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        t.arrival = t.released = t.start = t.finish = t.worker = None
    pol, prio = POLICIES[policy], PRIORITIES[priority]
    arrivals = []  # (time, order, task)
    waiting_parent = collections.defaultdict(list)  # parent id -> children
    for t in tasks:
        if t.parent is None:
            heapq.heappush(arrivals, (t.submitted_at, t.order, t))
        else:
            waiting_parent[t.parent.id].append(t)
    held, held_keys = [], []  # sorted by held_key
    hq_waiting, hq_keys, running, done = [], [], [], []  # hq_waiting sorted by (-priority, order)
    starts = collections.deque()
    dirty = True  # resources freed or tasks queued since the last placement pass
    now = arrivals[0][0] if arrivals else 0
    next_tick = now
    workers_by_id = {w.id: w for w in workers}
    released = None  # tasks the last tick released; None between ticks
    while arrivals or held or hq_waiting or running:
        released = None
        candidates = [arrivals[0][0] if arrivals else None, running[0][0] if running else None, next_tick if (held or hq_waiting) else None]
        candidates = [c for c in candidates if c is not None]
        if not candidates:
            break
        now = min(candidates)
        while arrivals and arrivals[0][0] <= now:
            _, _, t = heapq.heappop(arrivals)
            t.arrival = now
            key = held_key(t)
            i = bisect.bisect_left(held_keys, key)
            held_keys.insert(i, key); held.insert(i, t)
            dirty = True
        while running and running[0][0] <= now:
            _, _, t = heapq.heappop(running)
            w = workers_by_id[t.worker]
            w.free_cpus += t.cpus; w.free_mb += t.memory_mb; w.free_gpus += int(t.on_gpu)
            w.busy_until = [b for b in w.busy_until if b[0] > now]
            done.append(t)
            dirty = True
            for child in waiting_parent.pop(t.id, ()):
                heapq.heappush(arrivals, (now + child.lag, child.order, child))
        if now >= next_tick:
            while starts and starts[0] < now - 60:
                starts.popleft()
            n = pol(held, dict(now=now, workers=workers, hq_waiting=len(hq_waiting), recent_starts=len(starts)))
            for t in held[:n]:
                t.released = now
                key = (-prio(t), t.order)
                i = bisect.bisect_left(hq_keys, key)
                hq_keys.insert(i, key); hq_waiting.insert(i, t)
            del held[:n], held_keys[:n]
            released = n
            next_tick = now + tick
        if dirty and hq_waiting:
            started = place(hq_waiting, workers, now, backfill, running, tick, latency)
            if started:  # only the examined prefix can have started
                n = SCAN_LIMIT + 1
                keep = [i for i, t in enumerate(hq_waiting[:n]) if t.start is None]
                hq_waiting[:n], hq_keys[:n] = [hq_waiting[i] for i in keep], [hq_keys[i] for i in keep]
                starts.extend([now] * len(started))
            dirty = bool(started)
        if released == 0 and not (arrivals or running or dirty):
            break  # a tick released and started nothing with nothing in flight: nothing will ever change
    return dict(tasks=tasks, unfinished=[t for t in tasks if t.finish is None])


def report(trace, result):
    tasks = [t for t in result["tasks"] if t.finish is not None]
    by_dataset = collections.defaultdict(list)
    for t in tasks:
        by_dataset[t.dataset].append(t)
    datasets = {}
    for d, ts in by_dataset.items():
        actual = max(t.finished_at for t in ts) - min(t.submitted_at for t in ts)
        simulated = max(t.finish for t in ts) - min(t.arrival for t in ts)
        datasets[d] = dict(tasks=len(ts), actual_span_s=round(actual), simulated_span_s=round(simulated))
    waits = collections.defaultdict(list)
    for t in tasks:
        waits[t.klass].append(t.start - t.arrival)
    wait = {k: dict(median_s=round(quantile(v, .5), 1), p90_s=round(quantile(v, .9), 1), max_s=round(max(v), 1)) for k, v in waits.items()}
    spans = [v["simulated_span_s"] / max(v["actual_span_s"], 1) for v in datasets.values() if v["tasks"] >= 20]
    stuck = collections.Counter((t.operation, t.cpus, t.memory_mb) for t in result["unfinished"]).most_common(3)
    return dict(datasets=datasets, wait=wait, unfinished=len(result["unfinished"]), unfinished_top=stuck,
                span_ratio_median=round(quantile(spans, .5), 3) if spans else None,
                span_ratio_p90=round(quantile(spans, .9), 3) if spans else None,
                total_simulated_span_s=round(max(t.finish for t in tasks) - min(t.arrival for t in tasks)))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--day", required=True, help="journal day, e.g. 2026-09-24")
    p.add_argument("--policy", default="measured", choices=sorted(POLICIES))
    p.add_argument("--priority", default="none", choices=sorted(PRIORITIES))
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--latency", type=float, default=START_LATENCY)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    t0 = time.time()
    trace = load_trace(a.root, a.day)
    loaded = time.time()
    result = simulate(trace, a.policy, a.priority, a.backfill, latency=a.latency)
    rep = report(trace, result)
    rep.update(policy=a.policy, priority=a.priority, backfill=a.backfill, tasks=len(trace["tasks"]), workers=len(trace["workers"]),
               inferred_workers=trace["inferred_workers"], load_s=round(loaded - t0, 1), simulate_s=round(time.time() - loaded, 1))
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"{a.day} policy={a.policy} priority={a.priority} backfill={a.backfill}: {rep['tasks']} tasks, {rep['workers']} workers "
              f"(inferred {len(rep['inferred_workers'])}), unfinished {rep['unfinished']} {rep['unfinished_top']}, load {rep['load_s']} s, sim {rep['simulate_s']} s")
        print(f"  dataset span simulated/actual: median {rep['span_ratio_median']}  p90 {rep['span_ratio_p90']}")
        for k, v in sorted(rep["wait"].items()):
            print(f"  wait {k:6s} median {v['median_s']:7.1f}  p90 {v['p90_s']:8.1f}  max {v['max_s']:8.1f}")
    return rep


if __name__ == "__main__":
    main()
