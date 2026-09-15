import asyncio

import pytest

from ecarsi.agent_session import reference
from ecarsi.dataset_workflow import AnalysisUnitWorkflow, dataset_step
from ecarsi.warm_pool.state import save


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
            from ecarsi.agent_session import verified
            publication = verified(reference(updated['publication']))
            assert publication['n_input'] == publication['n_survived'] + publication['n_removed'] == 2000
            assert publication['n_removed'] == 170 and not publication['forced_release']
            # A downstream count consistent with itself but detached from its upstream must fail.
            save(zoom, dict(state='complete', input=reference(cross), n_input=3000, n_survived=2990, n_removed=10))
            with pytest.raises(ValueError, match='survivor chain'):
                dataset_step('round', [spec, unit, progress, str(cross), str(zoom)])
        progress = updated


def test_next_round_does_not_repeat_organize_or_per_sample(monkeypatch):
    import ecarsi.dataset_workflow as module

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
        progress = dict(per_sample='samples.json', input='previous-zoom.json', stats=[{}], rounds=[{}])
        assert await AnalysisUnitWorkflow().run({}, {}, progress) == 'completed.json'
        assert stages == [('cross_sample', 2), ('zoom_in', 2)]
    asyncio.run(scenario())


def test_failed_unit_does_not_cancel_its_running_sibling(monkeypatch):
    import ecarsi.dataset_workflow as module
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
