"""Combine the admission ledger with Dask's existing worker metrics."""
import time


def snapshot(client):
    from .scheduler import dispatch
    state = client.run_on_scheduler(dispatch, "status")
    workers = client.scheduler_info()["workers"]
    return summarize(state, workers)


def summarize(state, workers):
    now = time.time()
    rows = []
    for address, p in state["workers"].items():
        metrics = workers.get(address, {}).get("metrics", {})
        tasks = [t for t in state["tasks"].values()
                 if t.get("worker") == address and t["state"] in {"granted", "running"}]
        status = tasks[0]["state"] if tasks else "idle"
        if p.get("draining"):
            status = "draining"
        if now - p["observed_at"] > 90 or address not in workers:
            status = "stale"
        if now >= p["end_time"] - 60:
            status = "expiring"
        rows.append({**p, "address": address, "state": status,
                     "task": tasks[0].get("label") if tasks else None,
                     "cpu_percent": metrics.get("cpu", 0) / p["cpus"] if "cpu" in metrics else None,
                     "rss_bytes": metrics.get("memory"), "metrics_time": metrics.get("time"),
                     "remaining_seconds": max(0, int(p["end_time"] - now))})
    return {"observed_at": now, "workers": rows,
            "queued": [t for t in state["tasks"].values() if t["state"] == "queued"]}


def render(state):
    def number(value, divisor=1):
        return "?" if value is None else f"{value / divisor:.1f}"

    lines = ["NODE          JOB       STATE       CPU  CPU%  RSS/WORKER GiB  SLURM GiB  GPU  LEFT"]
    for w in state["workers"]:
        hours, rest = divmod(w["remaining_seconds"], 3600)
        lines.append(f"{w['host']:<13} {w['job_id']:<9} {w['state']:<11} {w['cpus']:>3} "
                     f"{number(w['cpu_percent']):>5}  {number(w['rss_bytes'], 2**30):>5}/{w['memory']/2**30:<6.1f} "
                     f"{w['allocation_memory']/2**30:>9.1f} {w['gpus']:>4} {hours:>3}h{rest//60:02}m")
        for gpu in w.get("gpu_stats", []):
            lines.append(f"  {gpu['name']}: GPU {number(gpu['utilization_percent'])}%  "
                         f"VRAM {number(gpu['memory_used_mib'], 1024)}/{number(gpu['memory_total_mib'], 1024)} GiB")
        if w["task"]:
            lines.append(f"  {w['task']}")
    lines.append(f"Queued: {len(state['queued'])}. CPU% is worker CPU usage / assigned CPUs; GPU metrics refresh every 30s.")
    for task in state["queued"]:
        lines.append(f"  {task.get('label', task['id'])}: {task.get('reason', 'waiting')}")
    return "\n".join(lines)
