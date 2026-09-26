"""What the pool's own task journals say each operation takes: the release layer's constants come from
here rather than from guesses (2026-09-25, D8 step 2). One paced sequential read of the workers'
`tasks-<day>.jsonl` files; run it as `warm_pool measure`, the scheduler does so every half hour."""
import json
import time
from pathlib import Path

DAYS = 3            # journals this old still describe the pool
RATE_WINDOW = 3600  # completions over the last hour give the pool's task throughput


def quantile(values, p):
    """The p-quantile of a non-empty list, by rank."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def summary(values):
    return dict(n=len(values), median_s=round(quantile(values, .5), 1), p90_s=round(quantile(values, .9), 1)) if values else dict(n=0)


def measure(pool_root, days=DAYS, now=None):
    """Per operation: CPU and GPU run times and queue waits of succeeded tasks (seconds), plus the
    pool's completions per second over the last hour and the p90 run time of batch work."""
    now = now or time.time()
    since = now - days * 86400
    per, recent, work = {}, 0, []
    for path in sorted(Path(pool_root, "workers").glob("*/tasks-????-??-??.jsonl")):
        day = time.mktime(time.strptime(path.stem[len("tasks-"):], "%Y-%m-%d"))
        if day + 86400 < since:
            continue
        with path.open(encoding="utf-8") as stream:
            for n, line in enumerate(stream):
                if n % 500 == 0:
                    time.sleep(0.002)  # shares the control node's Lustre client and CPU with the scheduler
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                finished = row.get("finished_at") or 0
                if row.get("state") != "succeeded" or not row.get("duration_s") or finished < since:
                    continue
                bucket = per.setdefault(row.get("operation") or "?", dict(cpu=[], gpu=[], wait=[]))
                bucket["gpu" if row.get("gpu_ids") else "cpu"].append(row["duration_s"])
                if row.get("queue_wait_s") is not None:
                    bucket["wait"].append(row["queue_wait_s"])
                if finished >= now - RATE_WINDOW:
                    recent += 1
                if row.get("operation") != "agent.call" and ".tool-" not in (row.get("request_id") or ""):
                    work.append(row["duration_s"])
    operations = {op: dict(cpu=summary(b["cpu"]), gpu=summary(b["gpu"]), wait=summary(b["wait"])) for op, b in per.items()}
    return dict(generated_at=now, days=days, operations=operations,
                completions_per_second=round(recent / RATE_WINDOW, 4),
                work_p90_s=round(quantile(work, .9), 1) if work else None)


def write(pool_root, days=DAYS):
    from .state import save
    result = measure(pool_root, days)
    save(Path(pool_root) / "measured.json", result)
    return result
