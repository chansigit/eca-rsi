"""A dead agent session restarts once as a fresh session; a second death skips the sample or lineage."""
import asyncio
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from ecarsi.agent.session import reference
from ecarsi.warm_pool.state import read, save


def test_restart_names_a_fresh_session_and_records_the_superseded_one(tmp_path):
    from ecarsi.control.coordinator import agent_step
    spec = dict(session_id='osp-abc', output_root=str(tmp_path / 'agent-1'), prompt='p')
    fresh = agent_step('restart', [spec, 'turn-3: failed'])
    assert fresh['session_id'] == 'osp-abc-r2' and fresh['output_root'] == str(tmp_path / 'agent-1' / 'restart')
    assert read(tmp_path / 'agent-1' / 'restart.json')['superseded'] == 'osp-abc'
    assert agent_step('restart', [spec, 'turn-3: failed']) == fresh  # an activity retry lands on the same record


def test_run_agent_tries_a_second_session_once(monkeypatch):
    import ecarsi.control.coordinator as module

    async def scenario(failures):
        attempts, restarts = [], []

        async def child(fn, spec, id):
            attempts.append((spec['session_id'], id))
            if len(attempts) <= failures:
                raise RuntimeError('session died')
            return 'result'

        async def call(fn, action, args):
            assert action == 'restart'
            restarts.append(args[1])
            return dict(args[0], session_id=args[0]['session_id'] + '-r2')
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
        return await module.run_agent(dict(session_id='s'), 'wf/s', call), attempts, restarts
    result, attempts, restarts = asyncio.run(scenario(1))
    assert result == 'result' and attempts == [('s', 'wf/s'), ('s-r2', 'wf/s/restart')] and restarts == ['session died']
    with pytest.raises(RuntimeError, match='session died'):
        asyncio.run(scenario(2))


def test_sample_whose_sessions_died_is_finalized_unannotated(monkeypatch):
    import ecarsi.control.persample as module

    async def scenario():
        events = []

        async def call(fn, action, args):
            events.append(action)
            if action == 'compute':
                return dict(id='compute', output='computed.json')
            if action == 'read':
                return dict(empty=False)
            if action == 'agent':
                return dict(session_id='osp-s')
            if action == 'restart':
                return dict(args[0], session_id='osp-s-r2')
            if action == 'skipped':
                assert 'osp-s-r2' in args[2]
                return 'skipped.json'
            if action == 'finalize':
                assert args[2] is None and args[3] == 'compute'
                return dict(id='finalize', output='final.json')
            raise AssertionError(action)

        async def await_pool(spec, request):
            return request['id']

        async def child(fn, spec, id):
            raise RuntimeError('session died: ' + spec['session_id'])
        monkeypatch.setattr(module, 'call', call)
        monkeypatch.setattr(module, 'await_pool', await_pool)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
        monkeypatch.setattr(module.workflow, 'info', lambda: SimpleNamespace(workflow_id='persample/r/sample-0'))
        return await module.SampleWorkflow().run({}, dict(sample_id='s'), 'partition'), events
    result, events = asyncio.run(scenario())
    assert result == 'finalize' and events == ['compute', 'read', 'agent', 'restart', 'skipped', 'finalize']


