"""Protocol v3/v4: the same numerics as v2 behind a contract the model can satisfy first time, in few turns.

Measured 2026-09-16: 13.9 % of all model turns were decision submissions the host rejected.
Measured 2026-09-17: finalize_annotation never changed a decision in 247 sessions; paged reads
cost a turn per page and skipped the 61st path in 62 sessions.
"""
import json
from pathlib import Path

import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from ecarsi import crosssample_v3, zoomin_v3
from ecarsi.warm_pool.state import read, save

FAKE_BUNDLE = {'files': ['figures/umap_msp_leiden_r1.0.png', 'figures/qc_violin.png', 'per_sample_qc.csv',
                         'deg_input/x.csv', 'integrated.h5ad', 'type_quality_intersections.csv']}


def fake_session(program):
    tools = []
    for name in ('list_evidence', 'read_evidence', 'annotation_status', 'type_context', 'deg_lookup',
                 'submit_quality', 'submit_types', 'submit_plan', 'submit_decision', 'finalize_annotation'):
        if name == 'deg_lookup':
            fields = {'key': {'type': 'string'}, 'cluster': {'type': 'string'}, 'gene': {'type': 'string'},
                      'view': {'type': 'string', 'enum': ['global', 'local', 'both']}, 'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 200}}
        elif name.startswith('submit'):
            fields = {'proposal_json': {'type': 'string'}}
        elif name == 'finalize_annotation':
            fields = {}
        else:
            fields = {'offset': {'type': 'integer', 'minimum': 0}}
        tools.append(dict(name=name, description='d', read_only=False, multimodal=False,
                          parameters={'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
                          args=['-m', program, 'tool', name, '{state}', '{arguments}'],
                          inputs=[{'path': '/x/' + program + '.py', 'sha256': '0' * 64}], outputs=['result.json'], result_file='result.json',
                          cpus=1, memory_mb=64, timeout_seconds=30))
    return dict(session_id='s', prompt='base prompt', tools=tools, completion_tool='finalize_annotation')


def check_contract(session, program, paged):
    assert 'Required order' in session['prompt'] and session['prompt'].startswith('base prompt')
    for tool in session['tools']:
        assert tool['args'][:2] == ['-m', program]
        assert tool['inputs'][0]['path'].endswith(program.split('.')[-1] + '.py') and len(tool['inputs']) == 2
        Draft202012Validator.check_schema(tool['parameters'])
        assert tool['parameters']['additionalProperties'] is False
        if tool['name'] != 'finalize_annotation':
            assert (tool['parameters']['properties'] == {}) == (tool['name'] in paged)
    lookup = Draft202012Validator(next(t['parameters'] for t in session['tools'] if t['name'] == 'deg_lookup'))
    assert lookup.is_valid({'cluster': '3'}) and lookup.is_valid({'gene': 'CD3D', 'min_logfc': 1, 'max_padj': None})
    assert not lookup.is_valid({}) and not lookup.is_valid({'top_n': 5}) and not lookup.is_valid({'cluster': '3', 'bogus': 1})


def test_zoomin_session_contract(monkeypatch):
    monkeypatch.setattr(zoomin_v3.v2, 'agent_spec', lambda *a: fake_session('ecarsi.zoomin_v2'))
    monkeypatch.setattr(zoomin_v3, 'verified', lambda ref: FAKE_BUNDLE)
    monkeypatch.setattr(zoomin_v3, 'intersections', lambda bundle: {'0': {'1': 120, '10': 4}, '5': {'2': 80}})
    for kind in ('plan', 'lineage'):
        session = zoomin_v3.agent_spec({}, {}, kind, 'parent')
        check_contract(session, 'ecarsi.zoomin_v3', zoomin_v3.PAGED)
        names = [t['name'] for t in session['tools']]
        assert 'finalize_annotation' not in names
        assert session['completion_tool'] == ('submit_quality' if kind == 'lineage' else 'finalize_annotation')
        assert '"figures/umap_msp_leiden_r1.0.png"' in session['prompt'] and 'deg_input' not in session['prompt']
        assert ('Pending type clusters' in session['prompt']) == (kind == 'lineage')
        if kind == 'lineage':
            assert '["1", "2", "10"]' in session['prompt']
        for tool in session['tools']:
            assert (zoomin_v3.OBJECT_NOTE in tool['description']) == (tool['name'] in zoomin_v3.SUBMISSIONS)


def test_crosssample_session_contract(monkeypatch):
    monkeypatch.setattr(crosssample_v3.v2, 'agent_spec', lambda *a: fake_session('ecarsi.crosssample_v2'))
    monkeypatch.setattr(crosssample_v3, 'verified', lambda ref: FAKE_BUNDLE)
    for phase in ('inclusion', 'type', 'quality'):
        session = crosssample_v3.agent_spec({}, {}, phase, 'parent', None)
        check_contract(session, 'ecarsi.crosssample_v3', crosssample_v3.PAGED)
        assert crosssample_v3.OBJECT_NOTE in next(t['description'] for t in session['tools'] if t['name'] == 'submit_decision')
        assert ('Evidence files' in session['prompt']) == (phase != 'inclusion')


def paging_v2(pages):
    """A v2 tool that answers one page per call and records nothing in the state."""
    def fake_tool(name, state_path, args_path, destination):
        offset = read(args_path)['offset']
        page = dict(pages[offset], state=save(Path(destination) / 'state.json', {'offset': offset}) or {'path': str(Path(destination) / 'state.json')})
        save(Path(destination) / 'result.json', page)
    return fake_tool


def test_paged_reads_merge_every_page(tmp_path, monkeypatch):
    pages = {0: {'content': ['a', 'b'], 'next_offset': 30}, 30: {'content': ['c'], 'next_offset': 60}, 60: {'content': ['d'], 'next_offset': None}}
    monkeypatch.setattr(zoomin_v3.v2, 'tool', paging_v2(pages))
    zoomin_v3.tool('list_evidence', 'state.json', 'unused.json', tmp_path)
    result = read(tmp_path / 'result.json')
    assert result['content'] == ['a', 'b', 'c', 'd'] and result['next_offset'] is None
    status = {0: {'types': [1], 'quality': [], 'intersections': {'0': {'1': 3}}, 'next_offset': 10},
              10: {'types': [2], 'quality': [9], 'intersections': {'10': {'2': 4}}, 'next_offset': None}}
    monkeypatch.setattr(zoomin_v3.v2, 'tool', paging_v2(status))
    (tmp_path / 'second').mkdir()  # every tool call has its own destination directory
    zoomin_v3.tool('annotation_status', 'state.json', 'unused.json', tmp_path / 'second')
    result = read(tmp_path / 'second' / 'result.json')
    assert result['types'] == [1, 2] and result['quality'] == [9] and result['intersections'] == {'0': {'1': 3}, '10': {'2': 4}}


def test_accepted_quality_completes_the_session(tmp_path, monkeypatch):
    state = save(tmp_path / 'state-after.json', dict(evidence={'path': 'e', 'sha256': '1' * 64}, types={'clusters': []},
                                                     quality={'clusters': []}, removal_fraction=0.02))
    def accept(name, state_path, args_path, destination):
        save(Path(destination) / 'result.json', {'content': 'Quality coverage accepted', 'state': {'path': str(tmp_path / 'state-after.json')}})
    monkeypatch.setattr(zoomin_v3.v2, 'tool', accept)
    save(tmp_path / 'args.json', {'proposal_json': {'clusters': []}})
    zoomin_v3.tool('submit_quality', 'state.json', str(tmp_path / 'args.json'), tmp_path)
    result = read(tmp_path / 'result.json')
    assert result['accepted'] is True and result['removal_fraction'] == 0.02 and result['quality'] == {'clusters': []}


def test_a_proposal_with_a_trailing_quote_is_accepted():
    proposal = '{"clusters": [{"cluster_id": "0"}]}'
    for module, name in ((zoomin_v3, 'submit_quality'), (crosssample_v3, 'submit_decision')):
        fixed = module.canonical_arguments(name, {'proposal_json': proposal + '"'})
        assert json.loads(fixed['proposal_json']) == {'clusters': [{'cluster_id': '0'}]}
        broken = module.canonical_arguments(name, {'proposal_json': proposal + ', "more": 1}'})
        assert broken['proposal_json'] == proposal + ', "more": 1}'  # real trailing data still reaches v2's error path
