import asyncio, collections, os, sys  # run with ops/run.sh control (it passes CONTROL)
from ecarsi.control.temporal import endpoint
from temporalio.client import Client
async def main():
    ep = endpoint(os.environ["CONTROL"])["endpoint"]
    client = await Client.connect(ep)
    by_type = collections.Counter(); n = 0
    async for wf in client.list_workflows('ExecutionStatus="Running"'):
        by_type[wf.workflow_type] += 1; n += 1
    print("running executions:", n)
    for t, c in by_type.most_common(): print(f"  {t:28s} {c}")
asyncio.run(main())
sys.stdout.flush(); os._exit(0)  # the temporal client can segfault at interpreter teardown
