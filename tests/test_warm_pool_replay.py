"""The trace replay: causal arrivals, and a wide task that starves under FIFO but not under priority + reservation."""
import json
import time

from ecarsi.warm_pool.replay import Task, Worker, load_trace, report, simulate


def journal(pool, worker, day, rows):
    d = pool / "workers" / worker
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"tasks-{day}.jsonl").open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def row(rid, op, sub, start, dur, cpus=1, dataset="D", wf="zoom-in/x", worker="w1", host="h1", cpu_ids=(0,)):
    return dict(request_id=rid, attempt_id="a" * 12, operation=op, state="succeeded", dataset_id=dataset, workflow_id=wf,
                worker_id=worker, host=host, submitted_at=sub, started_at=start, finished_at=start + dur, duration_s=dur,
                cpus=cpus, memory_mb=100, cpu_ids=list(cpu_ids), gpu_ids=[])


def test_trace_keeps_causal_lags_and_infers_workers(tmp_path):
    day = "2026-09-24"
    journal(tmp_path, "w1", day, [row("a.deg-1", "zoom-in.deg", 100, 101, 10), row("b.deg-2", "zoom-in.deg", 115, 116, 10),
                                  row("c.tool-1", "read_evidence", 120, 121, 2, dataset="E")])
    trace = load_trace(tmp_path, day)
    a, b, c = sorted(trace["tasks"], key=lambda t: t.order)
    assert a.parent is None and b.parent is a and b.lag == 115 - 111 and c.parent is None  # E has no earlier task
    assert a.klass == "work" and c.klass == "tool"
    assert [w.id for w in trace["workers"]] == ["w1"] and trace["inferred_workers"] == ["w1"]
    result = simulate(trace, "passthrough", "none", latency=0)
    assert b.arrival == a.finish + b.lag and not result["unfinished"]
    rep = report(trace, result)
    assert rep["datasets"]["D"]["tasks"] == 2 and rep["unfinished"] == 0


def synthetic():
    """One 4-core worker, a stream of 1-core 10 s tasks every 2 s, and a 4-core task submitted at t=5."""
    tasks = []
    for i in range(30):
        tasks.append(Task(id=f"deg{i}", dataset="D", workflow="z", operation="zoom-in.deg", klass="work", cpus=1, memory_mb=10,
                          gpu_preferred=False, cpu_seconds=10, gpu_seconds=10, submitted_at=2 * i, started_at=None, finished_at=None,
                          parent=None, lag=None, order=i))
    tasks.append(Task(id="wide", dataset="W", workflow="c", operation="cross-sample.compute", klass="work", cpus=4, memory_mb=10,
                      gpu_preferred=False, cpu_seconds=5, gpu_seconds=5, submitted_at=5, started_at=None, finished_at=None,
                      parent=None, lag=None, order=100))
    return dict(tasks=tasks, workers=[Worker("w", "h", 4, 1000, 0, 0, 10_000)], ratio={}, inferred_workers=[])


def test_a_wide_task_starves_under_fifo_and_starts_under_priority_with_reservation():
    trace = synthetic()
    fifo = simulate(trace, "passthrough", "none", backfill=False, latency=0)
    wide = next(t for t in fifo["tasks"] if t.id == "wide")
    starved = wide.start - wide.arrival
    trace = synthetic()
    prio = simulate(trace, "passthrough", "cpus", backfill=True, latency=0)
    wide = next(t for t in prio["tasks"] if t.id == "wide")
    assert wide.start - wide.arrival < starved  # the reservation stops 1-core tasks from taking the cores it waits for
    assert wide.start - wide.arrival <= 12 and starved >= 40
