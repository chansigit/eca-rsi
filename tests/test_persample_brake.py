"""The step brake in the per-sample stage (0022): no new sample starts, the running ones finish, the stage ends PAUSED."""
import asyncio

import pytest
from temporalio import workflow
from temporalio.client import WorkflowFailureError

from ecarsi.control.persample import PersampleWorkflow
from tests.temporal_env import QUEUE, fakes, ready, temporal


@workflow.defn(name='SampleWorkflow')
class Sample:
    @workflow.run
    async def run(self, spec, entry, parent, notify_computed=False) -> dict:
        return dict(sample=entry['sample_id'])


SPEC = dict(run_id='r', batch_size=1, max_in_flight_samples=2, output_root='run/units/u/01-per-sample', pool_root='pool')


def test_the_step_brake_admits_no_new_sample_and_lets_the_running_one_finish():
    calls, actions = [], []
    def before(control):
        calls.append(control)
        return dict(task_queue=None, brake='step' if len(calls) > 1 else None)
    def step(action, args):
        actions.append(action)
        if action == 'partition':
            return dict(id='p' + str(args[1]), output='partition.json')
        if action == 'read':
            offset = int(args[0][1:])
            return dict(total_samples=3, next_offset=offset + 1, entries=[dict(sample_id='s' + str(offset))])
        raise AssertionError(action)
    async def main():
        async with temporal([PersampleWorkflow, Sample], fakes(sample_step=step, check_pool=ready, before_child=before)) as client:
            with pytest.raises(WorkflowFailureError) as failure:
                await client.execute_workflow(PersampleWorkflow.run, SPEC, id='persample/r', task_queue=QUEUE)
            first = (await client.get_workflow_handle('persample/r/sample-0').describe()).status.name
            return str(failure.value.cause), first
    message, first = asyncio.run(main())
    assert message.startswith('PAUSED: loop_control brake step stopped the per-sample stage')
    assert first == 'COMPLETED' and calls == ['run/units/u'] * 2
    assert actions.count('partition') == 2 and 'publish' not in actions   # the second batch was read, not started
