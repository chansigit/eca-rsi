"""The control-plane monitor Periscope mounts at /_control/: read the records the control plane
and its workers publish about themselves, and render them.

Lives under `ecarsi/ui/` for two reasons that happen to be the same reason. It is the monitoring
surface, so `tests/test_monitor_isolation.py` holds it to one rule -- every fact comes from the node
that owns it, self-dated, and this module opens no connection to anything it watches. And `ui` is
the directory `run_state.PRESENTATION` keeps out of the computation's identity digest, so changing a
page can never again invalidate a running stage: shipping a monitoring change on 2026-09-21 cost a
drain of every pool worker and left runtime identity checking disabled for a whole batch.

Operator reports (`ecarsi.observatory` status/releases/tokens) are on the other side of that line and
may query Temporal directly. A human running a report is not a web page.
"""
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from ..agent import status as bridge_status
from ..warm_pool.state import read, status as pool_status

PAGE = Path(__file__).with_name("observatory.html")
# The monitor answers "are the workers healthy right now", not "how was last week scheduled": every
# finished attempt is in the worker's own tasks-<day>.jsonl journal, which is where a post-mortem
# belongs. So the index reaches back a fixed, short distance and forgets what falls out of it --
# both the folders it stats and the settled rows it keeps. Retaining every request ever seen made
# each API call walk a hundred thousand rows to answer a one-hour question (2026-09-21).
MAX_WINDOW = 4 * 3600      # widest window the page may ask for
INDEX_HORIZON = MAX_WINDOW + 3600  # request folders untouched for longer are neither read nor retained


def resource_history(pool: Path, since: float, until: float, cache: dict | None = None) -> list[dict]:
    """Read only selected UTC day partitions; reuse parsed append-only tails."""
    cache = cache if cache is not None else {}
    rows = []
    day = datetime.fromtimestamp(since, timezone.utc).date()
    last = datetime.fromtimestamp(until, timezone.utc).date()
    while day <= last:
        for path in (pool / "workers").glob("*/" + day.isoformat() + ".jsonl"):
            size = path.stat().st_size
            old_size, saved = cache.get(path, (0, []))
            if size < old_size:
                old_size, saved = 0, []
            if size != old_size:
                with path.open("rb") as stream:
                    stream.seek(old_size)
                    while True:
                        position = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            break  # retry a partial final line after the next append
                        try:
                            value = json.loads(line)
                            if isinstance(value, dict) and isinstance(value.get("observed_at"), (int, float)):
                                saved.append(value)
                        except ValueError:
                            continue
                cache[path] = (position, saved)
            rows.extend(row for row in saved if since <= row.get("observed_at", 0) <= until)
        day += timedelta(days=1)
    return sorted(rows, key=lambda row: row["observed_at"])


