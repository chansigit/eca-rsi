"""Read-only development dashboard for the isolated RSI v2 records."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .agent_bridge import status as bridge_status
from .warm_pool.state import lock, read, status as pool_status

PAGE = Path(__file__).with_name("dev_observatory.html")


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
    pool = Path(pool_root) if pool_root else root / "organize-v2-pool"
    bridge = Path(bridge_root) if bridge_root else root / "organize-v2-bridge"
    cache = cache if cache is not None else {}
    pool_done = cache.setdefault("pool_done", {})
    bridge_done = cache.setdefault("bridge_done", {})
    scheduler = read(pool / "scheduler.json", {})
    worker = read(root / "organize-v2-pool-worker/worker.json", {})
    bridge_summary = read(bridge / "summary.json", {})
    pool_rows = []
    if (pool / "config.json").is_file():
        for folder in (pool / "requests").iterdir():
            if not (folder / "request.json").is_file():
                continue
            if pool_done.get(folder.name, {}).get('state') == 'succeeded' and not (folder / "cancel.json").is_file():
                pool_rows.append(pool_done[folder.name])
                continue
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
            if row["state"] in {"succeeded", "failed", "cancelled"}:
                pool_done[folder.name] = row
    bridge_rows = []
    if (bridge / "config.json").is_file():
        for folder in sorted((bridge / "requests").iterdir()):
            if not (folder / "request.json").is_file():
                continue
            if bridge_done.get(folder.name, {}).get('state') == 'reply_saved':
                bridge_rows.append(bridge_done[folder.name])
                continue
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
            if row["state"] in {"reply_saved", "failed"}:
                bridge_done[folder.name] = row
    pool_by_id = {row["id"]: row for row in pool_rows}
    for row in bridge_rows:
        for pool_id in row.get("pool_attempts", []):
            if pool_id in pool_by_id:
                pool_by_id[pool_id]["model"] = row["model"]
    outputs = []
    for publication in sorted(root.glob("organize-v2-*/*-output/publication.json")):
        output = publication.parent
        manifest = read(output / "organize/manifest.json", {})
        if manifest.get("state") != "complete":
            continue
        audit = manifest.get("experiment_audit", {})
        outputs.append({
            "name": output.parent.name.removeprefix("organize-v2-").replace("-20260914", ""),
            "run": output.name, "cells": sum(u.get("n_cells", 0) for u in manifest.get("units_written", [])),
            "samples": sum(v.get("experiments", 0) for v in audit.values()),
            "published_at": publication.stat().st_mtime,
            "path": str(output),
        })
    reports = sorted(root.glob("multinode-*/run-*/acceptance.json"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    report = read(reports[0], {}) if reports else {}
    acceptance = {"passed": report.get("passed"), "tests": report.get("tests", []),
                  "nodes": [{"host": n.get("host"), "worker_cpu": n.get("worker_cpu")}
                            for n in report.get("nodes", [])]}
    temporal_source = 'unvalidated development SQLite'
    service = None
    if temporal_service_root:
        from .temporal_service import endpoint
        temporal_source = 'PostgreSQL on shared storage'
        try:
            service = endpoint(temporal_service_root)
            temporal_host = service['endpoint'].rsplit(':', 1)[0]
            temporal_port = service['ui_port']
        except ConnectionError:
            temporal_port = 0
    try:
        with socket.create_connection((temporal_host, temporal_port), timeout=0.2):
            temporal_ui = True
    except OSError:
        temporal_ui = False
    workers = worker_inventory(pool, pool_rows, time.time(), cache)
    return {
        "generated_at": time.time(), "host": socket.gethostname(),
        "mode": "development / read-only", "temporal_ui": temporal_ui,
        "temporal_port": temporal_port, "temporal_source": temporal_source,
        "temporal_service": service,
        "scheduler": scheduler, "worker": worker, "bridge_summary": bridge_summary,
        "worker_live_count": sum(w["reporting"] for w in workers), "workers": workers,
        "pool_waiting": sum(t["state"] == "queued" for t in pool_rows),
        "acceptance": acceptance,
        "pool_requests": sorted(pool_rows, key=lambda x: x["submitted_at"], reverse=True),
        "bridge_requests": sorted(bridge_rows, key=lambda x: x["submitted_at"], reverse=True),
        "outputs": sorted(outputs, key=lambda x: x["published_at"], reverse=True),
    }


def serve(root: Path, port: int, temporal_port: int, bind: str,
          pool_root: Path | None = None, bridge_root: Path | None = None,
          temporal_service_root: Path | None = None) -> None:
    cache = {}
    guard = threading.Lock()

    def warm():
        # The first walk over every saved pool/bridge request takes minutes on
        # Lustre; do it at startup so the first page load does not look down.
        try:
            with guard:
                cache["snapshot"] = snapshot(root, temporal_port, bind, cache,
                                             pool_root, bridge_root, temporal_service_root)
                cache["snapshot_at"] = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - the request path rebuilds it anyway
            print(f"observatory warm-up skipped: {exc}", flush=True)
    threading.Thread(target=warm, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                body, kind = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif url.path in {"/api/status", "/api/timeline"}:
                try:
                    with guard:
                        if time.monotonic() - cache.get("snapshot_at", 0) >= 2:
                            cache["snapshot"] = snapshot(root, temporal_port, bind, cache,
                                                         pool_root, bridge_root, temporal_service_root)
                            cache["snapshot_at"] = time.monotonic()
                        data = cache["snapshot"]
                        if url.path == "/api/timeline":
                            query = parse_qs(url.query)
                            until = float(query.get("until", [time.time()])[0])
                            since = float(query.get("since", [until - 4 * 3600])[0])
                            limit = int(query.get("limit", [2000])[0])
                            dataset_page = int(query["dataset_page"][0]) if "dataset_page" in query else None
                            dataset = query.get("dataset", [""])[0]
                            if not 0 < until - since <= 86400 or not 1 <= limit <= 2000 or len(dataset) > 256:
                                raise ValueError("choose a time window of at most 24 hours and limit up to 2000")
                            result = task_timeline(data["pool_requests"], data["bridge_requests"],
                                                   since, until, dataset, limit, dataset_page)
                            result["resources"] = summarize_resources(resource_history(
                                Path(pool_root) if pool_root else Path(root) / "organize-v2-pool", since, until,
                                cache.setdefault("resource_files", {})), since, until)
                        else:
                            data = dict(data)
                            data["pool_total"] = len(data["pool_requests"])
                            data["pool_succeeded"] = sum(r["state"] == "succeeded" for r in data["pool_requests"])
                            data["bridge_total"] = len(data["bridge_requests"])
                            data["bridge_saved"] = sum(r["state"] == "reply_saved" for r in data["bridge_requests"])
                            data["bridge_tokens_total"] = sum(
                                (r.get("usage") or {}).get("tokens_in") or 0 for r in data["bridge_requests"])
                            data["bridge_tokens_total"] += sum(
                                (r.get("usage") or {}).get("tokens_out") or 0 for r in data["bridge_requests"])
                            data["outputs_total"] = len(data["outputs"])
                            data["pool_requests"] = data["pool_requests"][:20]
                            data["bridge_requests"] = data["bridge_requests"][:20]
                            data["outputs"] = data["outputs"][:20]
                            result = data
                    body = json.dumps(result, allow_nan=False).encode()
                except (ValueError, OverflowError) as exc:
                    self.send_error(400, str(exc))
                    return
                kind = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer((bind, port), Handler) as server:
        print(f"RSI v2 observatory: {bind}:{port}", flush=True)
        server.serve_forever()


async def temporal_ui(database: Path, port: int, ui_port: int, bind: str) -> None:
    """Experimental Temporal SQLite viewer; shared-filesystem recovery is unvalidated."""
    from temporalio.testing import WorkflowEnvironment

    database = database.resolve()
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    with lock(database.parent / "server.lock", blocking=False):
        environment = await WorkflowEnvironment.start_local(
            ip=bind, port=port, ui=True, ui_port=ui_port,
            dev_server_database_filename=str(database),
            download_dest_dir=str(database.parent),
        )
        print(f"Temporal experimental development UI: http://127.0.0.1:{ui_port}/", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await environment.shutdown()


# ---------------------------------------------------------------------------
# Command-line status: the same records as the page, read from the cheap
# sources only (HQ server, summary files, worker telemetry tails, Temporal
# visibility). It never walks the request folders unless --sessions asks.

def classify_job(name):
    """HQ job names are 'rsi.<request id>.<attempt>': operation class and dataset run."""
    request = name.removeprefix('rsi.').rsplit('.', 1)[0] if name.startswith('rsi.') else name
    if request.startswith('agent-'):
        return 'agent', 'model calls'
    if '.tool-' in request:
        return 'tool', request.split('.tool-', 1)[0].split('-', 1)[0] + ' sessions'
    run, _, operation = request.rpartition('.')
    return re.sub(r'-[0-9a-f]{8,}$', '', operation) or operation, re.sub(r'-[0-9a-f]{16,}$', '', run) or run


def hq_view(pool):
    """Workers and jobs as the HQ server sees them; an unreachable server yields empty lists."""
    from .warm_pool.backend import HyperQueue
    try:
        hq = HyperQueue(pool)
        workers = hq.call('worker', 'list') or []
        jobs = {state: hq.call('job', 'list', '--filter', state) or [] for state in ('running', 'waiting')}
    except (RuntimeError, OSError, ValueError) as exc:
        return {'error': str(exc)[:200], 'workers': [], 'jobs': {'running': [], 'waiting': []}}
    for worker in workers:
        try:
            info = (hq.call('worker', 'info', str(worker['id'])) or [{}])[0]
            worker['running_tasks'] = next(iter((info.get('runtime_info') or {}).values()), {}).get('running_tasks')
        except (RuntimeError, OSError, ValueError, StopIteration):
            worker['running_tasks'] = None
    return {'workers': workers, 'jobs': jobs}


def worker_rows(pool, hq, now):
    identities = {}
    for path in (pool / 'workers').glob('*/identity.json'):
        identity = read(path, {})
        job = str((identity.get('allocation') or {}).get('job_id') or identity.get('slurm_job_id') or '')
        identities[(identity.get('host'), job)] = identity
    latest = {}
    for row in resource_history(pool, now - 120, now):
        latest[row['host']] = row
    rows = []
    for worker in hq['workers']:
        host = worker['configuration']['hostname'].split('.')[0]
        resources = {r['name']: r for r in worker['configuration']['resources']['resources']}
        # One host can carry several Slurm grants over time; the work directory names the current one.
        job = Path(worker['configuration'].get('work_dir', '')).name.rpartition('-')[2]
        identity = identities.get((host, job), {})
        allocation = identity.get('allocation') or {}
        sample = latest.get(host)
        cpus = len(resources.get('cpus', {}).get('values', []))
        rows.append({
            'id': worker['id'], 'host': host, 'slurm_job_id': identity.get('slurm_job_id') or allocation.get('job_id'),
            'cpus': cpus, 'memory_gb': resources.get('mem', {}).get('size', 0) / 10000 / 1024,
            'gpus': sum(1 for name in resources if name.startswith('gpuSlot')),
            'running_tasks': worker.get('running_tasks'),
            'cpu_cores_used': sample['cpu_percent'] * cpus / 100 if sample and sample.get('cpu_percent') is not None else None,
            'node_memory_used_gb': sample['memory_used_bytes'] / 2**30 if sample else None,
            'node_memory_gb': sample['memory_total_bytes'] / 2**30 if sample else None,
            'seen_seconds_ago': now - sample['observed_at'] if sample else None,
            'hours_left': (allocation['end_time'] - now) / 3600 if allocation.get('end_time') else None,
        })
    return rows


async def temporal_view(service_root, now):
    from datetime import datetime, timedelta, timezone
    from temporalio.client import Client
    from .temporal_service import endpoint
    client = await Client.connect(endpoint(service_root)['endpoint'])
    counts = {}
    for state in ('Running', 'Completed', 'Failed', 'Terminated'):
        counts[state.lower()] = sum([1 async for _ in client.list_workflows(
            f"WorkflowType = 'DatasetWorkflow' AND ExecutionStatus = '{state}'")])
    datasets = {}
    async for w in client.list_workflows("ExecutionStatus = 'Running' AND WorkflowType != 'DatasetWorkflow' "
                                         "AND WorkflowType != 'AnalysisUnitWorkflow'"):
        stage, _, rest = w.id.partition('/')
        run = rest.split('/')[0]
        entry = datasets.setdefault(re.sub(r'-[0-9a-f]{16,}$', '', run), {'stage': stage, 'workflows': []})
        entry['workflows'].append({'type': w.workflow_type, 'id': w.id.split('/')[-1],
                                   'age_minutes': (now - w.start_time.timestamp()) / 60})
    since = datetime.fromtimestamp(now - 6 * 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    failures = [{'type': w.workflow_type, 'id': w.id, 'closed_at': w.close_time.timestamp()}
                async for w in client.list_workflows(f"ExecutionStatus = 'Failed' AND CloseTime > '{since}'")]
    return {'dataset_counts': counts, 'running': datasets, 'failures_6h': failures}


def session_stats(bridge, pool, hours, now):
    """Opt-in scan of recent request folders: turns per session and submission rejections."""
    cutoff = now - hours * 3600
    turns, started = {}, set()
    for entry in os.scandir(bridge / 'requests'):
        if '.turn-' not in entry.name or entry.stat().st_mtime < cutoff:
            continue
        session, number = entry.name.rsplit('.turn-', 1)
        turns[session] = max(turns.get(session, 0), int(number) + 1)
        if number == '0':
            started.add(session)
    kinds = {}
    for session in started:
        kinds.setdefault(session.split('-', 1)[0], []).append(turns[session])
    submissions = {}
    for entry in os.scandir(pool / 'requests'):
        if '.tool-' not in entry.name or entry.stat().st_mtime < cutoff:
            continue
        request = read(Path(entry.path) / 'request.json', {})
        operation = request.get('spec', {}).get('operation_id', '')
        if not operation.startswith('submit_'):
            continue
        result = read(Path(entry.path) / request.get('attempt_id', '') / 'outputs' / 'result.json')
        if result is None:
            continue
        bucket = submissions.setdefault(operation, {'accepted': 0, 'rejected': 0})
        bucket['rejected' if result.get('is_error') else 'accepted'] += 1
    return {'hours': hours,
            'sessions': {kind: {'started': len(v), 'turns_p50': sorted(v)[len(v) // 2], 'turns_max': max(v)}
                         for kind, v in kinds.items()},
            'submissions': submissions}


def status_report(root, pool_root=None, bridge_root=None, temporal_service_root=None, sessions_hours=None):
    root = Path(root)
    pool = Path(pool_root) if pool_root else root / 'organize-v2-pool'
    bridge = Path(bridge_root) if bridge_root else root / 'organize-v2-bridge'
    now = time.time()
    hq = hq_view(pool)
    jobs = {}
    for state, items in hq['jobs'].items():
        by_class, by_dataset = {}, {}
        for job in items:
            kind, dataset = classify_job(job['name'])
            by_class[kind] = by_class.get(kind, 0) + 1
            by_dataset[dataset] = by_dataset.get(dataset, 0) + 1
        jobs[state] = {'total': len(items), 'by_class': by_class, 'by_dataset': by_dataset}
    report = {'generated_at': now, 'host': socket.gethostname(), 'root': str(root),
              'scheduler': read(pool / 'scheduler.json', {}), 'bridge': read(bridge / 'summary.json', {}),
              'hq_error': hq.get('error'), 'workers': worker_rows(pool, hq, now), 'jobs': jobs}
    if temporal_service_root:
        try:
            report['temporal'] = asyncio.run(temporal_view(temporal_service_root, now))
        except Exception as exc:  # noqa: BLE001 - the report must still print the rest
            report['temporal'] = {'error': repr(exc)[:200]}
    if sessions_hours:
        report['sessions'] = session_stats(bridge, pool, sessions_hours, now)
    return report


def render_status(report):
    now = report['generated_at']
    ago = lambda t: f"{now - t:.0f} s ago" if t else 'never'
    lines = [f"RSI v2 status  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}  {report['host']}  {report['root']}",
             '', 'CONTROL PLANE']
    scheduler = report['scheduler']
    lines.append(f"  scheduler  {scheduler.get('state', 'unknown'):8s} {scheduler.get('host', '?'):16s} scan {scheduler.get('dispatch_scan_seconds', 0):.1f} s  seen {ago(scheduler.get('observed_at'))}")
    bridge = report['bridge']
    counts = bridge.get('counts', {})
    lines.append(f"  bridge     in flight {bridge.get('running', 0)}/{bridge.get('concurrency', '?')}  queued {counts.get('queued', 0)}  "
                 f"replies {counts.get('reply_saved', 0)}  failed {counts.get('failed', 0)}  scan {bridge.get('dispatch_scan_seconds', 0):.1f} s  seen {ago(bridge.get('updated_at'))}")
    for model in bridge.get('models', []):
        lines.append(f"    {model['model'].get('model', '?'):32s} {model.get('state', '?'):9s} in flight {model.get('in_flight', 0):3d}  "
                     f"last {model.get('last_latency_seconds') or 0:6.1f} s  ok {model.get('successes', 0)}  fail {model.get('failures', 0)}  timeout {model.get('timeouts', 0)}")
    temporal = report.get('temporal')
    if temporal:
        if 'error' in temporal:
            lines.append('  temporal   ' + temporal['error'])
        else:
            lines.append('  temporal   datasets ' + ', '.join(f"{k} {v}" for k, v in temporal['dataset_counts'].items())
                         + f"; failed workflows in 6 h: {len(temporal['failures_6h'])}")
    lines += ['', f"WORKERS ({len(report['workers'])} in HQ)" + (f"  HQ error: {report['hq_error']}" if report['hq_error'] else '')]
    lines.append('  id   host          job        cpus   used   mem GiB   node mem GiB   tasks  time left  seen')
    for w in report['workers']:
        used = f"{w['cpu_cores_used']:5.1f}" if w['cpu_cores_used'] is not None else '    ?'
        mem = f"{w['memory_gb']:6.0f}   " + (f"{w['node_memory_used_gb']:5.0f}/{w['node_memory_gb']:<5.0f}" if w['node_memory_used_gb'] is not None else '     ?     ')
        left = f"{w['hours_left']:6.1f} h" if w['hours_left'] is not None else '       ?'
        seen = f"{w['seen_seconds_ago']:.0f} s" if w['seen_seconds_ago'] is not None else 'no telemetry'
        gpu = f" gpu {w['gpus']}" if w['gpus'] else ''
        lines.append(f"  {w['id']:<4} {w['host']:13s} {str(w['slurm_job_id'] or '?'):10s} {w['cpus']:3d}   {used}   {mem}    {str(w['running_tasks'] if w['running_tasks'] is not None else '?'):>5s}  {left}  {seen}{gpu}")
    for state in ('running', 'waiting'):
        jobs = report['jobs'].get(state, {'total': 0, 'by_class': {}, 'by_dataset': {}})
        lines += ['', f"POOL {state.upper()} {jobs['total']}  " + ', '.join(f"{k} {v}" for k, v in sorted(jobs['by_class'].items(), key=lambda kv: -kv[1]))]
        if jobs['by_dataset']:
            lines.append('  ' + ', '.join(f"{k} {v}" for k, v in sorted(jobs['by_dataset'].items(), key=lambda kv: -kv[1])[:12]))
    if temporal and 'running' in temporal:
        lines += ['', f"DATASETS RUNNING ({len(temporal['running'])})"]
        for name, entry in sorted(temporal['running'].items()):
            parts = []
            for kind in ('CrosssampleWorkflow', 'ZoominWorkflow', 'AgentWorkflow'):
                ages = sorted(w['age_minutes'] for w in entry['workflows'] if w['type'] == kind)
                if ages:
                    parts.append(f"{kind.removesuffix('Workflow')} x{len(ages)} {ages[0]:.0f}-{ages[-1]:.0f} min" if len(ages) > 1
                                 else f"{kind.removesuffix('Workflow')} {ages[0]:.0f} min")
            lines.append(f"  {name:28s} {entry['stage']:13s} " + '; '.join(parts))
        for failure in temporal['failures_6h'][-5:]:
            lines.append(f"  FAILED {time.strftime('%H:%M', time.localtime(failure['closed_at']))} {failure['type']} {failure['id'][:80]}")
    sessions = report.get('sessions')
    if sessions:
        lines += ['', f"SESSIONS (last {sessions['hours']:g} h)"]
        for kind, v in sorted(sessions['sessions'].items()):
            lines.append(f"  {kind:6s} started {v['started']:4d}  turns p50 {v['turns_p50']:3d}  max {v['turns_max']:3d}")
        for op, v in sorted(sessions['submissions'].items()):
            total = v['accepted'] + v['rejected']
            lines.append(f"  {op:18s} accepted {v['accepted']:4d}  rejected {v['rejected']:4d}  ({100 * v['rejected'] / max(1, total):.0f} %)")
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("serve")
    web.add_argument("--root", type=Path, required=True)
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--temporal-ui-port", type=int, default=8233)
    web.add_argument("--temporal-service-root", type=Path, help="shared PostgreSQL-backed Temporal service discovery")
    web.add_argument("--bind", default="127.0.0.1")
    web.add_argument("--pool-root", type=Path, help="shared Pool receipt and worker telemetry root")
    web.add_argument("--bridge-root", type=Path, help="shared Agent Bridge receipt root")
    history = commands.add_parser("temporal-ui")
    history.add_argument("--database", type=Path, required=True)
    history.add_argument("--port", type=int, default=7233)
    history.add_argument("--ui-port", type=int, default=8233)
    history.add_argument("--bind", default="127.0.0.1")
    text = commands.add_parser("status", help="print the same records as the page, from the cheap sources only")
    text.add_argument("--root", type=Path, required=True)
    text.add_argument("--pool-root", type=Path)
    text.add_argument("--bridge-root", type=Path)
    text.add_argument("--temporal-service-root", type=Path)
    text.add_argument("--sessions", type=float, metavar="HOURS", help="also scan recent sessions: turns and rejected submissions")
    text.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "status":
        report = status_report(args.root, args.pool_root, args.bridge_root, args.temporal_service_root, args.sessions)
        print(json.dumps(report, indent=2, default=str) if args.json else render_status(report))
    elif args.command == "serve":
        serve(args.root, args.port, args.temporal_ui_port, args.bind,
              args.pool_root, args.bridge_root, args.temporal_service_root)
    else:
        asyncio.run(temporal_ui(args.database, args.port, args.ui_port, args.bind))


if __name__ == "__main__":
    main()
