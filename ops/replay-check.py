"""Replay the histories of every RUNNING Zoomin / Crosssample execution against the code on
PYTHONPATH (the dev worktree before a fast-forward): a change that would make a coordinator
fail their workflow tasks with a nondeterminism error shows up here first, offline.
usage: runpy-dev.sh ops/replay-check.py [--limit N] [--types ZoominWorkflow,CrosssampleWorkflow]
       [--status Completed --where 'StartTime > "2026-10-04T00:00:00Z"']: closed histories too, e.g. when none is running"""
import argparse, asyncio, os, time
from temporalio.client import Client
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner
from ecarsi.control.temporal import endpoint
from ecarsi.control.zoomin import ZoominWorkflow
from ecarsi.control.crosssample import CrosssampleWorkflow
from ecarsi.control.persample import PersampleWorkflow, SampleWorkflow
from ecarsi.control.dataset import DatasetWorkflow, AnalysisUnitWorkflow
from ecarsi.control.coordinator import OrganizeWorkflow, AgentWorkflow

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=0); ap.add_argument("--types", default="ZoominWorkflow,CrosssampleWorkflow")
    ap.add_argument("--status", default="Running"); ap.add_argument("--where", default="")
    args = ap.parse_args()
    client = await Client.connect(endpoint(os.environ["CONTROL"])["endpoint"])
    kinds = args.types.split(",")
    query = f'ExecutionStatus="{args.status}" AND (' + " OR ".join(f'WorkflowType="{k}"' for k in kinds) + ")"
    if args.where: query += " AND " + args.where
    targets = [w async for w in client.list_workflows(query)]
    targets.sort(key=lambda w: -(w.history_length or 0))
    if args.limit: targets = targets[:args.limit]
    replayer = Replayer(workflows=[ZoominWorkflow, CrosssampleWorkflow, PersampleWorkflow, SampleWorkflow, DatasetWorkflow,
                                   AnalysisUnitWorkflow, OrganizeWorkflow, AgentWorkflow], workflow_runner=UnsandboxedWorkflowRunner())
    ok, bad = 0, []
    t0 = time.time()
    for w in targets:
        try:
            history = await client.get_workflow_handle(w.id, run_id=w.run_id).fetch_history()
            t = time.time(); await replayer.replay_workflow(history); dt = time.time() - t
            ok += 1; print(f"ok   {w.workflow_type:20s} {len(history.events):6d} events {dt:5.1f}s  {w.id}", flush=True)
        except Exception as exc:
            bad.append((w.id, type(exc).__name__, str(exc)[:160])); print(f"FAIL {w.workflow_type:20s} {w.id}: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    print(f"replayed {ok} ok, {len(bad)} failed, {time.time()-t0:.0f}s")
    for b in bad: print(" ", b)
asyncio.run(main())
