import base64
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import agent_evidence as batch
from ecarsi.agent_session import immutable, reference, verified
from ecarsi.persample_v2 import sealed
from ecarsi.warm_pool.state import read, save

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMZkAAAAASUVORK5CYII=')


def run_batch(directory, monkeypatch, module, state, name, arguments, allowed, multimodal=True):
    directory.mkdir()
    args = immutable(directory / 'arguments.json', arguments)
    packet = immutable(directory / 'packet.json', dict(module=module, name=name,
        state=state['path'], arguments=args['path'], allowed=allowed, multimodal=multimodal))
    monkeypatch.chdir(directory)
    batch.execute(packet['path'])
    return read(directory / 'result.json')


def test_osp_returns_required_evidence_without_skipping_marker_judgment(tmp_path, monkeypatch):
    source = tmp_path / 'computed'; source.mkdir()
    data = ad.AnnData(np.ones((6, 3)), obs=pd.DataFrame({
        'cluster': pd.Categorical(['0'] * 6), 'total_counts': [3] * 6},
        index=[f'c{i}' for i in range(6)]), var=pd.DataFrame(index=['CD3D', 'LYZ', 'MS4A1']))
    data.write_h5ad(source / 'clustered.h5ad')
    (source / 'umap_clusters.png').write_bytes(PNG)
    (source / 'de_top_genes_r1.csv').write_text('cluster,gene\n0,CD3D\n' * 4000)
    bundle = sealed(source, tmp_path / 'computed.json')
    state = immutable(tmp_path / 'before.json', dict(bundle=bundle, data=reference(source / 'clustered.h5ad'),
        key='cluster', version=0, seen=dict(figures=[], tables=[], genes=False, qc=False)))
    result = run_batch(tmp_path / 'batch', monkeypatch, 'ecarsi.persample_v2', state,
        'read_evidence', dict(kind='figures', offset=0), ['read_evidence', 'check_qc_scores'])
    after = verified(result['state'])
    assert after['seen'] == dict(figures=['umap_clusters.png'], tables=[0, 60000], genes=False, qc=True)
    assert len(result['images']) == 1 and result['evidence_batch']['pending'] is None
    assert len(result['evidence_batch']['calls']) == 4
    assert verified(state)['seen']['qc'] is False
    from ecarsi.persample_v2 import tool
    reject = tmp_path / 'reject'; reject.mkdir()
    args = immutable(reject / 'args.json', dict(proposal_json='{}', version=0))
    tool('submit_annotation', result['state']['path'], args['path'], reject)
    assert 'verify current markers' in read(reject / 'result.json')['error']


def inclusion_state(tmp_path, count=3):
    source = tmp_path / 'source'; source.mkdir()
    samples = [dict(sample=f's{i}', n_cells=20, annotation={}) for i in range(count)]
    files = {}
    for sample in samples:
        p = source / (sample['sample'] + '.png'); p.write_bytes(PNG)
        files[sample['sample'] + '/figures/umap_clusters.png'] = reference(p)
    evidence = immutable(tmp_path / 'bundle.json', dict(samples=samples, files=files))
    return immutable(tmp_path / 'state.json', dict(evidence=evidence, phase='inclusion',
        types=None, read=[], lookups=[], qc=False))


