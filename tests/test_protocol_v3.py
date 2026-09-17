"""Protocol v3: the same numerics as v2 behind a contract the model can satisfy first time.

Measured 2026-09-16: 13.9 % of all model turns were decision submissions the host rejected.
"""
import json
from pathlib import Path

import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from ecarsi import crosssample_v3, zoomin_v3
from ecarsi.warm_pool.state import read, save


def fake_session(program):
    tools = []
    for name in ('list_evidence', 'read_evidence', 'deg_lookup', 'submit_quality', 'submit_types', 'submit_plan', 'submit_decision'):
        if name == 'deg_lookup':
            fields = {'key': {'type': 'string'}, 'cluster': {'type': 'string'}, 'gene': {'type': 'string'},
                      'view': {'type': 'string', 'enum': ['global', 'local', 'both']}, 'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 200}}
        elif name.startswith('submit'):
            fields = {'proposal_json': {'type': 'string'}}
        else:
            fields = {'offset': {'type': 'integer', 'minimum': 0}}
        tools.append(dict(name=name, description='d', read_only=False, multimodal=False,
                          parameters={'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
                          args=['-m', program, 'tool', name, '{state}', '{arguments}'],
                          inputs=[{'path': '/x/' + program + '.py', 'sha256': '0' * 64}], outputs=['result.json'], result_file='result.json',
                          cpus=1, memory_mb=64, timeout_seconds=30))
    return dict(session_id='s', prompt='base prompt', tools=tools)


def check_contract(session, program):
    assert 'Required order' in session['prompt'] and session['prompt'].startswith('base prompt')
    for tool in session['tools']:
        assert tool['args'][:2] == ['-m', program]
        assert tool['inputs'][0]['path'].endswith(program.split('.')[-1] + '.py') and len(tool['inputs']) == 2
        Draft202012Validator.check_schema(tool['parameters'])
        assert tool['parameters']['additionalProperties'] is False
    lookup = Draft202012Validator(next(t['parameters'] for t in session['tools'] if t['name'] == 'deg_lookup'))
    assert lookup.is_valid({'cluster': '3'}) and lookup.is_valid({'gene': 'CD3D', 'min_logfc': 1, 'max_padj': None})
    assert not lookup.is_valid({}) and not lookup.is_valid({'top_n': 5}) and not lookup.is_valid({'cluster': '3', 'bogus': 1})


def test_zoomin_v3_session_contract(monkeypatch):
    monkeypatch.setattr(zoomin_v3.v2, 'agent_spec', lambda *a: fake_session('ecarsi.zoomin_v2'))
    for kind in ('plan', 'lineage'):
        session = zoomin_v3.agent_spec({}, {}, kind, 'parent')
        check_contract(session, 'ecarsi.zoomin_v3')
        for tool in session['tools']:
            assert (zoomin_v3.OBJECT_NOTE in tool['description']) == (tool['name'] in zoomin_v3.SUBMISSIONS)


def test_crosssample_v3_session_contract(monkeypatch):
    monkeypatch.setattr(crosssample_v3.v2, 'agent_spec', lambda *a: fake_session('ecarsi.crosssample_v2'))
    for phase in ('inclusion', 'type', 'quality'):
        session = crosssample_v3.agent_spec({}, {}, phase, 'parent', None)
        check_contract(session, 'ecarsi.crosssample_v3')
        assert crosssample_v3.OBJECT_NOTE in next(t['description'] for t in session['tools'] if t['name'] == 'submit_decision')


def test_canonical_arguments_give_v2_what_it_expects():
    proposal = {'clusters': [{'cluster_id': '1'}]}
    assert zoomin_v3.canonical_arguments('submit_quality', {'proposal_json': proposal}) == {'proposal_json': json.dumps(proposal)}
    assert crosssample_v3.canonical_arguments('submit_decision', {'proposal_json': proposal}) == {'proposal_json': json.dumps(proposal)}
    assert zoomin_v3.canonical_arguments('submit_quality', {'proposal_json': '{}'}) == {'proposal_json': '{}'}
    assert crosssample_v3.canonical_arguments('deg_lookup', {'gene': 'CD3D', 'min_logfc': None, 'max_padj': 1e-3}) == {'gene': 'CD3D', 'cluster': '', 'max_padj': 1e-3}
    scheduled = pytest.importorskip('zmip.scheduled')
    filled = zoomin_v3.canonical_arguments('deg_lookup', {'cluster': '2', 'view': 'local'})
    assert filled == {'cluster': '2', 'view': 'local', 'gene': '', 'key': scheduled.TYPE_KEY}


def test_coverage_hint_lists_scope_intersections_and_labels():
    scheduled = pytest.importorskip('zmip.scheduled')
    obs = pd.DataFrame({scheduled.TYPE_KEY: pd.Categorical(['0', '0', '1', '1', '1']),
                        scheduled.QUALITY_KEY: pd.Categorical(['0', '1', '1', '1', '2'])}, index=[f'c{i}' for i in range(5)])
    hint = zoomin_v3.coverage_hint(obs, {'type_scope': ['1']}, ['Fibroblast'], ['Endothelial'])
    assert '["1"]' in hint and '"Fibroblast"' in hint and '"Endothelial"' in hint
    assert json.dumps({'0': {'0': 1}, '1': {'0': 1, '1': 2}, '2': {'1': 1}}) in hint


def _wrapped_tool(module, tmp_path, monkeypatch, content):
    seen = {}

    def v2_tool(name, state_path, args_path, destination):
        seen['args'] = read(args_path)
        save(Path(destination) / 'state.json', {})
        save(Path(destination) / 'result.json', {'is_error': True, 'content': content, 'state': {'path': str(Path(destination) / 'state.json'), 'sha256': '0' * 64}})
    monkeypatch.setattr(module.v2, 'tool', v2_tool)
    state = tmp_path / 'state.json'
    save(state, {'evidence': {'path': str(tmp_path / 'none.json'), 'sha256': '0' * 64}, 'phase': 'type'})
    args = tmp_path / 'arguments.json'
    save(args, {'proposal_json': {'clusters': []}})
    out = tmp_path / 'out'
    out.mkdir()
    module.tool('submit_quality' if module is zoomin_v3 else 'submit_decision', str(state), str(args), out)
    return seen['args'], read(out / 'result.json')


@pytest.mark.parametrize('module', [zoomin_v3, crosssample_v3])
def test_tool_wrapper_stringifies_object_proposals_and_explains_bad_json(tmp_path, monkeypatch, module):
    args, result = _wrapped_tool(module, tmp_path, monkeypatch, 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)')
    assert args == {'proposal_json': json.dumps({'clusters': []})}  # v2 saw a string
    assert result['is_error'] and 'pass the proposal as a JSON object' in result['content']


def test_tool_wrapper_never_turns_a_hint_failure_into_a_crash(tmp_path, monkeypatch):
    # The evidence reference does not exist, so the coverage hint cannot be built; the v2 error still comes back.
    _, result = _wrapped_tool(zoomin_v3, tmp_path, monkeypatch, 'Quality decisions must cover every 2.0 cluster')
    assert result['is_error'] and result['content'] == 'Quality decisions must cover every 2.0 cluster'


def test_boundary_hint_derives_the_adjacent_kept_pairs(monkeypatch):
    evidence = pytest.importorskip('msp.evidence')
    monkeypatch.setattr(evidence, 'load_paga_neighbors', lambda folder, key: {'0': ['1', '2'], '1': ['0'], '2': ['0']})
    monkeypatch.setattr(crosssample_v3.v2, 'artifact', lambda bundle, name: Path('/nowhere/deg.sqlite'))
    proposal = {'clusters': [{'cluster_id': '0', 'action': 'keep', 'coarse_label': 'T cell'},
                             {'cluster_id': '1', 'action': 'keep', 'coarse_label': 'B cell'},
                             {'cluster_id': '2', 'action': 'remove', 'coarse_label': 'doublet'}]}
    hint = crosssample_v3.boundary_hint({}, proposal)
    assert json.dumps([['B cell', 'T cell']]) in hint
