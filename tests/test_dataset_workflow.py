import asyncio

import pytest

from ecarsi.agent.session import reference
from ecarsi.control.dataset import AnalysisUnitWorkflow, dataset_step
from ecarsi.warm_pool.state import save


def test_organize_resume_accepts_relocated_publication_but_rejects_changed_data(tmp_path, monkeypatch):
    from tests.test_organize_v2_publish import outputs
    from ecarsi.stages.organize import publish
    from ecarsi import layout as L
    import ecarsi.control.coordinator as coordinator

    output, destination = outputs(tmp_path)
    publish(output, destination)
    spec = dict(output_root=str(destination), pool_root=str(tmp_path), run_id='organize')
    monkeypatch.setattr(coordinator, 'check_pool', lambda *args: {
        'state': 'ready', 'path': str(output / 'completion.json')})
    assert dataset_step('completed', ['organize', spec]) == str(destination)
    monkeypatch.setattr(coordinator, 'check_pool', lambda *args: (_ for _ in ()).throw(KeyError(args[1])))
    assert dataset_step('completed', ['organize', spec]) == str(destination)   # Pool folder pruned
    monkeypatch.setattr(coordinator, 'check_pool', lambda *args: {
        'state': 'ready', 'path': str(output / 'completion.json')})
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


def test_next_round_does_not_repeat_organize_or_per_sample(monkeypatch):
    import ecarsi.control.dataset as module

    async def scenario():
        stages = []
        async def call(fn, action, args):
            if action == 'stage':
                stages.append((args[2], args[4]))
                return {'run_id': args[2]}
            assert action == 'round'
            return {'publication': 'completed.json'}
        async def child(fn, spec, **kwargs):
            return spec['run_id'] + '.json'
        monkeypatch.setattr(module, 'call', call)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
        monkeypatch.setattr(module.workflow, 'patched', lambda name: False)
        progress = dict(per_sample='samples.json', input='previous-zoom.json', stats=[{}], rounds=[{}])
        assert await AnalysisUnitWorkflow().run({}, {}, progress) == 'completed.json'
        assert stages == [('cross_sample', 2), ('zoom_in', 2)]
    asyncio.run(scenario())


def test_resume_reuses_complete_stages_and_only_starts_unfinished_zoom(monkeypatch):
    import ecarsi.control.dataset as module

    async def scenario():
        started = []
        async def call(fn, action, args):
            if action == 'resume_stage':
                return {'run_id': args[2]}
            if action == 'completed':
                return None if args[0] == 'zoom_in' else args[0] + '.json'
            assert action == 'round'
            return {'publication': 'complete.json'}
        async def child(fn, spec, **kwargs):
            started.append(kwargs['id'])
            return 'zoom.json'
        monkeypatch.setattr(module, 'call', call)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
        monkeypatch.setattr(module.workflow, 'patched', lambda name: False)
        assert await AnalysisUnitWorkflow().run({}, {}, resume=True) == 'complete.json'
        assert started == ['zoom-in/zoom_in']
    asyncio.run(scenario())


def test_dataset_publication_preserves_incomplete_revision_and_seals_success(tmp_path):
    from ecarsi.warm_pool.state import read, digest
    spec = dict(output_root=str(tmp_path), dataset_id='test')
    path = dataset_step('publish', [spec, [], [{'unit': 'U', 'error': 'failed'}]])
    previous = read(path)
    dataset_step('publish', [spec, [], []])
    assert read(tmp_path / ('publication-' + digest(previous) + '.json')) == previous
    assert read(path)['state'] == 'complete'
    with pytest.raises(ValueError, match='completed publication'):
        dataset_step('publish', [spec, [], [{'unit': 'U', 'error': 'failed'}]])


def test_unit_waits_for_accepted_pool_release_before_completing(monkeypatch):
    import ecarsi.control.dataset as module
    actions = []
    async def call(fn, action, args):
        actions.append(action)
        if action == 'stage':
            return {'run_id': args[2]}
        if action == 'round':
            return {'publication': 'unit.json'}
        if action == 'release':
            assert args[1] == 'unit.json'
            return {'id': 'release', 'output': 'released.json'}
        if action == 'round-ledger':
            return {'id': 'ledger', 'output': 'ledger.json'}
        if action == 'round-ledger-published':
            return 'ledger'
        if action == 'pause-after-stage':
            return None                      # nothing in loop_control.json asks for a stop
        assert action == 'released' and args == ['unit.json', 'result.json']
    async def child(*args, **kwargs):
        return 'stage.json'
    async def pool(spec, request):
        actions.append('await release')
        return 'result.json'
    monkeypatch.setattr(module, 'call', call)
    monkeypatch.setattr(module, 'await_pool', pool)
    monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
    monkeypatch.setattr(module.workflow, 'patched', lambda name: True)
    progress = dict(per_sample='per.json', input='zoom.json', stats=[{}], rounds=[{}])
    assert asyncio.run(AnalysisUnitWorkflow().run({}, {}, progress)) == 'unit.json'
    assert actions[-3:] == ['release', 'await release', 'released']