def summarize_resources(rows: list[dict], since: float, until: float) -> list[dict]:
    """At most 240 mean observations per worker in the selected window."""
    width = max(30, (until - since) / 240)
    bins = {}
    for row in rows:
        gpus = row.get("gpus") or []
        gpu_total = sum(g.get("memory_total_mb") or 0 for g in gpus)
        gpu_used = sum(g.get("memory_used_mb") or 0 for g in gpus)
        gpu_busy = [g["utilization_percent"] for g in gpus
                    if g.get("utilization_percent") is not None]
        values = {
            "cpu_percent": row.get("cpu_percent"),
            "cpu_cores_used": row["cpu_percent"] * len(row.get("cpu_ids", [])) / 100
                if row.get("cpu_percent") is not None else None,
            "cpu_cores_allocated": len(row.get("cpu_ids", [])),
            "memory_percent": 100 * row["memory_used_bytes"] / row["memory_total_bytes"]
                if row.get("memory_total_bytes") else None,
            "memory_used_gb": row.get("memory_used_bytes", 0) / 2**30,
            "memory_total_gb": row.get("memory_total_bytes", 0) / 2**30,
            "gpu_percent": sum(gpu_busy) / len(gpu_busy) if gpu_busy else None,
            "gpu_memory_percent": 100 * gpu_used / gpu_total
                if gpu_total else None,
            "gpu_memory_used_gb": gpu_used / 1024 if gpu_total else None,
            "gpu_memory_total_gb": gpu_total / 1024 if gpu_total else None,
        }
        key = (row.get("worker_id") or row["host"], int((row["observed_at"] - since) / width))
        group = bins.setdefault(key, {"worker_id": row.get("worker_id"), "host": row["host"],
                                      "observed_at": 0, "count": 0, "values": {}})
        group["observed_at"] += row["observed_at"]
        group["count"] += 1
        for name, value in values.items():
            if value is not None:
                total, count = group["values"].get(name, (0, 0))
                group["values"][name] = (total + value, count + 1)
    return sorted(({"worker_id": b["worker_id"], "host": b["host"],
                    "observed_at": b["observed_at"] / b["count"], "samples": b["count"],
                    **{name: (b["values"][name][0] / b["values"][name][1])
                       if name in b["values"] else None for name in (
                           "cpu_percent", "cpu_cores_used", "cpu_cores_allocated",
                           "memory_percent", "memory_used_gb", "memory_total_gb",
                           "gpu_percent", "gpu_memory_percent", "gpu_memory_used_gb",
                           "gpu_memory_total_gb")}}
                   for b in bins.values()), key=lambda b: (b["host"], b["observed_at"]))


