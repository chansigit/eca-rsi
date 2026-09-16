"""Durable admission check: python tests/check_persample_capacity.py HOST:PORT."""
import asyncio
import sys
import uuid
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import Worker, Replayer, UnsandboxedWorkflowRunner
from ecarsi.persample_workflow import PersampleWorkflow


@activity.defn(name="sample_step")
def sample_step(action, args):
    if action == "partition":
        return dict(id=f'{args[1]}:{args[0].get("test_samples", 2)}', output="partition.json")
    if action == "read":
        offset, total = map(int, args[0].split(':'))
        return dict(total_samples=total, next_offset=offset + 1,
                    entries=[dict(sample_id=str(offset))])
    if action == "publish":
        assert len(args[1]) == args[3]['total_samples'] and not args[2]
        return "published"
    raise AssertionError(action)


@activity.defn(name="check_pool")
def check_pool(root, request_id, output):
    return dict(state="ready", path=request_id)


@workflow.defn(name="SampleWorkflow")
class WaitingSample:
    def __init__(self):
        self.released = False
        self.computed = False

    @workflow.signal
    def release(self):
        self.released = True

    @workflow.signal
    def compute_ready(self):
        self.computed = True

    @workflow.run
    async def run(self, spec, entry, parent, notify_computed=False):
        if notify_computed:
            await workflow.wait_condition(lambda: self.computed or self.released)
            owner = workflow.info().parent
            await workflow.get_external_workflow_handle(owner.workflow_id, run_id=owner.run_id).signal(
                'sample_computed', entry['sample_id'])
        await workflow.wait_condition(lambda: self.released)
        return entry['sample_id']


async def check(endpoint):
    from concurrent.futures import ThreadPoolExecutor
    runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=None)
    client = await Client.connect(endpoint, runtime=runtime)
    identity = 'capacity-check-' + uuid.uuid4().hex
    async def exists(suffix):
        for _ in range(100):
            try:
                info = await client.get_workflow_handle(identity + suffix).describe()
                assert info.status.name == 'RUNNING'
                return
            except Exception:
                await asyncio.sleep(.1)
        raise AssertionError('Child never started: ' + suffix)
    with ThreadPoolExecutor(max_workers=4) as executor:
        def worker():
            return Worker(client, task_queue=identity, workflows=[PersampleWorkflow, WaitingSample],
                activities=[sample_step, check_pool], activity_executor=executor,
                workflow_runner=UnsandboxedWorkflowRunner())
        async with worker():
            handle = await client.start_workflow(PersampleWorkflow.run,
                dict(batch_size=1, max_in_flight_samples=1, pool_root='test'), id=identity, task_queue=identity)
            await exists('/sample-0')
            try:
                await handle.execute_update(PersampleWorkflow.set_in_flight_limit, 0)
                raise AssertionError('Invalid capacity was accepted')
            except WorkflowUpdateFailedError:
                pass
            assert await handle.execute_update(PersampleWorkflow.set_in_flight_limit, 2) == 2
            await exists('/sample-1')  # First sample is still waiting: admission must wake now.
            assert (await client.get_workflow_handle(identity + '/sample-0').describe()).status.name == 'RUNNING'
        async with worker():
            assert await handle.query(PersampleWorkflow.in_flight_limit) == 2
            # Lowering capacity must not cancel admitted work.
            assert await handle.execute_update(PersampleWorkflow.set_in_flight_limit, 1) == 1
            for index in range(2):
                await client.get_workflow_handle(identity + '/sample-' + str(index)).signal('release')
            assert await asyncio.wait_for(handle.result(), 20) == 'published'
        await Replayer(workflows=[PersampleWorkflow],
                       workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(await handle.fetch_history())
        identity += '-separate'
        async with worker():
            handle = await client.start_workflow(PersampleWorkflow.run,
                dict(batch_size=1, max_in_flight_samples=1, max_prepared_samples=2,
                     test_samples=3, pool_root='test'), id=identity, task_queue=identity)
            await exists('/sample-0')
            await client.get_workflow_handle(identity + '/sample-0').signal('compute_ready')
            await exists('/sample-1')
            assert (await client.get_workflow_handle(identity + '/sample-0').describe()).status.name == 'RUNNING'
            await client.get_workflow_handle(identity + '/sample-1').signal('compute_ready')
            await asyncio.sleep(.5)
            from temporalio.service import RPCError, RPCStatusCode
            try:
                await client.get_workflow_handle(identity + '/sample-2').describe()
                raise AssertionError('Prepared backlog bound was exceeded')
            except RPCError as exc:
                assert exc.status == RPCStatusCode.NOT_FOUND
            await client.get_workflow_handle(identity + '/sample-0').signal('release')
            await exists('/sample-2')
            for index in (1, 2):
                await client.get_workflow_handle(identity + '/sample-' + str(index)).signal('release')
            assert await asyncio.wait_for(handle.result(), 20) == 'published'
        await Replayer(workflows=[PersampleWorkflow],
                       workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(await handle.fetch_history())
    print(identity + ': admission wakeup, restart, replay, compute/model separation and backlog bound passed')


if __name__ == '__main__':
    asyncio.run(check(sys.argv[1]))
