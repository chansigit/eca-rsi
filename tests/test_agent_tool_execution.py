import pytest

from ecarsi.agent_session import immutable, reference, verified
from ecarsi.agent_tool_execution import execute, plan
from ecarsi.persample_v2 import sealed, evidence_files
from ecarsi.warm_pool.state import read, save


def test_pagination_preserves_all_text_and_seen_state_with_bounded_budget(tmp_path, monkeypatch):
    folder = tmp_path / 'computed'
    folder.mkdir()
    (folder / 'clustered.h5ad').write_bytes(b'not loaded for evidence reading')
    (folder / 'de_top_genes_r1.csv').write_text('gene,score\n' + 'CD3D,1\n' * 20000)
    bundle = sealed(folder, tmp_path / 'bundle.json')
    state = immutable(tmp_path / 'state.json', dict(bundle=bundle,
        data=reference(folder / 'clustered.h5ad'), key='r1', version=0,
        seen=dict(figures=[], tables=[], genes=False, qc=False)))
    arguments = immutable(tmp_path / 'arguments.json', dict(kind='tables', offset=0))
    request = dict(request_id='test', args=['-m', 'ecarsi.persample_v2', 'tool', 'read_evidence',
        state['path'], arguments['path']], memory_mb=12288, inputs=[state, arguments])
    output = tmp_path / 'output'
    output.mkdir()
    planned = plan(request, tmp_path, tmp_path / 'pool')
    assert planned['memory_mb'] == 256
    monkeypatch.chdir(output)
    execute(planned['args'][2])
    result = read(output / 'result.json')
    assert result['pages_returned'] == 3 and result['next_offset'] is None
    assert result['text'] == evidence_files(verified(bundle))[1]
    assert verified(result['state'])['seen']['tables'] == [0, 60000, 120000]
    assert verified(state)['seen']['tables'] == []  # Original version remains immutable.
    assert plan(request, tmp_path, tmp_path / 'pool') == planned
    with pytest.raises(ValueError, match='execution changed'):
        plan(dict(request, memory_mb=8192), tmp_path, tmp_path / 'pool')


def test_prior_request_keeps_original_budget_and_command(tmp_path):
    directory = tmp_path / 'tool'
    directory.mkdir()
    root = tmp_path / 'pool'
    previous = root / 'requests/test'
    previous.mkdir(parents=True)
    request = dict(request_id='test', args=['-m', 'ecarsi.persample_v2', 'tool',
        'read_evidence', 'state.json', 'arguments.json'], memory_mb=12288, inputs=[])
    save(previous / 'request.json', dict(spec=request))
    assert plan(request, directory, root) == request
    assert not (directory / 'execution.json').exists()