def task_timeline(pool_rows: list[dict], bridge_rows: list[dict], since: float, until: float,
                  dataset: str = "", limit: int = 2000, dataset_page: int | None = None) -> dict:
    """Generic, bounded timeline from explicit trace metadata and durable receipts."""
    tasks = []
    # Bridge is the logical inbox; its Pool attempts are the actual execution bars.
    aliases = {row["id"]: row["pool_attempts"][-1] for row in bridge_rows if row.get("pool_attempts")}
    for service, rows in (("pool", pool_rows), ("bridge", bridge_rows)):
        for item in rows:
            if service == "bridge" and item["id"] in aliases:
                continue
            trace = item.get("trace")
            if trace:
                trace = {**trace, "depends_on": [aliases.get(p, p) for p in trace.get("depends_on", [])]}
            source = "explicit"
            if trace is None:
                run_id, dot, _ = item["id"].rpartition(".")
                if item.get("operation", "").startswith("organize.") and dot:
                    trace = {"workflow_id": "organize/" + run_id,
                             "dataset_id": re.split(r"-20\d{6}", run_id, maxsplit=1)[0],
                             "unit_id": item["operation"]}
                    source = "legacy inferred"
                else:
                    trace = {"workflow_id": "", "dataset_id": "Unattributed",
                             "unit_id": item.get("operation", "")}
                    source = "unattributed"
            if dataset and dataset.lower() not in trace["dataset_id"].lower():
                continue
            if item["submitted_at"] > until or (item.get("finished_at") or until) < since:
                continue
            tasks.append({key: item.get(key) for key in (
                "id", "operation", "state", "submitted_at", "started_at", "finished_at",
                "host", "worker_id", "cpu_ids", "cpus", "memory_mb", "model", "gpu_ids", "compute_backend")}
                | {"service": service, "trace": trace, "trace_source": source})
    pagination = {}
    if dataset_page is not None:
        if dataset_page < 0:
            raise ValueError("dataset page must be nonnegative")
        latest = {}
        for task in tasks:
            name = task["trace"]["dataset_id"]
            latest[name] = max(latest.get(name, 0), task.get("finished_at") or
                               task.get("started_at") or task["submitted_at"])
        names = sorted(latest, key=lambda name: (-latest[name], name))
        dataset_page = min(dataset_page, max(0, (len(names) - 1) // 10))
        selected = set(names[dataset_page * 10:(dataset_page + 1) * 10])
        tasks = [task for task in tasks if task["trace"]["dataset_id"] in selected]
        pagination = dict(dataset_page=dataset_page, dataset_page_size=10, dataset_total=len(names))
    tasks.sort(key=lambda item: item["submitted_at"])
    total = len(tasks)
    if dataset_page is None and total > limit:
        # The worker view asks "is every worker working", so a global cap is the wrong cut: it drops
        # the oldest tasks, and with them whole lanes, making a busy worker look absent. Cap each lane
        # instead -- every worker keeps its most recent tasks and no worker disappears.
        def lane(task):
            return "Agent Bridge" if task["service"] == "bridge" else task["worker_id"] or task["host"] or "unknown"
        lanes = {lane(task) for task in tasks}
        per_lane = max(1, limit // max(1, len(lanes)))
        counts, retained = {}, []
        for task in reversed(tasks):
            key = lane(task)
            if counts.get(key, 0) < per_lane:
                retained.append(task)
                counts[key] = counts.get(key, 0) + 1
        tasks = list(reversed(retained))
        return {"tasks": tasks, "total": total, "truncated": True, "lanes": len(lanes),
                "per_lane": per_lane, "since": since, "until": until,
                "source": "durable Pool and Bridge request/acceptance/result records"}
    if dataset_page is not None and total > limit:
        # Keep every dataset on the page visible; a busy dataset must not evict
        # its neighbors. Filtering one dataset gives it the full task budget.
        per_dataset = max(1, limit // max(1, len(selected)))
        counts, retained = {}, []
        for task in reversed(tasks):
            name = task["trace"]["dataset_id"]
            if counts.get(name, 0) < per_dataset:
                retained.append(task)
                counts[name] = counts.get(name, 0) + 1
        tasks = list(reversed(retained))
    return {"tasks": tasks[-limit:], "total": total, "truncated": total > limit,
            "since": since, "until": until,
            "source": "durable Pool and Bridge request/acceptance/result records", **pagination}


def worker_inventory(pool: Path, tasks: list[dict], now: float, cache: dict) -> list[dict]:
    """Worker budgets are distinct from host measurements; retain departed identities."""
    recent = {}
    for row in resource_history(pool, now - 300, now, cache.setdefault("resource_files", {})):
        recent.setdefault(row.get("worker_id"), []).append(row)
    workers = []
    for path in sorted((pool / "workers").glob("*/identity.json")):
        identity = read(path, {})
        worker_id = identity.get("worker_id", path.parent.name)
        rows = recent.get(worker_id, [])
        latest = rows[-1] if rows else None
        if latest is None:
            # Read only a bounded tail, including for workers last seen on an older day.
            logs = sorted(path.parent.glob("????-??-??.jsonl"))
            for log in reversed(logs):
                stat = log.stat()
                key = (str(log), stat.st_size, stat.st_mtime_ns)
                tails = cache.setdefault("worker_tails", {})
                if key not in tails:
                    with log.open("rb") as stream:
                        stream.seek(max(0, stat.st_size - 65536))
                        lines = stream.read().split(b"\n")[:-1]
                    value = None
                    for line in reversed(lines):
                        try:
                            candidate = json.loads(line)
                            if isinstance(candidate, dict) and isinstance(candidate.get("observed_at"), (int, float)):
                                value = candidate
                                break
                        except ValueError:
                            pass
                    tails[key] = value
                latest = tails[key]
                if latest:
                    break
        last_seen = (latest or {}).get("observed_at")
        reporting = last_seen is not None and 0 <= now - last_seen <= 90
        measurements = summarize_resources([latest], last_seen, last_seen + 1)[0] if latest else {}
        averaged = [summarize_resources([r], r["observed_at"], r["observed_at"] + 1)[0]
                    for r in rows]
        means = {}
        for metric in ("cpu_percent", "memory_percent", "gpu_percent", "gpu_memory_percent"):
            values = [r[metric] for r in averaged if r[metric] is not None]
            means[metric] = sum(values) / len(values) if values else None
        # Cancellation is still occupying its reservation until an execution receipt arrives.
        active = [t for t in tasks if t.get("worker_id") == worker_id
                  and t.get("started_at") and not t.get("finished_at")]
        workers.append({"worker_id": worker_id, "host": identity.get("host", "Unknown host"),
                        "slurm_job_id": identity.get("slurm_job_id"),
                        "gpu_ids": identity.get("gpu_ids", []),
                        "gpu_devices": (latest or {}).get("gpus", []),
                        "allocation": identity.get("allocation"),
                        "cpus": len(identity.get("cpu_ids", [])),
                        "memory_mb": identity.get("memory_mb"), "reporting": reporting,
                        "last_seen": last_seen, "current": measurements, "mean_5m": means,
                        "sample_count_5m": len(rows), "tasks": active,
                        "reserved_cpus": sum(t.get("cpus") or 0 for t in active),
                        "reserved_memory_mb": sum(t.get("memory_mb") or 0 for t in active),
                        "reserved_gpus": len({g for t in active for g in t.get("gpu_ids") or []})})
    known = {w["worker_id"] for w in workers}
    historical = {}
    for task in tasks:
        if task.get("host") and task.get("worker_id") not in known:
            key = task.get("worker_id") or ("legacy-host", task["host"])
            row = historical.setdefault(key, {"worker_id": task.get("worker_id"),
                "host": task["host"], "slurm_job_id": None, "reporting": False,
                "last_seen": None, "last_activity": 0, "tasks": [], "history_only": True})
            row["last_activity"] = max(row["last_activity"], task.get("finished_at") or
                                       task.get("started_at") or task["submitted_at"])
            if task.get("started_at") and not task.get("finished_at"):
                row["tasks"].append(task)
    return workers + list(historical.values())


def snapshot(root: Path, temporal_port: int = 8233, temporal_host: str = "127.0.0.1",
             cache: dict | None = None, pool_root: Path | None = None,
             bridge_root: Path | None = None, temporal_service_root: Path | None = None) -> dict:
    """Read published records; never connect to a scheduler or submit work."""
    root = Path(root)
    pool = Path(pool_root) if pool_root else root / "pool"
    bridge = Path(bridge_root) if bridge_root else root / "bridge"
    cache = cache if cache is not None else {}
    pool_done = cache.setdefault("pool_done", {})
    bridge_done = cache.setdefault("bridge_done", {})
    now = time.time()
    horizon = INDEX_HORIZON
    cache["indexed_since"] = now - horizon
    pool_stale, bridge_stale = cache.setdefault("pool_stale", {}), cache.setdefault("bridge_stale", {})
    for settled in (pool_done, bridge_done):
        for key in [k for k, row in settled.items()
                    if (row.get("finished_at") or row["submitted_at"]) < now - horizon]:
            del settled[key]
    reads = 0

    def pace():
        # ponytail: crude rate cap. The warm-up and widening walks share the coordinators' Lustre client;
        # an unpaced walk over tens of thousands of folders can stall their 30 s polls (2026-09-17).
        nonlocal reads
        reads += 1
        if reads > 500:
            time.sleep(0.01)

    def recent(entry, stale):
        """Folder mtime is the last state change; untouched folders beyond the horizon are remembered with
        that mtime and not stat'ed again, and are read once a wider horizon reaches them."""
        key = (entry.name, entry.inode())  # a folder re-created under an old name is new
        mtime = stale.get(key)
        if mtime is None:
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                return False
        if now - mtime > horizon:
            stale[key] = mtime
            return False
        stale.pop(key, None)
        return True
    scheduler = read(pool / "scheduler.json", {})
    bridge_summary = read(bridge / "summary.json", {})
    pool_rows = []
    if (pool / "config.json").is_file():
        # One directory listing per refresh; a settled request is never stat'ed again.
        for entry in os.scandir(pool / "requests"):
            if (entry.name, entry.inode()) in pool_done:
                pool_rows.append(pool_done[(entry.name, entry.inode())])
                continue
            if not recent(entry, pool_stale):
                continue
            folder = Path(entry.path)
            if not (folder / "request.json").is_file():
                continue
            pace()
            item = pool_status(pool, folder.name)
            request = read(folder / "request.json", {})
            spec = request.get("spec", {})
            receipt = item.get("receipt") or {}
            row = {
                "id": item["request_id"], "operation": item["operation_id"],
                "trace": spec.get("trace"),
                "state": item["state"], "cpus": spec.get("cpus"),
                "memory_mb": spec.get("memory_mb"), "submitted_at": item["submitted_at"],
                "started_at": receipt.get("started_at") or (item.get("accepted") or {}).get("started_at"),
                "finished_at": receipt.get("finished_at"),
                "host": (item.get("accepted") or {}).get("host"),
                "worker_id": (item.get("accepted") or {}).get("worker_id"),
                "cpu_ids": (item.get("accepted") or {}).get("cpu_ids"),
                "gpu_ids": (item.get("accepted") or {}).get("gpu_ids", []),
                "compute_backend": (item.get("accepted") or {}).get("compute_backend"),
                "gpu_request": spec.get("gpu"),
                "peak_rss_bytes": receipt.get("peak_rss_bytes"),
            }
            pool_rows.append(row)
            if row["state"] in {"succeeded", "cancelled"}:  # a failed request can still be retried in place
                pool_done[(entry.name, entry.inode())] = row
    bridge_rows = []
    if (bridge / "config.json").is_file():
        for entry in os.scandir(bridge / "requests"):
            if (entry.name, entry.inode()) in bridge_done:
                bridge_rows.append(bridge_done[(entry.name, entry.inode())])
                continue
            if not recent(entry, bridge_stale):
                continue
            folder = Path(entry.path)
            if not (folder / "request.json").is_file():
                continue
            pace()
            item = bridge_status(bridge, folder.name)
            request = read(folder / "request.json", {})
            response = item.get("response") or {}
            row = {
                "id": folder.name, "state": item["state"],
                "operation": request.get("spec", {}).get("operation_id"),
                "trace": request.get("spec", {}).get("trace"),
                "submitted_at": item["submitted_at"],
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "model": response.get("model"), "usage": response.get("usage"),
                "pool_attempts": [a["pool_request_id"] for a in item.get("attempts", [])],
                "host": (item.get("worker") or {}).get("host"),
            }
            bridge_rows.append(row)
            if row["state"] == "reply_saved":  # a failed turn can still be retried in place
                bridge_done[(entry.name, entry.inode())] = row
    pool_by_id = {row["id"]: row for row in pool_rows}
    for row in bridge_rows:
        for pool_id in row.get("pool_attempts", []):
            if pool_id in pool_by_id:
                pool_by_id[pool_id]["model"] = row["model"]
    # Where Temporal is and whether its UI answers: published by the control plane, which owns the
    # fact. This used to resolve the endpoint itself and probe the UI port over a socket -- a page
    # opening connections to the node running the coordinators, on every refresh.
    published = read(root / "fleet-status.json", {}).get("service") or {}
    temporal_source = published.get("source", "not published")
    service = published.get("service")
    temporal_ui = bool(published.get("ui"))
    temporal_port = published.get("ui_port") or 0
    workers = worker_inventory(pool, pool_rows, time.time(), cache)
    return {
        "generated_at": time.time(), "host": socket.gethostname(),
        "mode": "development / read-only", "temporal_ui": temporal_ui,
        "temporal_port": temporal_port, "temporal_source": temporal_source,
        "temporal_service": service,
        "scheduler": scheduler, "bridge_summary": bridge_summary,
        "worker_live_count": sum(w["reporting"] for w in workers), "workers": workers,
        "pool_waiting": sum(t["state"] == "queued" for t in pool_rows),
        "pool_requests": sorted(pool_rows, key=lambda x: x["submitted_at"], reverse=True),
        "bridge_requests": sorted(bridge_rows, key=lambda x: x["submitted_at"], reverse=True),
    }


class ControlPlane:
    """The control-plane monitor Periscope mounts at /_control/: one paced index of the pool and bridge
    request folders, refreshed at most every 2 s on demand and widened in the background when a page
    asks for a window older than the index. Read-only; never connects to a scheduler or submits work."""

    def __init__(self, root: Path, pool_root: Path | None = None, bridge_root: Path | None = None,
                 temporal_service_root: Path | None = None, temporal_port: int = 8233, temporal_host: str = "127.0.0.1"):
        self.root, self.pool_root, self.bridge_root = Path(root), pool_root, bridge_root
        self.temporal_service_root, self.temporal_port, self.temporal_host = temporal_service_root, temporal_port, temporal_host
        self.cache, self.guard = {}, threading.Lock()

    def _snapshot(self, cache=None):
        return snapshot(self.root, self.temporal_port, self.temporal_host,
                        self.cache if cache is None else cache,
                        self.pool_root, self.bridge_root, self.temporal_service_root)

    def start(self):
        """Warm the index in the background: the first listing of the request folders takes a while on
        Lustre, and a page load must not look like an outage meanwhile."""
        def warm():
            try:
                with self.guard:
                    self.cache["snapshot"], self.cache["snapshot_at"] = self._snapshot(), time.monotonic()
            except Exception as exc:  # noqa: BLE001 - the request path rebuilds it anyway
                print(f"control plane warm-up skipped: {exc}", flush=True)
        threading.Thread(target=warm, daemon=True).start()
        return self

    @staticmethod
    def page() -> bytes:
        return PAGE.read_bytes()

    def api(self, name: str, query: dict) -> dict:
        """'status' or 'timeline' with the page's query parameters; ValueError for a bad window."""
        cache = self.cache
        until = float(query.get("until", [time.time()])[0])
        since = float(query.get("since", [until - 3600])[0])
        limit = int(query.get("limit", [2000])[0])
        dataset_page = int(query["dataset_page"][0]) if "dataset_page" in query else None
        dataset = query.get("dataset", [""])[0]
        timeline = name == "timeline"
        if timeline and (not 0 < until - since <= MAX_WINDOW or not 1 <= limit <= 5000 or len(dataset) > 256):
            raise ValueError(f"choose a time window of at most {MAX_WINDOW // 3600} hours and limit up to 5000; "
                             "older work is in each worker's tasks-<day>.jsonl journal")
        with self.guard:
            if time.monotonic() - cache.get("snapshot_at", 0) >= 2:
                cache["snapshot"], cache["snapshot_at"] = self._snapshot(), time.monotonic()
            data = cache["snapshot"]
            if timeline:
                result = task_timeline(data["pool_requests"], data["bridge_requests"], since, until, dataset, limit, dataset_page)
                result["resources"] = summarize_resources(resource_history(
                    Path(self.pool_root) if self.pool_root else self.root / "pool", since, until,
                    cache.setdefault("resource_files", {})), since, until)
                result["indexed_since"] = cache.get("indexed_since")
                return result
            data = dict(data)
            rows = data["pool_requests"] + data["bridge_requests"]
            data["pool_total"] = len(data["pool_requests"])
            data["pool_succeeded"] = sum(r["state"] == "succeeded" for r in data["pool_requests"])
            data["indexed_since"] = cache.get("indexed_since")
            data["earliest_activity"] = min((r["submitted_at"] for r in rows), default=None)
            day = time.time() - 86400
            failed = [dict(id=r["id"], at=r.get("finished_at") or r["submitted_at"], operation=r.get("operation"),
                           dataset=(r.get("trace") or {}).get("dataset_id"), workflow_id=(r.get("trace") or {}).get("workflow_id"))
                      for r in rows if "fail" in str(r.get("state")) and (r.get("finished_at") or r["submitted_at"]) >= day]
            data["recent_failures"] = sorted(failed, key=lambda f: -f["at"])[:50]
            data["pool_requests"] = data["pool_requests"][:20]
            data["bridge_requests"] = data["bridge_requests"][:20]
            return data
