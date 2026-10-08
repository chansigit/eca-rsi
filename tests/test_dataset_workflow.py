import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from ecarsi.agent.session import reference
from ecarsi.control.dataset import AnalysisUnitWorkflow, DatasetWorkflow, dataset_step
from ecarsi.files import read, save
from tests.temporal_env import QUEUE, fakes, ready, temporal


def finished(pool, request_id, output):
    """A real Pool request whose attempt succeeded with `output`: check_pool reads it as ready."""
    from ecarsi.warm_pool.state import submit
    (pool / 'requests').mkdir(parents=True, exist_ok=True)
    pool.chmod(0o700)
    if not (pool / 'config.json').exists():
        save(pool / 'config.json', {'runtime': {}})
    submit(pool, dict(request_id=request_id, operation_id='test', args=['-c', 'pass'], cpus=1, memory_mb=64,
                      timeout_seconds=30, outputs=[Path(output).name]))
    request = read(pool / 'requests' / request_id / 'request.json')
    save(pool / 'requests' / request_id / request['attempt_id'] / 'receipt.json',
         dict(state='succeeded', outputs=[reference(output)]))


def pruned(pool, request_id):
    """The run's Pool folder pruned after it finished: check_pool raises KeyError."""
    (pool / 'requests' / request_id / 'request.json').unlink()


# The stage children of an analysis unit; each answers with its own workflow id (`<prefix><run_id>`),
# so the unit's 'round' activity shows which children actually ran.
@workflow.defn(name='PersampleWorkflow')
class Persample:
    @workflow.run
    async def run(self, stage: dict) -> str:
        return workflow.info().workflow_id


@workflow.defn(name='CrosssampleWorkflow')
class Crosssample:
    @workflow.run
    async def run(self, stage: dict) -> str:
        return workflow.info().workflow_id


@workflow.defn(name='ZoominWorkflow')
class Zoomin:
    @workflow.run
    async def run(self, stage: dict) -> str:
        return workflow.info().workflow_id


STAGES = [AnalysisUnitWorkflow, Persample, Crosssample, Zoomin]
BUDGET = {'pool_root': 'pool', 'zoom_in': {'merge_budget': {'timeout_seconds': 60}}}


def unit_step(actions, answers=None):
    """dataset_step for one analysis unit: records each action and answers the way a healthy run does."""
    def step(action, args):
        actions.append(action)
        if answers and action in answers:
            return answers[action](args)
        return {'stage': {'run_id': 'stage'}, 'pause-after-stage': None, 'round': {'publication': 'unit.json'},
                'round-ledger': {'id': 'ledger', 'output': 'ledger.json'}, 'round-ledger-published': 'ledger',
                'release': {'id': 'release', 'output': 'released.json'}, 'released': args[0] if args else None,
                'degraded': None}[action]
    return step


async def run_unit(activities, *args):
    async with temporal(STAGES, activities) as client:
        return await client.execute_workflow(AnalysisUnitWorkflow.run, args=list(args), id='unit/test', task_queue=QUEUE)


def test_organize_resume_accepts_relocated_publication_but_rejects_changed_data(tmp_path):
    from tests.test_organize_v2_publish import outputs
    from ecarsi.stages.organize import publish
    from ecarsi import layout as L

    output, destination = outputs(tmp_path)
    publish(output, destination)
    pool = tmp_path / 'pool'
    spec = dict(output_root=str(destination), pool_root=str(pool), run_id='organize')
    finished(pool, 'organize.execute', output / 'completion.json')
    assert dataset_step('completed', ['organize', spec]) == str(destination)
    pruned(pool, 'organize.execute')
    assert dataset_step('completed', ['organize', spec]) == str(destination)   # Pool folder pruned
    finished(pool, 'organize.execute', output / 'completion.json')
    L.input_h5ad(L.unit_dir(destination, 'unit')).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='Organize output changed'):
        dataset_step('completed', ['organize', spec])