def test_inclusion_batches_inventories_and_figures_but_requires_every_sample(tmp_path, monkeypatch):
    state = inclusion_state(tmp_path)
    allowed = ['sample_inventory', 'read_evidence']
    result = run_batch(tmp_path / 'inventory', monkeypatch, 'ecarsi.crosssample_v2', state,
        'sample_inventory', dict(offset=0), allowed, multimodal=False)
    assert verified(result['state'])['inventories'] == ['s0', 's1', 's2']
    assert result['next_offset'] is None and len(result['additional_evidence']) == 2
    assert not verified(result['state'])['read']
    # Limit the batch to one image. The computed-but-unreturned second image
    # must not appear in the state used by submission validation.
    monkeypatch.setattr(batch, 'IMAGE_BYTES', 150)
    figure = run_batch(tmp_path / 'figure', monkeypatch, 'ecarsi.crosssample_v2', result['state'],
        'read_evidence', dict(path='s0/figures/umap_clusters.png', offset=0), allowed)
    assert verified(figure['state'])['read'] == ['s0/figures/umap_clusters.png']
    pending = figure['evidence_batch']['pending']
    assert pending == dict(tool='read_evidence', arguments=dict(path='s1/figures/umap_clusters.png', offset=0))
    from ecarsi.crosssample_v2 import tool
    args = immutable(tmp_path / 'decision.json', dict(proposal_json=json.dumps({'notes': 'Reviewed all samples', 'samples': [
        dict(sample=f's{i}', include=True, reason='Reviewed evidence') for i in range(3)]})))
    reject = tmp_path / 'reject'; reject.mkdir()
    tool('submit_decision', figure['state']['path'], args['path'], reject)
    assert 'Read each sample cluster UMAP' in read(reject / 'result.json')['content']
    monkeypatch.setattr(batch, 'IMAGE_BYTES', 12 * 2**20)
    rest = run_batch(tmp_path / 'rest', monkeypatch, 'ecarsi.crosssample_v2', figure['state'],
        pending['tool'], pending['arguments'], allowed)
    assert len(rest['images']) == 2 and len(verified(rest['state'])['read']) == 3
    accept = tmp_path / 'accept'; accept.mkdir()
    tool('submit_decision', rest['state']['path'], args['path'], accept)
    assert read(accept / 'result.json')['accepted'] is True


def test_pagination_bound_and_frozen_execution_plan(tmp_path, monkeypatch):
    state = inclusion_state(tmp_path, count=12)
    result = run_batch(tmp_path / 'inventory', monkeypatch, 'ecarsi.crosssample_v2', state,
        'sample_inventory', dict(offset=0), ['sample_inventory'], multimodal=False)
    assert result['next_offset'] == 8 and len(result['evidence_batch']['calls']) == 8
    assert len(verified(result['state'])['inventories']) == 8
    packet = read(tmp_path / 'inventory/packet.json')
    request = dict(request_id='bounded', operation_id='sample_inventory',
        args=['-m', packet['module'], 'tool', 'sample_inventory', packet['state'], packet['arguments']],
        cpus=1, memory_mb=1024, timeout_seconds=60, inputs=[state], outputs=['result.json'])
    session = dict(pool_root=str(tmp_path / 'pool'), tools=[dict(name='sample_inventory',
        args=['-m', packet['module'], 'tool', 'sample_inventory', '{state}', '{arguments}'],
        cpus=1, memory_mb=1024, timeout_seconds=60)])
    directory = tmp_path / 'plan'; directory.mkdir()
    planned = batch.plan(request, directory, session)
    assert planned['args'][:2] == ['-m', 'ecarsi.agent_evidence']
    assert batch.plan(request, directory, session) == planned
    with pytest.raises(ValueError, match='execution changed'):
        batch.plan(dict(request, memory_mb=2048), directory, session)
    # A previously registered original plan must not be upgraded in place.
    directory = tmp_path / 'old'; directory.mkdir()
    from ecarsi.agent_tool_execution import plan
    original = plan(request, directory, session['pool_root'])
    assert batch.plan(request, directory, session) == original == request


@pytest.mark.parametrize('module', ['ecarsi.crosssample_v2', 'ecarsi.zoomin_v2'])
def test_text_pages_and_errors_keep_original_checks(tmp_path, monkeypatch, module):
    text = 'gene,score\nCD3D,1\n\u03b22M,2\n' * 4000
    path = tmp_path / 'table.csv'; path.write_text(text)
    evidence = immutable(tmp_path / 'bundle.json', dict(files={'table.csv': reference(path)}))
    state = immutable(tmp_path / 'state.json', dict(evidence=evidence, phase='type', kind='plan', read=[]))
    result = run_batch(tmp_path / 'pages', monkeypatch, module, state, 'read_evidence',
                       dict(path='table.csv', offset=0), ['read_evidence'])
    assert result['next_offset'] is None
    returned = result['content'] + ''.join(r['result']['content'] for r in result['additional_evidence'])
    assert len(returned) == len(text)
    assert batch.digest(returned) == batch.digest(text) and verified(result['state'])['read'] == ['table.csv']
    # Corrupted evidence still fails; batching never substitutes cached contents.
    path.write_text('corrupt')
    bad = run_batch(tmp_path / 'bad', monkeypatch, module, state, 'read_evidence',
                    dict(path='table.csv', offset=0), ['read_evidence'])
    assert bad['is_error'] and verified(bad['state'])['read'] == []
    assert len(bad['evidence_batch']['calls']) == 1