def test_completed_stage_requires_same_input_spec_and_accepted_result(tmp_path, monkeypatch):
    import ecarsi.control.coordinator as coordinator
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
    monkeypatch.setattr(coordinator, 'check_pool', lambda *args: {'state': 'ready', 'path': str(output)})
    assert dataset_step('completed', ['zoom_in', spec]) == str(root / 'publication.json')
    def pruned(*args):
        raise KeyError(args[1])
    monkeypatch.setattr(coordinator, 'check_pool', pruned)   # the run's Pool folders were pruned after it finished
    assert dataset_step('completed', ['zoom_in', spec]) == str(root / 'publication.json')
    save(output, {**bundle, 'n_survived': 3})
    with pytest.raises(ValueError):
        dataset_step('completed', ['zoom_in', spec])
    save(output, bundle)
    monkeypatch.setattr(coordinator, 'check_pool', lambda *args: {'state': 'ready', 'path': str(output)})
    with pytest.raises(ValueError, match='specification changed'):
        dataset_step('completed', ['zoom_in', {**spec, 'config': {'changed': True}}])
    save(output, {**bundle, 'n_survived': 3})
    with pytest.raises(ValueError, match='no longer accepted'):
        dataset_step('completed', ['zoom_in', spec])


def test_resume_rejects_unreconciled_requests_and_audits_new_run(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    import ecarsi.warm_pool.state as pool
    import ecarsi.control.dataset as module
    from temporalio.common import WorkflowIDReusePolicy
    spec = dict(output_root=str(tmp_path), pool_root=str(tmp_path / 'pool'),
                bridge_root=str(tmp_path / 'bridge'), run_id='test')
    save(tmp_path / 'spec.json', spec)
    failed_publication = {'state': 'incomplete', 'failed_units': ['test']}
    save(tmp_path / 'publication.json', failed_publication)
    request = tmp_path / 'pool/requests/test.ledger-1/request.json'   # named `<run_id>.<kind>-…`, as the plane names them
    request.parent.mkdir(parents=True)
    save(request, {'spec': {'trace': {'workflow_id': 'dataset/test'}}})
    class Event:
        workflow_execution_started_event_attributes = NS(input=NS(payloads=[]))
        def HasField(self, name):
            return False
    class Client:
        data_converter = None
        def __init__(self):
            self.data_converter = self
            self.started = []
        def get_workflow_handle(self, *args, **kwargs):
            return self
        async def decode(self, *args):
            return [spec]
        async def describe(self):
            return NS(status=NS(name='FAILED'), run_id='old')
        async def fetch_history(self):
            return NS(events=[Event()])
        async def start_workflow(self, *args, **kwargs):
            self.started.append(kwargs)
            return NS(result_run_id='new')
    client = Client()
    monkeypatch.setattr(pool, 'status', lambda *args: {'state': 'unknown_external_result'})
    with pytest.raises(ValueError, match='Reconcile'):
        asyncio.run(module.resume_dataset(client, 'dataset/test', 'queue', 'confirmed fix'))
    assert not client.started
    monkeypatch.setattr(pool, 'status', lambda *args: {'state': 'succeeded'})
    asyncio.run(module.resume_dataset(client, 'dataset/test', 'queue', 'confirmed fix'))
    assert client.started == [dict(args=[spec, True], id='dataset/test', task_queue='queue',
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)]
    assert len(list((tmp_path / 'recoveries').glob('*.started.json'))) == 1
    from ecarsi.agent.session import verified
    audit = pool.read(next((tmp_path / 'recoveries').glob('*.started.json')))
    intent = verified(audit['intent'])
    assert verified(intent['previous_publication']) == failed_publication
    # The live slot is cleared once its content is safely archived under its own digest, or
    # Periscope reads the old failed_units record for as long as the resumed run takes to
    # finish (2026-09-21: two datasets stayed "failed" 40+ minutes into a clean rerun).
    assert not (tmp_path / 'publication.json').exists()
    save(tmp_path / 'publication.json', {'state': 'complete'})


def test_failed_unit_does_not_cancel_its_running_sibling(monkeypatch):
    import ecarsi.control.dataset as module
    from types import SimpleNamespace
    from temporalio.exceptions import ApplicationError

    async def scenario():
        children, published = [], []

        async def call(fn, action, args):
            if action == 'organize':
                return {'run_id': 'organize'}
            if action == 'units':
                return [dict(name='failed'), dict(name='healthy')]
            assert action == 'publish'
            published.append(args)
            return 'incomplete.json'

        async def organize(*args, **kwargs):
            return 'organized'

        async def child(*args, **kwargs):
            future = asyncio.get_running_loop().create_future()
            if not children:
                future.set_exception(RuntimeError('scientific failure'))
            children.append(future)
            return future

        async def wait(pending, **kwargs):
            if len(pending) == 1:
                assert not children[1].cancelled()
                children[1].set_result('healthy.json')
            return await asyncio.wait(pending, **kwargs)

        monkeypatch.setattr(module, 'call', call)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', organize)
        monkeypatch.setattr(module.workflow, 'start_child_workflow', child)
        monkeypatch.setattr(module.workflow, 'wait', wait)
        monkeypatch.setattr(module.workflow, 'info', lambda: SimpleNamespace(workflow_id='dataset/test'))
        with pytest.raises(ApplicationError, match='Analysis units failed'):
            await module.DatasetWorkflow().run({})
        assert published == [[{}, ['healthy.json'], [{'unit': 'failed', 'error': 'scientific failure'}]]]

    asyncio.run(scenario())
