"""Resuming a workflow keeps it on the queue its coordinators poll.

2026-09-16: a dataset resumed from the CLI without --task-queue landed on the package default
queue, which no coordinator served; its first workflow task sat unclaimed for 15 minutes.
"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from ecarsi import dataset_workflow
from ecarsi.warm_pool.state import save


class FakeClient:
    def __init__(self, status, queue, spec):
        self.status, self.queue, self.spec, self.started = status, queue, spec, None
        self.data_converter = NS(decode=self._decode)

    async def _decode(self, payloads):
        return [self.spec]

    def get_workflow_handle(self, identity, run_id=None):
        started = NS(input=NS(payloads=[]), task_queue=NS(name=self.queue))
        event = NS(workflow_execution_started_event_attributes=started, HasField=lambda name: False)

        async def describe():
            return NS(status=NS(name=self.status), run_id='run-1')

        async def fetch_history():
            return NS(events=[event])
        return NS(describe=describe, fetch_history=fetch_history)

    async def start_workflow(self, run, args, id, task_queue, id_reuse_policy):
        self.started = task_queue
        return NS(result_run_id='run-2', id=id)


def make_spec(tmp_path):
    for name in ('pool', 'bridge'):
        (tmp_path / name / 'requests').mkdir(parents=True)
    (tmp_path / 'root').mkdir()
    spec = dict(run_id='ds', dataset_id='d', output_root=str(tmp_path / 'root'),
                pool_root=str(tmp_path / 'pool'), bridge_root=str(tmp_path / 'bridge'))
    save(tmp_path / 'root' / 'spec.json', spec)
    return spec


@pytest.mark.parametrize('status', ['FAILED', 'TERMINATED', 'CANCELED'])
def test_resume_defaults_to_the_previous_runs_queue(tmp_path, status):
    client = FakeClient(status, 'ecarsi-site-queue', make_spec(tmp_path))
    asyncio.run(dataset_workflow.resume_dataset(client, 'dataset/ds', None, 'operator asked'))
    assert client.started == 'ecarsi-site-queue'


def test_resume_explicit_queue_wins_and_running_is_refused(tmp_path):
    spec = make_spec(tmp_path)
    client = FakeClient('FAILED', 'old', spec)
    asyncio.run(dataset_workflow.resume_dataset(client, 'dataset/ds', 'new', 'moved'))
    assert client.started == 'new'
    with pytest.raises(ValueError, match='failed, terminated'):
        asyncio.run(dataset_workflow.resume_dataset(FakeClient('RUNNING', 'old', spec), 'dataset/ds', None, 'x'))