def test_publication_lists_skipped_samples_and_fails_past_the_limit(tmp_path):
    from ecarsi.control.persample import sample_step
    save(tmp_path / 'input.json', {})

    def final(root, name, cells, annotated):
        path = root / (name + '.json')
        save(path, dict(sample=name, empty=False, annotation={'path': 'a'} if annotated else None,
                        validation=dict(n_input=cells, n_survived=cells, n_removed=0)))
        return str(path)

    def publish(root, sizes):
        root.mkdir()
        spec = dict(output_root=str(root), input_manifest=reference(tmp_path / 'input.json'))
        save(root / 'computed-b.json', dict(sample='b', validation=dict(n_survived=sizes['b'])))
        sample_step('skipped', [spec, str(root / 'computed-b.json'), 'session died twice'])
        totals = dict(total_samples=2, n_input=sum(sizes.values()), n_excluded=0, exclusions=reference(tmp_path / 'input.json'))
        return read(sample_step('publish', [spec, [final(root, 'a', sizes['a'], True), final(root, 'b', sizes['b'], False)], [], totals]))
    kept = publish(tmp_path / 'small', dict(a=95, b=5))
    assert kept['state'] == 'complete' and kept['failed_samples'] == []
    assert kept['skipped_samples'] == [dict(sample='b', n_cells=5, error='session died twice')]
    lost = publish(tmp_path / 'large', dict(a=80, b=20))
    assert lost['state'] == 'incomplete' and [f['sample'] for f in lost['failed_samples']] == ['b']
    assert '10%' in lost['failed_samples'][0]['error']


@pytest.mark.parametrize('cells,outcome', [(10, 'merged'), (20, 'failed')])
def test_lineage_whose_sessions_died_keeps_its_labels_within_the_limit(monkeypatch, cells, outcome):
    import ecarsi.control.zoomin as module

    async def scenario():
        requests, events = {}, []

        async def call(fn, action, args):
            if action in ('read', 'session'):
                path = args[0]
                if path == 'lineage-decision':
                    return {'evidence': {'path': 'evidence'}}
                if path == 'plan-decision':
                    return {'proposal': {'lineages': [dict(name='A', zoom=True, n_cells=100 - cells), dict(name='B', zoom=True, n_cells=cells)]}}
                if path.startswith('compute'):
                    return {'tasks': []}
                if path.startswith('agent'):
                    return {'session_id': path}
            if action == 'restart':
                return dict(args[0], session_id=args[0]['session_id'] + '-r2')
            if action == 'accepted':
                return {'path': args[0], 'parent': args[0]}
            if action == 'publish':
                return 'publication'
            payload = args[1]
            identifier = action + '-' + str(len(requests))
            requests[identifier] = (action, payload)
            return {'id': identifier, 'output': identifier}

        async def await_pool(spec, request):
            events.append(requests[request['id']])
            return request['id']

        def lineage_index(session_id):
            _, agent = requests[session_id.replace('-r2', '')]
            _, assemble = requests[agent['paths'][0]]
            _, compute = requests[assemble['paths'][0]]
            _, subset = requests[compute['paths'][0]]
            return subset['index']

        async def child(fn, session, id):
            _, payload = requests[session['session_id'].replace('-r2', '')]
            if payload['kind'] == 'plan':
                return 'plan-decision'
            if lineage_index(session['session_id']) == 1:
                raise RuntimeError('session died: ' + session['session_id'])
            return 'lineage-decision'
        monkeypatch.setattr(module, 'call', call)
        monkeypatch.setattr(module, 'await_pool', await_pool)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
        monkeypatch.setattr(module.workflow, 'info', lambda: SimpleNamespace(workflow_id='zoom-test'))
        monkeypatch.setattr(module.workflow, 'patched', lambda name: True)
        monkeypatch.setattr(module.workflow, 'wait', asyncio.wait)
        result = await module.ZoominWorkflow().run(dict(max_in_flight_lineages=1, max_in_flight_deg=2))
        return result, events
    if outcome == 'failed':
        with pytest.raises(ApplicationError, match='over 10%'):
            asyncio.run(scenario())
        return
    result, events = asyncio.run(scenario())
    assert result == 'publication' and [a for a, _ in events].count('apply') == 1
    action, payload = events[-1]
    assert action == 'merge' and len(payload['paths']) == 3
    assert [(s['index'], s['name'], s['n_cells']) for s in payload['skipped']] == [(1, 'B', 10)]
    assert 'session died: agent' in payload['skipped'][0]['error'] and payload['skipped'][0]['error'].endswith('-r2')


