from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from ecarsi.bridge.parallel import budget, choose, eligible, merge_states
import ecarsi.bridge.session as session
from ecarsi.warm_pool.state import immutable, reference, verified
from ecarsi.warm_pool.state import read, save


def test_merge_retains_all_observations_and_rejects_scientific_mutations():
    base = dict(evidence={'sha256': 'original'}, read=['old'], qc=False, lookups=[])
    a, b = deepcopy(base), deepcopy(base)
    a['read'].append('a.png');a['lookups'].append({'query': 'A'})
    b['read'].append('b.png');b['qc'] = True
    merged = merge_states(base, [b, a])
    assert set(merged['read']) == {'old', 'a.png', 'b.png'} and merged['qc']
    assert merged['lookups'] == [{'query': 'A'}] and base['read'] == ['old']
    for bad in [dict(a, evidence={}), dict(a, read=[]), dict(a, types={'accepted': True}), dict(a, qc=None)]:
        with pytest.raises(ValueError):
            merge_states(base, [bad])
    osp = dict(version=3, seen=dict(figures=[], tables=[], genes=False, qc=False))
    after = deepcopy(osp);after['seen'].update(figures=['a.png'], genes=True)
    assert merge_states(osp, [after])['seen']['genes']
    after['version'] = 4
    with pytest.raises(ValueError):
        merge_states(osp, [after])


def test_parallel_handoff_preserves_all_results_and_next_turn_state(tmp_path):
    from tests.test_agent_session import setup, Client, ScriptedModel, completed_tool, execute_turn
    from harness_bridge import _harness_openai as adapter
    from agents.models.interface import ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall
    class Model(ScriptedModel):
        async def get_response(self, **kwargs):
            self.inputs.append(kwargs['input'])
            return ModelResponse(output=[ResponseFunctionToolCall(type='function_call', name='read_evidence',
                call_id=f'c{len(self.inputs)}-{i}', arguments='{"value":'+str(i)+'}', id=f'c{len(self.inputs)}-{i}', status='completed')
                for i in range(18)], usage=Usage(requests=1, input_tokens=10, output_tokens=4), response_id='r')
    spec, _ = setup(tmp_path)
    base = immutable(tmp_path/'initial.json', dict(phase='type', read=[], qc=False, lookups=[], evidence={}))
    tool = dict(spec['tools'][0], name='read_evidence', read_only=True, memory_mb=49152,
                args=['-m','ecarsi.stages.crosssample','tool','read_evidence','{state}','{arguments}'])
    spec = dict(spec, session_id='parallel', output_root=str(tmp_path/'parallel'), tools=[tool], tool_state=base)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    ref = session.create_session(spec)
    with patch.object(adapter, '_client', return_value=Client()), patch.object(adapter, '_model', return_value=Model()):
        reply = execute_turn(root, session.submit_turn(ref, 0))
        assert eligible(spec, session.turn_reply(ref, reply)['calls'])
        assert choose(ref, reply)
        accepted = []
        for i in range(18):
            item = session.tool_request(ref, reply, i)
            request = read(Path(spec['pool_root'])/'requests'/item['request_id']/'request.json')['spec']
            assert base in request['inputs'] and request['memory_mb'] == 2048
            state = immutable(tmp_path/f'fork-{i}.json', dict(verified(base), read=[str(i)]))
            accepted.append(completed_tool(spec, item, {'text': str(i), 'state': state}))
        assert choose(ref, reply)  # Restart retains its original parallel policy.
        with pytest.raises(ValueError, match='Missing or reordered'):
            session.continuation(ref, reply, accepted[:-1], parallel=True)
        context = session.continuation(ref, reply, accepted, parallel=True)
        merged = verified(context)['tool_state']
        assert verified(merged)['read'] == list(map(str, range(18)))
        second = execute_turn(root, session.submit_turn(ref, 1, context))
        item = session.tool_request(ref, second, 0)
        request = read(Path(spec['pool_root'])/'requests'/item['request_id']/'request.json')['spec']
        assert merged in request['inputs']
        assert not choose(ref, second)  # An old ordered partial batch stays ordered.
        # Generic read-only programs and scientific mutations stay ordered.
        assert not eligible(dict(spec, tools=[dict(tool, args=['arbitrary'])]), session.turn_reply(ref, reply)['calls'])
        assert not eligible(dict(spec, tools=[dict(tool, read_only=False)]), session.turn_reply(ref, reply)['calls'])


def test_matrix_budget_uses_compute_receipt_and_keeps_prior_request(tmp_path):
    pool = tmp_path/'pool'
    output = pool/'requests/compute/attempt/outputs/prepared.json'
    output.parent.mkdir(parents=True);save(output, {})
    computed = reference(output)
    save(output.parent.parent/'receipt.json', dict(state='succeeded', peak_rss_bytes=2**30, outputs=[computed]))
    evidence = immutable(tmp_path/'evidence.json', dict(prepared=computed))
    state = immutable(tmp_path/'state.json', dict(evidence=evidence, phase='type'))
    tool = dict(name='check_genes', args=['-m','ecarsi.stages.crosssample','tool','check_genes','{state}','{arguments}'])
    req = dict(request_id='t', args=tool['args'], memory_mb=49152, inputs=[])
    (tmp_path/'new').mkdir()
    result = budget(req, tmp_path/'new', {'pool_root':str(pool)}, tool, state)
    assert result['memory_mb'] == 3072
    assert budget(req, tmp_path/'new', {'pool_root':str(pool)}, tool, state) == result
    path = pool/'requests/old/request.json';path.parent.mkdir();save(path, {})
    old = dict(req, request_id='old')
    assert budget(old, tmp_path/'old', {'pool_root':str(pool)}, tool, state) == old
