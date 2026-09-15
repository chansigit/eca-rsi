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
                  dataset: str = "", limit: int = 2000) -> dict:
    """Generic, bounded timeline from explicit trace metadata and durable receipts."""
    tasks = []
    for service, rows in (("pool", pool_rows), ("bridge", bridge_rows)):
        for item in rows:
            trace = item.get("trace")
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
                "host", "worker_id", "cpu_ids", "cpus", "memory_mb", "model")}
                | {"service": service, "trace": trace, "trace_source": source})
    tasks.sort(key=lambda item: item["submitted_at"])
    total = len(tasks)
    return {"tasks": tasks[-limit:], "total": total, "truncated": total > limit,
            "since": since, "until": until,
            "source": "durable Pool and Bridge request/acceptance/result records"}


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
                        "cpus": len(identity.get("cpu_ids", [])),
                        "memory_mb": identity.get("memory_mb"), "reporting": reporting,
                        "last_seen": last_seen, "current": measurements, "mean_5m": means,
                        "sample_count_5m": len(rows), "tasks": active,
                        "reserved_cpus": sum(t.get("cpus") or 0 for t in active),
                        "reserved_memory_mb": sum(t.get("memory_mb") or 0 for t in active)})
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
             bridge_root: Path | None = None) -> dict:
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
            if folder.name in pool_done and not (folder / "cancel.json").is_file():
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
            if folder.name in bridge_done:
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
            }
            bridge_rows.append(row)
            if row["state"] in {"reply_saved", "failed"}:
                bridge_done[folder.name] = row
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
    try:
        with socket.create_connection((temporal_host, temporal_port), timeout=0.2):
            temporal_ui = True
    except OSError:
        temporal_ui = False
    workers = worker_inventory(pool, pool_rows, time.time(), cache)
    return {
        "generated_at": time.time(), "host": socket.gethostname(),
        "mode": "development / read-only", "temporal_ui": temporal_ui,
        "temporal_port": temporal_port, "temporal_source": "unvalidated development SQLite",
        "scheduler": scheduler, "worker": worker, "bridge_summary": bridge_summary,
        "worker_live_count": sum(w["reporting"] for w in workers), "workers": workers,
        "pool_waiting": sum(t["state"] == "queued" for t in pool_rows),
        "acceptance": acceptance,
        "pool_requests": sorted(pool_rows, key=lambda x: x["submitted_at"], reverse=True),
        "bridge_requests": sorted(bridge_rows, key=lambda x: x["submitted_at"], reverse=True),
        "outputs": sorted(outputs, key=lambda x: x["published_at"], reverse=True),
    }


def serve(root: Path, port: int, temporal_port: int, bind: str,
          pool_root: Path | None = None, bridge_root: Path | None = None) -> None:
    cache = {}
    guard = threading.Lock()

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
                                                         pool_root, bridge_root)
                            cache["snapshot_at"] = time.monotonic()
                        data = cache["snapshot"]
                        if url.path == "/api/timeline":
                            query = parse_qs(url.query)
                            until = float(query.get("until", [time.time()])[0])
                            since = float(query.get("since", [until - 4 * 3600])[0])
                            limit = int(query.get("limit", [2000])[0])
                            dataset = query.get("dataset", [""])[0]
                            if not 0 < until - since <= 86400 or not 1 <= limit <= 2000 or len(dataset) > 256:
                                raise ValueError("choose a time window of at most 24 hours and limit up to 2000")
                            result = task_timeline(data["pool_requests"], data["bridge_requests"],
                                                   since, until, dataset, limit)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("serve")
    web.add_argument("--root", type=Path, required=True)
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--temporal-ui-port", type=int, default=8233)
    web.add_argument("--bind", default="127.0.0.1")
    web.add_argument("--pool-root", type=Path, help="shared Pool receipt and worker telemetry root")
    web.add_argument("--bridge-root", type=Path, help="shared Agent Bridge receipt root")
    history = commands.add_parser("temporal-ui")
    history.add_argument("--database", type=Path, required=True)
    history.add_argument("--port", type=int, default=7233)
    history.add_argument("--ui-port", type=int, default=8233)
    history.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.root, args.port, args.temporal_ui_port, args.bind,
              args.pool_root, args.bridge_root)
    else:
        asyncio.run(temporal_ui(args.database, args.port, args.ui_port, args.bind))


if __name__ == "__main__":
    main()