def test_round_uses_legacy_convergence_and_conserves_cell_counts(tmp_path):
    policy = dict(rounds=None, cap=10, extra_rounds_after_convergence=0, max_removed=1000)
    spec = dict(output_root=str(tmp_path), round_policy=policy)
    unit = dict(name='U')
    sample = tmp_path/'per-sample.json'
    save(sample, dict(state='complete', n_input=2000, n_survived=1900, n_removed=100))
    progress = dict(per_sample=str(sample), input=str(sample), stats=[], rounds=[])
    for number, cells in ((1, 1850), (2, 1830)):
        directory = tmp_path/'units/U/rounds'/f'round{number:02d}'
        directory.mkdir(parents=True)
        before = 1900 if number == 1 else 1850
        cross, zoom = directory/'cross.json', directory/'zoom.json'
        save(cross, dict(state='complete', input=reference(progress['input']), n_input=before,
                         n_survived=cells+10, n_removed=before-cells-10))
        save(zoom, dict(state='complete', input=reference(cross), n_input=cells+10,
                        n_survived=cells, n_removed=10))
        updated = dataset_step('round', [spec, unit, progress, str(cross), str(zoom)])
        assert ('publication' in updated) == (number == 2)  # Round one never auto-releases.
        if number == 2:
            from ecarsi.agent.session import verified
            publication = verified(reference(updated['publication']))
            assert publication['n_input'] == publication['n_survived'] + publication['n_removed'] == 2000
            assert publication['n_removed'] == 170 and not publication['forced_release']
            # A downstream count consistent with itself but detached from its upstream must fail.
            save(zoom, dict(state='complete', input=reference(cross), n_input=3000, n_survived=2990, n_removed=10))
            with pytest.raises(ValueError, match='survivor chain'):
                dataset_step('round', [spec, unit, progress, str(cross), str(zoom)])
        progress = updated


def test_resume_after_pause_decides_again_and_keeps_earlier_decisions(tmp_path):
    # #15: a resume re-drives every round; 'round' used to decide again and conflict with its record.
    policy = dict(rounds=None, cap=10, extra_rounds_after_convergence=0, max_removed=1000)
    spec, unit, sample = dict(output_root=str(tmp_path), round_policy=policy), dict(name='U'), tmp_path/'per-sample.json'
    save(sample, dict(state='complete', n_input=2000, n_survived=1900, n_removed=100))
    progress = dict(per_sample=str(sample), input=str(sample), stats=[], rounds=[])
    directory = tmp_path/'units/U/rounds/round01'
    directory.mkdir(parents=True)
    cross, zoom, control = directory/'cross.json', directory/'zoom.json', tmp_path/'units/U/loop_control.json'
    save(cross, dict(state='complete', input=reference(str(sample)), n_input=1900, n_survived=1860, n_removed=40))
    save(zoom, dict(state='complete', input=reference(cross), n_input=1860, n_survived=1850, n_removed=10))
    args = [spec, unit, progress, str(cross), str(zoom)]
    save(control, dict(pause=True))
    assert 'paused' in dataset_step('round', args)
    save(control, {})  # the owner clears the stop and resumes
    resumed = dataset_step('round', args)
    assert 'paused' not in resumed and resumed['stats'][-1]['decision'] == 'continue'
    assert len(list(directory.glob('publication-*.json'))) == 1  # the paused record is kept
    save(control, dict(cap=1))  # would force a release now; the stored decision stands
    assert dataset_step('round', args) == resumed


def test_next_round_does_not_repeat_organize_or_per_sample():
    actions, stages = [], []
    def stage(args):
        stages.append((args[2], args[4]))
        return {'run_id': args[2]}
    activities = fakes(dataset_step=unit_step(actions, {'stage': stage, 'round': lambda args: {'publication': 'completed.json'}}),
                       check_pool=lambda *args: ready(*args))
    progress = dict(per_sample='samples.json', input='previous-zoom.json', stats=[{}], rounds=[{}])
    assert asyncio.run(run_unit(activities, BUDGET, {}, progress)) == 'completed.json'
    assert stages == [('cross_sample', 2), ('zoom_in', 2)]


