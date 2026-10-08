"""Resume several closed dataset workflows: python3 resume-many.py "<reason>" dataset/<run_id>...  (control container).
A pool request that blocks the resume with a retryable failure (EDQUOT, WorkerLost) is retried at the same budget first."""
import asyncio, os, re, sys, time
from pathlib import Path
from temporalio.client import Client
from ecarsi.control.temporal import endpoint
from ecarsi.control.dataset import resume_dataset
from ecarsi.files import read
from ecarsi.warm_pool.state import retry
CONTROL, POOL = os.environ["CONTROL"], os.environ["POOL"]  # ops/run.sh passes them
reason, wids = sys.argv[1], sys.argv[2:]
RECONCILE = re.compile(r"Reconcile (\S+) \((\S+)\) before resume")
def say(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
async def one(client, wid):
    d = await client.get_workflow_handle(wid).describe()
    if d.status.name == "RUNNING":
        return say("skip", wid, "RUNNING")
    for _ in range(6):
        try:
            h = await resume_dataset(client, wid, None, reason)  # None: the queue the run was started on
            return say("resumed", wid, "was", d.status.name, "run", h.result_run_id)
        except ValueError as exc:
            m = RECONCILE.search(str(exc))
            if not m:
                return say("FAILED", wid, str(exc)[:300])
            rid, state = m.group(1), m.group(2)
            folder = Path(POOL) / "requests" / rid
            req = read(folder / "request.json")
            receipt = (read(folder / req["attempt_id"] / "receipt.json") or {}) if req else {}
            err = str(receipt.get("error", ""))
            if req and state == "failed" and ("Errno 122" in err or "WorkerLost" in err or receipt.get("retryable") is True):
                retry(POOL, rid, reason="retried at the same budget before resume: " + err[:80])
                say("  retried pool request", rid, "|", err[:100])
                continue
            return say("BLOCKED", wid, "by", rid, state, "|", err[:160] or "(bridge request / no receipt)")
    say("FAILED", wid, "too many reconciliations")
async def main():
    client = await Client.connect(endpoint(CONTROL)["endpoint"])
    for wid in wids:
        try:
            await one(client, wid)
        except Exception as exc:
            say("ERROR", wid, type(exc).__name__, str(exc)[:200])


asyncio.run(main())
