"""Resume one closed dataset workflow: python3 resume-one.py dataset/<run_id> "<reason>"  (control container)"""
import asyncio, os, sys
from temporalio.client import Client
from ecarsi.control.temporal import endpoint
from ecarsi.control.dataset import resume_dataset
CONTROL = os.environ["CONTROL"]  # ops/run.sh passes it
wid, reason = sys.argv[1], sys.argv[2]
async def main():
    client = await Client.connect(endpoint(CONTROL)["endpoint"])
    d = await client.get_workflow_handle(wid).describe()
    print("before:", d.status.name)
    h = await resume_dataset(client, wid, None, reason)  # None: the queue the run was started on
    print("resumed", wid, "run", h.result_run_id)
asyncio.run(main())