def test_resume_reuses_complete_stages_and_only_starts_unfinished_zoom():
    actions, rounds = [], []
    def round_(args):
        rounds.append(args[3:5])
        return {'publication': 'complete.json'}
    answers = {'resume_stage': lambda args: {'run_id': args[2]},
               'completed': lambda args: None if args[0] == 'zoom_in' else args[0] + '.json', 'round': round_}
    activities = fakes(dataset_step=unit_step(actions, answers), check_pool=lambda *args: ready(*args))
    assert asyncio.run(run_unit(activities, BUDGET, {}, None, True)) == 'complete.json'
    # cross-sample came from its accepted publication; only zoom-in ran, as zoom-in/<its run_id>
    assert rounds == [['cross_sample.json', 'zoom-in/zoom_in']] and 'stage' not in actions


def test_dataset_publication_preserves_incomplete_revision_and_seals_success(tmp_path):
    from ecarsi.files import read, digest
    spec = dict(output_root=str(tmp_path), dataset_id='test')
    path = dataset_step('publish', [spec, [], [{'unit': 'U', 'error': 'failed'}]])
    previous = read(path)
    dataset_step('publish', [spec, [], []])
    assert read(tmp_path / ('publication-' + digest(previous) + '.json')) == previous
    assert read(path)['state'] == 'complete'
    with pytest.raises(ValueError, match='completed publication'):
        dataset_step('publish', [spec, [], [{'unit': 'U', 'error': 'failed'}]])


def test_unit_waits_for_accepted_pool_release_before_completing():
    actions = []
    def released(args):
        assert args == ['unit.json', 'result.json']
        return 'unit.json'
    def release(args):
        assert args[1] == 'unit.json'
        return {'id': 'release', 'output': 'released.json'}
    def pool(pool_root, request_id, output):
        if request_id == 'release':
            actions.append('await release')
        return {'state': 'ready', 'path': 'result.json'}
    activities = fakes(dataset_step=unit_step(actions, {'release': release, 'released': released}), check_pool=pool)
    progress = dict(per_sample='per.json', input='zoom.json', stats=[{}], rounds=[{}])
    # the round ledger needs its budget: without one it used to fail on a KeyError nobody saw (decision 0013)
    assert asyncio.run(run_unit(activities, BUDGET, {}, progress)) == 'unit.json'
    assert 'round-ledger-published' in actions and 'degraded' not in actions
    assert actions[-3:] == ['release', 'await release', 'released']


def test_a_ledger_that_never_runs_does_not_hold_the_unit():
    # #18: the round-ledger wait is bounded (2x its budget); on expiry the unit carries on.
    actions = []
    def pool(pool_root, request_id, output):
        if request_id == 'ledger':
            return {'state': 'waiting'}  # stands for an infeasible request; unbounded, the ledger would publish
        return {'state': 'ready', 'path': 'result.json'}
    activities = fakes(dataset_step=unit_step(actions), check_pool=pool)
    progress = dict(per_sample='per.json', input='zoom.json', stats=[{}], rounds=[{}])
    assert asyncio.run(run_unit(activities, BUDGET, {}, progress)) == 'unit.json'
    assert 'round-ledger-published' not in actions and actions[-2:] == ['release', 'released']
    assert 'degraded' in actions  # the expired ledger is recorded with the run (decision 0013)


def test_completed_stage_requires_same_input_spec_and_accepted_result(tmp_path):
    pool = tmp_path / 'pool'
    output = pool / 'requests' / 'compute' / 'attempt' / 'final.json'
    output.parent.mkdir(parents=True)
    source = tmp_path / 'source.json'
    save(source, {'state': 'complete'})
    bundle = dict(state='complete', input=reference(source), n_input=5, n_removed=1, n_survived=4)
    save(output, bundle)
    spec = dict(output_root=str(tmp_path / 'stage'), pool_root=str(pool), input=reference(source))
    root = tmp_path / 'stage'
    root.mkdir()
    save(root / 'spec.json', spec)
    save(root / 'publication.json', {**bundle, 'result': reference(output)})
    finished(pool, 'compute', output)
    assert dataset_step('completed', ['zoom_in', spec]) == str(root / 'publication.json')
    pruned(pool, 'compute')   # the run's Pool folders were pruned after it finished
    assert dataset_step('completed', ['zoom_in', spec]) == str(root / 'publication.json')
    save(output, {**bundle, 'n_survived': 3})
    with pytest.raises(ValueError):
        dataset_step('completed', ['zoom_in', spec])
    save(output, bundle)
    finished(pool, 'compute', output)
    with pytest.raises(ValueError, match='specification changed'):
        dataset_step('completed', ['zoom_in', {**spec, 'config': {'changed': True}}])
    save(output, {**bundle, 'n_survived': 3})
    with pytest.raises(ValueError, match='no longer accepted'):
        dataset_step('completed', ['zoom_in', spec])