def test_plan_without_marks_skipped_lineages_as_not_zoomed():
    from ecarsi.stages.zoomin import plan_without
    plan = {'lineages': [dict(name='A', zoom=True), dict(name='B', zoom=True), dict(name='C', zoom=False, reason='small')]}
    out = plan_without(plan, [dict(name='B', error='died')])
    assert [line['zoom'] for line in out['lineages']] == [True, False, False]
    assert 'died' in out['lineages'][1]['reason'] and out['lineages'][2]['reason'] == 'small' and plan['lineages'][1]['zoom']
    with pytest.raises(ValueError, match='zoomed lineages'):
        plan_without(plan, [dict(name='C', error='x')])


def test_resume_treats_requests_of_superseded_sessions_as_settled(tmp_path):
    from ecarsi.control.dataset import request_session, request_states, superseded_sessions
    from ecarsi.warm_pool.state import submit
    root = tmp_path / 'dataset'
    agent = root / 'units/u/01-per-sample/agent-1'
    for folder in (agent / 'restart', root / 'units/u/rounds/round01/03-zoom-in/zoom-1'):
        folder.mkdir(parents=True)
    save(agent / 'restart.json', dict(superseded='osp-a', spec={}))
    save(agent / 'restart/context-reset-2.json', dict(spec=dict(session_id='osp-a-r2-g2')))
    save(root / 'units/u/rounds/round01/03-zoom-in/zoom-1/context-reset-3.json', dict(spec=dict(session_id='zoom-z-g3')))
    assert superseded_sessions(root) == {'osp-a', 'osp-a-r2', 'zoom-z-g2'}
    assert request_session(dict(request_id='osp-a.turn-3')) == 'osp-a'
    assert request_session(dict(request_id='osp-a-r2.tool-ff')) == 'osp-a-r2'
    assert request_session(dict(request_id='agent-ff', operation_id='agent.call',
                                args=['-m', 'ecarsi.agent.dispatch', 'execute', '/b/requests/osp-a.turn-3/dispatch-1.json'])) == 'osp-a'
    assert request_session(dict(request_id='r.compute-1', operation_id='osp.compute')) is None
    for name in ('pool', 'bridge'):
        service = tmp_path / name
        service.mkdir(mode=0o700)
        (service / 'requests').mkdir()
        save(service / 'config.json', {'runtime': {}})
    trace = dict(workflow_id='persample/r', dataset_id='D', unit_id='osp.tool')
    task = submit(str(tmp_path / 'pool'), dict(request_id='osp-a.tool-1', operation_id='osp.tool', trace=trace,
                                             args=['-c', 'pass'], cpus=1, memory_mb=64, timeout_seconds=10, outputs=['x']))
    save(tmp_path / 'pool/requests/osp-a.tool-1' / task['attempt_id'] / 'receipt.json', {'state': 'failed'})
    with pytest.raises(ValueError, match='Reconcile osp-a.tool-1'):
        request_states(str(tmp_path / 'pool'), str(tmp_path / 'bridge'), {'persample/r'}, set(), {'osp-a'})
    states = request_states(str(tmp_path / 'pool'), str(tmp_path / 'bridge'), {'persample/r'}, {'osp-a'}, {'osp-a'})
    assert states == [dict(service='pool_root', request_id='osp-a.tool-1', state='superseded_session')]


def test_release_lists_skipped_samples_for_review(tmp_path):
    import pandas as pd
    from ecarsi.stages.release import review_items
    save(tmp_path / 'per-sample.json', dict(skipped_samples=[dict(sample='b', n_cells=5, error='session died twice')]))
    unit = dict(per_sample=reference(tmp_path / 'per-sample.json'), rounds=[], forced_release=False)
    exclusions = pd.DataFrame(columns=['round', 'release_stage', 'reason', 'cell_uid'])
    items = review_items(unit, exclusions, [])
    assert [(i.kind, i.scope, i.n_cells) for i in items] == [('agent_skipped', 'b', 5)]
    assert 'session died twice' in items[0].note
