#!/usr/bin/env python3
"""Delete the pool requests of finished dataset runs (inode plan layer 3, approved 2026-09-24).

A run is finished when its DatasetWorkflow is COMPLETED, or FAILED/TERMINATED while a later run of
the same dataset is COMPLETED (a failed run keeps its requests until it is superseded, as it keeps
its row on Periscope). Its requests are the lines of pool/by-workflow/<workflow id>.txt (written by
warm_pool.state.submit) for the dataset workflow, its unit workflows and their stage workflows, found
through Temporal's ParentWorkflowId. The deletion itself is a pool task (`warm_pool prune-list`) on a
worker node; this process only decides and submits. Requests journaled before 2026-09-24 are not
covered: those need the one-off listing scripts.

usage: request-pruner.py --service-root <run>/durable-control --pool-root <run>/pool --fleet-status <run>/fleet-status.json
                         [--interval 3600] [--limit 20] [--once] [--dry-run]
State: <pool>/pruned-runs.json (run -> done/pending). Logs to stdout.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from temporalio.client import Client

from ecarsi.control.temporal import endpoint
from ecarsi.warm_pool.state import digest, read, reference, save, status, submit

FINISHED = {"COMPLETED", "FAILED", "TERMINATED"}


def log(message):
    print(time.strftime("%F %T"), message, flush=True)


def prunable_runs(fleet):
    """dataset workflow id -> dataset name, for runs whose requests may go."""
    datasets = {wid: w for wid, w in fleet.get("workflows", {}).items() if w.get("kind") == "DatasetWorkflow"}
    completed = {w.get("dataset_id") for w in datasets.values() if w.get("status") == "COMPLETED"}
    return {wid: w.get("dataset_id") or wid for wid, w in datasets.items()
            if w.get("status") == "COMPLETED"
            or w.get("status") in FINISHED and w.get("dataset_id") in completed}


async def workflow_ids(client, dataset_wid):
    """The dataset workflow, its units and their stage workflows: every id a request may cite."""
    ids = {dataset_wid}
    async for unit in client.list_workflows(f'ParentWorkflowId="{dataset_wid}"'):
        if unit.id in ids:
            continue
        ids.add(unit.id)
        async for stage in client.list_workflows(f'ParentWorkflowId="{unit.id}"'):
            ids.add(stage.id)
    return sorted(ids)


def journals(pool, ids):
    return [p for p in (pool / "by-workflow" / (wid + ".txt") for wid in ids) if p.is_file()]


def prune_spec(pool, run, listing, dataset_name):
    return dict(request_id="prune-" + digest([run, str(listing)])[:32], operation_id="pool.prune",
                args=["-m", "ecarsi.warm_pool", "--root", str(pool), "prune-list", str(listing), "--result", "result.json"],
                cpus=2, memory_mb=1024, timeout_seconds=3600, inputs=[reference(listing)], outputs=["result.json"],
                trace=dict(workflow_id="pool/prune", dataset_id=dataset_name, unit_id="pool.prune"))


async def cycle(args):
    pool = Path(args.pool_root)
    fleet = read(args.fleet_status, {})
    state_path = pool / "pruned-runs.json"
    state = read(state_path, {})
    runs = prunable_runs(fleet)
    todo = [wid for wid in sorted(runs) if not state.get(wid, {}).get("done")]
    if not todo:
        log(f"{len(runs)} finished runs, all pruned")
        return
    client = await Client.connect(endpoint(args.service_root)["endpoint"])
    started = 0
    for wid in todo:
        entry = state.get(wid, {})
        if entry.get("pending"):
            current = status(pool, entry["pending"])
            if current["state"] == "succeeded":
                result = read(Path(current["receipt"]["outputs"][0]["path"]), {})
                log(f"{wid}: pruned {result}")
                if result.get("live"):
                    entry = {}  # live folders stay journaled; try again next cycle
                else:
                    for path in entry.get("journals", []):
                        Path(path).unlink(missing_ok=True)
                    Path(entry["listing"]).unlink(missing_ok=True)
                    entry = dict(done=time.time(), deleted=result.get("deleted", 0))
            elif current["state"] in {"failed", "cancelled"}:
                log(f"{wid}: prune task {entry['pending']} {current['state']}: {(current.get('receipt') or {}).get('error')}")
                entry = {}
            else:
                continue
            state[wid] = entry
            save(state_path, state)
            if entry.get("done") or started >= args.limit:
                continue
        if started >= args.limit:
            break
        ids = await workflow_ids(client, wid)
        files = journals(pool, ids)
        names = sorted({line.strip() for p in files for line in p.read_text(encoding="utf-8").splitlines() if line.strip()})
        if not names:
            log(f"{wid}: nothing journaled ({len(ids)} workflows)" + ("" if args.dry_run else "; marked done"))
            if not args.dry_run:
                state[wid] = dict(done=time.time(), deleted=0, note="nothing journaled")
                save(state_path, state)
            continue
        if args.dry_run:
            log(f"{wid}: would prune {len(names)} requests from {len(files)} journals ({len(ids)} workflows)")
            started += 1
            continue
        (pool / "prune").mkdir(exist_ok=True)
        listing = pool / "prune" / (digest(wid)[:16] + "-" + time.strftime("%Y%m%d-%H%M%S") + ".txt")
        listing.write_text("\n".join(names) + "\n", encoding="utf-8")
        spec = prune_spec(pool, wid, listing, runs[wid])
        submit(pool, spec)
        state[wid] = dict(pending=spec["request_id"], listing=str(listing), journals=[str(p) for p in files],
                          requests=len(names), submitted_at=time.time())
        save(state_path, state)
        log(f"{wid}: submitted {spec['request_id']} for {len(names)} requests")
        started += 1


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service-root", required=True, type=Path)
    parser.add_argument("--pool-root", required=True, type=Path)
    parser.add_argument("--fleet-status", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=3600)
    parser.add_argument("--limit", type=int, default=20, help="runs handled per cycle")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            await cycle(args)
        except Exception as exc:  # noqa: BLE001 - a pruner must outlive one bad cycle
            log(f"cycle failed: {type(exc).__name__}: {exc}")
        if args.once:
            return 0
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