@workflow.defn(name='DatasetWorkflow')
class FailsUnlessResumed:
    """The failed run resume_dataset recovers: it fails, and only a resumed run (resume=True) succeeds."""
    @workflow.run
    async def run(self, spec: dict, resume: bool = False) -> str:
        if not resume:
            raise ApplicationError('scientific failure', non_retryable=True)
        return 'resumed'


def test_resume_rejects_unreconciled_requests_and_audits_new_run(tmp_path):
    from ecarsi.warm_pool.state import submit
    import ecarsi.control.dataset as module
    pool = tmp_path / 'pool'
    (pool / 'requests').mkdir(parents=True)
    pool.chmod(0o700)
    save(pool / 'config.json', {'runtime': {}})
    spec = dict(output_root=str(tmp_path), pool_root=str(pool), bridge_root=str(tmp_path / 'bridge'), run_id='test')
    save(tmp_path / 'spec.json', spec)
    failed_publication = {'state': 'incomplete', 'failed_units': ['test']}
    save(tmp_path / 'publication.json', failed_publication)
    # named `<run_id>.<kind>-…`, as the plane names them, and traced to the dataset workflow
    submit(pool, dict(request_id='test.ledger-1', operation_id='dataset.round-ledger', args=['-c', 'pass'], cpus=1,
                      memory_mb=64, timeout_seconds=30, outputs=['ledger.json'],
                      trace=dict(workflow_id='dataset/test', dataset_id='test', unit_id='dataset.round-ledger')))
    folder = pool / 'requests' / 'test.ledger-1'
    save(folder / 'backend.json', dict(state='unknown_external_result'))

    async def scenario():
        async with temporal([FailsUnlessResumed], []) as client:
            with pytest.raises(WorkflowFailureError):
                await client.execute_workflow(FailsUnlessResumed.run, spec, id='dataset/test', task_queue=QUEUE)
            with pytest.raises(ValueError, match='Reconcile'):
                await module.resume_dataset(client, 'dataset/test', QUEUE, 'confirmed fix')
            assert not list((tmp_path / 'recoveries').glob('*.started.json'))  # nothing started
            request = read(folder / 'request.json')
            save(folder / request['attempt_id'] / 'receipt.json', dict(state='succeeded', outputs=[]))
            handle = await module.resume_dataset(client, 'dataset/test', QUEUE, 'confirmed fix')
            return await handle.result()   # the new run received [spec, True]
    assert asyncio.run(scenario()) == 'resumed'
    assert len(list((tmp_path / 'recoveries').glob('*.started.json'))) == 1
    from ecarsi.agent.session import verified
    audit = read(next((tmp_path / 'recoveries').glob('*.started.json')))
    intent = verified(audit['intent'])
    assert verified(intent['previous_publication']) == failed_publication
    assert [r['request_id'] for r in intent['requests']] == ['test.ledger-1']
    # The live slot is cleared once its content is safely archived under its own digest, or
    # Periscope reads the old failed_units record for as long as the resumed run takes to
    # finish (2026-09-21: two datasets stayed "failed" 40+ minutes into a clean rerun).
    assert not (tmp_path / 'publication.json').exists()


@workflow.defn(name='OrganizeWorkflow')
class Organize:
    @workflow.run
    async def run(self, stage: dict) -> str:
        return 'organized'


@workflow.defn(name='AnalysisUnitWorkflow')
class Unit:
    """'failed' fails at once; 'healthy' finishes later, unless the failure cancelled it."""
    @workflow.run
    async def run(self, spec: dict, unit: dict, progress=None, resume: bool = False) -> str:
        if unit['name'] == 'failed':
            raise ApplicationError('scientific failure', non_retryable=True)
        await workflow.sleep(timedelta(minutes=5))
        return 'healthy.json'


