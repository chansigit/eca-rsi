"""Resuming a workflow keeps it on the queue its coordinators poll.

2026-09-16: a dataset resumed from the CLI without --task-queue landed on the package default
queue, which no coordinator served; its first workflow task sat unclaimed for 15 minutes.
"""
import asyncio
from types import SimpleNamespace as NS

import pytest

import ecarsi.control.dataset as dataset_workflow
from ecarsi.files import save


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
        self.started, self.policy = task_queue, id_reuse_policy
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


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, value)


def make_finished_run(tmp_path):
    """A released unit with two rounds, as restart-from (#69) finds it."""
    spec = make_spec(tmp_path)
    unit = tmp_path / 'root' / 'units' / 'u'
    for n in (1, 2):
        for stage in ('02-cross-sample', '03-zoom-in'):
            put(unit / 'rounds' / f'round0{n}' / stage / 'spec.json',
                dict(run_id=f'r{n}-{stage}', **{k: spec[k] for k in ('dataset_id', 'pool_root', 'bridge_root')}))
        put(unit / 'rounds' / f'round0{n}' / 'publication.json', dict(round=n))
        (unit / 'rounds' / f'round0{n}' / 'ledger').mkdir()
    put(unit / 'release' / 'summary.json', dict(final_cells=1))
    put(unit / 'publication.json', dict(state='complete'))
    put(tmp_path / 'root' / 'publication.json', dict(state='complete'))
    return spec, unit


def test_restart_from_zoom_in_keeps_that_rounds_cross_sample_and_accepts_a_completed_run(tmp_path):
    from temporalio.common import WorkflowIDReusePolicy
    spec, unit = make_finished_run(tmp_path)
    client = FakeClient('COMPLETED', 'old', spec)
    asyncio.run(dataset_workflow.resume_dataset(client, 'dataset/ds', 'new', 'msp changed', restart=('zoom_in', 1)))
    assert client.started == 'new' and client.policy == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert (unit / 'rounds/round01/02-cross-sample/spec.json').exists(), 'round 1 cross-sample is reused'
    attic = unit / 'attic' / '01'
    for rel in ('rounds/round01/03-zoom-in/spec.json', 'rounds/round01/publication.json', 'rounds/round01/ledger',
                'rounds/round02/02-cross-sample/spec.json', 'release/summary.json', 'publication.json'):
        assert (attic / rel).exists() and not (unit / rel).exists(), rel
    assert not (tmp_path / 'root' / 'publication.json').exists(), 'the dataset publication is archived under its digest'
    from ecarsi.files import read
    records = read(unit / 'restarts.json')
    assert [(r['attempt'], r['stage'], r['round']) for r in records] == [(1, 'zoom_in', 1)]
    # request ids: per-sample and round 1 cross-sample keep theirs, everything from round 1 zoom-in on gets new ones
    assert dataset_workflow.restart_attempt(unit, 'per_sample', 0) == 0
    assert dataset_workflow.restart_attempt(unit, 'cross_sample', 1) == 0
    assert dataset_workflow.restart_attempt(unit, 'zoom_in', 1) == 1
    assert dataset_workflow.restart_attempt(unit, 'cross_sample', 2) == 1
    assert dataset_workflow.restart_count(tmp_path / 'root') == 1


def test_restart_from_cross_sample_moves_the_whole_round_and_a_second_restart_counts_up(tmp_path):
    spec, unit = make_finished_run(tmp_path)
    asyncio.run(dataset_workflow.resume_dataset(FakeClient('COMPLETED', 'old', spec), 'dataset/ds', None, 'first', restart=('zoom_in', 2)))
    asyncio.run(dataset_workflow.resume_dataset(FakeClient('FAILED', 'old', spec), 'dataset/ds', None, 'second', restart=('cross_sample', 1)))
    assert not list((unit / 'rounds').glob('round*')) and (unit / 'attic/02/rounds/round01/02-cross-sample/spec.json').exists()
    assert dataset_workflow.restart_attempt(unit, 'cross_sample', 1) == 1
    assert dataset_workflow.restart_attempt(unit, 'zoom_in', 2) == 2
    assert dataset_workflow.restart_attempt(unit, 'per_sample', 0) == 0


def test_restart_from_refuses_a_run_without_that_stage_and_plain_resume_refuses_completed(tmp_path):
    spec = make_spec(tmp_path)
    put(tmp_path / 'root' / 'units' / 'u' / 'rounds' / 'round01' / '02-cross-sample' / 'spec.json',
        dict(run_id='r', **{k: spec[k] for k in ('dataset_id', 'pool_root', 'bridge_root')}))
    with pytest.raises(ValueError, match='no zoom-in record of round 1'):
        asyncio.run(dataset_workflow.resume_dataset(FakeClient('COMPLETED', 'old', spec), 'dataset/ds', None, 'x', restart=('zoom_in', 1)))
    with pytest.raises(ValueError, match='restart-from also takes a completed one'):
        asyncio.run(dataset_workflow.resume_dataset(FakeClient('COMPLETED', 'old', spec), 'dataset/ds', None, 'x'))
