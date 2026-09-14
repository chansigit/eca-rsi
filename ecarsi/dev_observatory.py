"""Read-only development dashboard for the isolated RSI v2 records."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import time

from .agent_bridge import status as bridge_status
from .warm_pool.state import read, status as pool_status

PAGE = Path(__file__).with_name("dev_observatory.html")


def snapshot(root: Path, temporal_port: int = 8233) -> dict:
    """Read published records; never connect to a scheduler or submit work."""
    root = Path(root)
    pool = root / "organize-v2-pool"
    bridge = root / "organize-v2-bridge"
    scheduler = read(pool / "scheduler.json", {})
    worker = read(root / "organize-v2-pool-worker/worker.json", {})
    bridge_summary = read(bridge / "summary.json", {})
    pool_rows = []
    if (pool / "config.json").is_file():
        for item in pool_status(pool):
            request = read(pool / "requests" / item["request_id"] / "request.json", {})
            spec = request.get("spec", {})
            receipt = item.get("receipt") or {}
            pool_rows.append({
                "id": item["request_id"], "operation": item["operation_id"],
                "state": item["state"], "cpus": spec.get("cpus"),
                "memory_mb": spec.get("memory_mb"), "submitted_at": item["submitted_at"],
                "finished_at": receipt.get("finished_at"),
                "peak_rss_bytes": receipt.get("peak_rss_bytes"),
            })
    bridge_rows = []
    if (bridge / "config.json").is_file():
        for folder in sorted((bridge / "requests").iterdir()):
            if not (folder / "request.json").is_file():
                continue
            item = bridge_status(bridge, folder.name)
            response = item.get("response") or {}
            bridge_rows.append({
                "id": folder.name, "state": item["state"],
                "submitted_at": item["submitted_at"],
                "finished_at": item.get("finished_at"),
                "model": response.get("model"), "usage": response.get("usage"),
            })
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
        with socket.create_connection(("127.0.0.1", temporal_port), timeout=0.2):
            temporal_ui = True
    except OSError:
        temporal_ui = False
    temporal_history_count = 0
    for database in root.glob("organize-v2-*/temporal-dev.sqlite"):
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                temporal_history_count += connection.execute("SELECT count(*) FROM executions").fetchone()[0]
        except sqlite3.Error:
            pass
    return {
        "generated_at": time.time(), "host": socket.gethostname(),
        "mode": "development / read-only", "temporal_ui": temporal_ui,
        "temporal_port": temporal_port, "temporal_source": "isolated SQLite snapshot",
        "temporal_history_count": temporal_history_count,
        "scheduler": scheduler, "worker": worker, "bridge_summary": bridge_summary,
        "acceptance": acceptance,
        "pool_requests": sorted(pool_rows, key=lambda x: x["submitted_at"], reverse=True),
        "bridge_requests": sorted(bridge_rows, key=lambda x: x["submitted_at"], reverse=True),
        "outputs": sorted(outputs, key=lambda x: x["published_at"], reverse=True),
    }


def serve(root: Path, port: int, temporal_port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body, kind = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif self.path == "/api/status":
                body = json.dumps(snapshot(root, temporal_port), allow_nan=False).encode()
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

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"RSI v2 observatory: http://127.0.0.1:{port}/", flush=True)
        server.serve_forever()


async def temporal_ui(source: Path, destination: Path, port: int, ui_port: int) -> None:
    """Show old workflow history without writing to the original dev database."""
    from temporalio.testing import WorkflowEnvironment

    source = source.resolve(strict=True)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite Temporal snapshot: {destination}")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as copy:
            original.backup(copy)
    environment = await WorkflowEnvironment.start_local(
        ip="127.0.0.1", port=port, ui=True, ui_port=ui_port,
        dev_server_database_filename=str(destination),
        download_dest_dir=str(destination.parent),
    )
    print(f"Temporal historical UI: http://127.0.0.1:{ui_port}/", flush=True)
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
    history = commands.add_parser("temporal-ui")
    history.add_argument("--source-db", type=Path, required=True)
    history.add_argument("--snapshot-db", type=Path, required=True)
    history.add_argument("--port", type=int, default=7233)
    history.add_argument("--ui-port", type=int, default=8233)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.root, args.port, args.temporal_ui_port)
    else:
        asyncio.run(temporal_ui(args.source_db, args.snapshot_db, args.port, args.ui_port))


if __name__ == "__main__":
    main()