def test_failed_unit_does_not_cancel_its_running_sibling():
    published = []
    def step(action, args):
        if action == 'organize':
            return {'run_id': 'organize'}
        if action == 'units':
            return [dict(name='failed'), dict(name='healthy')]
        assert action == 'publish'
        published.append(args)
        return 'incomplete.json'

    async def scenario():
        async with temporal([DatasetWorkflow, Organize, Unit], fakes(dataset_step=step)) as client:
            with pytest.raises(WorkflowFailureError) as failure:
                await client.execute_workflow(DatasetWorkflow.run, {}, id='dataset/test', task_queue=QUEUE)
            return failure.value.cause
    cause = asyncio.run(scenario())
    assert isinstance(cause, ApplicationError) and 'Analysis units failed' in cause.message
    (spec, results, failures), = published
    assert spec == {} and results == ['healthy.json'] and [f['unit'] for f in failures] == ['failed']
    assert failures[0]['error'] == 'scientific failure'  # the cause, not 'Child Workflow execution failed'


# Decision 0022: every child workflow starts on the current version's queue, and the step brake.

def test_stage_children_start_on_the_queue_before_child_answers(tmp_path):
    """The unit runs on QUEUE; `before_child` names 'tests-b' (the current version with a coordinator), so every
    stage child runs there. None would keep them on QUEUE, as Temporal does by default."""
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker
    actions, asked = [], []
    def before(control):
        asked.append(control)
        return dict(task_queue='tests-b', brake=None)
    async def main():
        async with temporal([AnalysisUnitWorkflow], fakes(dataset_step=unit_step(actions), check_pool=ready, before_child=before)) as client:
            async with Worker(client, task_queue='tests-b', workflows=[Persample, Crosssample, Zoomin],
                              workflow_runner=UnsandboxedWorkflowRunner()):
                await client.execute_workflow(AnalysisUnitWorkflow.run, args=[BUDGET | {'output_root': 'run'}, {'name': 'u'}],
                                              id='unit/test', task_queue=QUEUE)
            return {wid: (await client.get_workflow_handle(wid).describe()).task_queue
                    for wid in ('persample/stage', 'cross-sample/stage', 'zoom-in/stage')}
    queues = asyncio.run(main())
    assert set(queues.values()) == {'tests-b'}, queues
    assert asked == ['run/units/u'] * 4   # three children and the release step read the unit's loop_control


def test_the_step_brake_ends_the_unit_before_its_next_child(tmp_path):
    actions, calls = [], []
    def before(control):
        calls.append(control)
        return dict(task_queue=None, brake='step' if len(calls) > 1 else None)
    with pytest.raises(WorkflowFailureError) as failure:
        asyncio.run(run_unit(fakes(dataset_step=unit_step(actions), check_pool=ready, before_child=before),
                             BUDGET | {'output_root': 'run'}, {'name': 'u'}))
    assert str(failure.value.cause).startswith('PAUSED: loop_control brake step')
    assert actions == ['stage', 'stage']   # per-sample ran; cross-sample was prepared but never started (no 'round')


@workflow.defn(name='Sleeper')
class Sleeper:
    @workflow.run
    async def run(self):
        await workflow.wait_condition(lambda: False)


def test_the_hard_brake_terminates_a_running_dataset_and_refuses_a_closed_one():
    from ecarsi.control.dataset import hard_brake
    async def main():
        async with temporal([Sleeper], []) as client:
            await client.start_workflow(Sleeper.run, id='dataset/sleepy', task_queue=QUEUE)
            answer = await hard_brake(client, 'dataset/sleepy', 'owner asked')
            status = (await client.get_workflow_handle('dataset/sleepy').describe()).status.name
            with pytest.raises(ValueError, match='not running'):
                await hard_brake(client, 'dataset/sleepy', 'again')
            return answer, status
    answer, status = asyncio.run(main())
    assert status == 'TERMINATED' and answer['terminated'] and answer['resume'].startswith('resume-dataset sleepy')
