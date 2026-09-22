#!/usr/bin/env python3
"""Publish the control plane's own verdict on every run, for Periscope to read.

Periscope derives the fleet table from the files stages write, so it is only ever as current as the
last stage boundary: a dataset that died inside Temporal without writing anything reads as running
until the twelve-hour staleness rule notices, and one that was just resumed reads as failed until
its first publication lands. Temporal knows immediately -- a single query returns every dataset
workflow with its status, 92 of them in 20 ms here -- but Periscope runs on an interpreter with no
Temporal client, and the control plane's standing rule is that it publishes records while Periscope
only reads them. So this publishes them: one small file, rewritten every few seconds, carrying its
own timestamp so a reader can tell a quiet fleet from a dead publisher.

    fleet-status.py --service-root <run>/durable-control --out <run>/fleet-status.json [--interval 10]

Read-only against Temporal; it starts no work and changes no state.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

KINDS = ("DatasetWorkflow", "AnalysisUnitWorkflow")


async def collect(client) -> dict:
    """The newest execution of each workflow id. A resumed dataset and a workflow that continued as
    new both leave older executions behind, and listing returns them in no particular order, so the
    latest start wins: reporting a superseded FAILED over the RUNNING that replaced it is precisely
    the lie this file exists to stop."""
    out = {}
    for kind in KINDS:
        async for wf in client.list_workflows(f"WorkflowType = '{kind}'"):
            started = wf.start_time.timestamp() if wf.start_time else 0.0
            previous = out.get(wf.id)
            if previous is not None and previous["started"] >= started:
                continue
            out[wf.id] = {
                "kind": kind,
                "status": wf.status.name,
                "started": started,
                "closed": wf.close_time.timestamp() if wf.close_time else None,
            }
    return out


def publish(path: Path, payload: dict) -> None:
    """Atomic: a reader must never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    from temporalio.client import Client
    from ecarsi.control.temporal import endpoint

    client = None
    while True:
        started = time.time()
        try:
            if client is None:
                client = await Client.connect(endpoint(str(args.service_root))["endpoint"])
            workflows = await collect(client)
            publish(args.out, {"generated_at": started, "took_s": round(time.time() - started, 3),
                               "workflows": workflows})
        except Exception as exc:  # noqa: BLE001 - a monitor must outlive a restart of what it watches
            client = None
            sys.stderr.write(f"[fleet-status] {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
        if args.once:
            return 0
        await asyncio.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
