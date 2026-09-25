import base64
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

import ecarsi.stages.evidence as batch
from ecarsi.agent.session import immutable, reference, verified
from ecarsi.stages.persample import sealed
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
    result = run_batch(tmp_path / 'batch', monkeypatch, 'ecarsi.stages.persample', state,
        'read_evidence', dict(kind='figures', offset=0), ['read_evidence', 'check_qc_scores'])
    after = verified(result['state'])
    assert after['seen'] == dict(figures=['umap_clusters.png'], tables=[0, 60000], genes=False, qc=True)
    assert len(result['images']) == 1 and result['evidence_batch']['pending'] is None
    assert len(result['evidence_batch']['calls']) == 4
    assert verified(state)['seen']['qc'] is False
    from ecarsi.stages.persample import tool
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
    import ecarsi.stages.crosssample as cross
    monkeypatch.setattr(cross, 'INVENTORY_PAGE_BYTES', 1)   # one sample per page: the batch mechanics under test
    state = inclusion_state(tmp_path)
    allowed = ['sample_inventory', 'read_evidence']
    result = run_batch(tmp_path / 'inventory', monkeypatch, 'ecarsi.stages.crosssample', state,
        'sample_inventory', dict(offset=0), allowed, multimodal=False)
    assert verified(result['state'])['inventories'] == ['s0', 's1', 's2']
    assert result['next_offset'] is None and len(result['additional_evidence']) == 2
    assert not verified(result['state'])['read']
    # Limit the batch to one image. The computed-but-unreturned second image
    # must not appear in the state used by submission validation.
    monkeypatch.setattr(batch, 'IMAGE_BYTES', 150)
    figure = run_batch(tmp_path / 'figure', monkeypatch, 'ecarsi.stages.crosssample', result['state'],
        'read_evidence', dict(path='s0/figures/umap_clusters.png', offset=0), allowed)
    assert verified(figure['state'])['read'] == ['s0/figures/umap_clusters.png']
    pending = figure['evidence_batch']['pending']
    assert pending == dict(tool='read_evidence', arguments=dict(path='s1/figures/umap_clusters.png', offset=0))
    from ecarsi.stages.crosssample import tool
    args = immutable(tmp_path / 'decision.json', dict(proposal_json=json.dumps({'notes': 'Reviewed all samples', 'samples': [
        dict(sample=f's{i}', include=True, reason='Reviewed evidence') for i in range(3)]})))
    reject = tmp_path / 'reject'; reject.mkdir()
    tool('submit_decision', figure['state']['path'], args['path'], reject)
    assert 'Read each sample cluster UMAP' in read(reject / 'result.json')['content']
    monkeypatch.setattr(batch, 'IMAGE_BYTES', 12 * 2**20)
    rest = run_batch(tmp_path / 'rest', monkeypatch, 'ecarsi.stages.crosssample', figure['state'],
        pending['tool'], pending['arguments'], allowed)
    assert len(rest['images']) == 2 and len(verified(rest['state'])['read']) == 3
    accept = tmp_path / 'accept'; accept.mkdir()
    tool('submit_decision', rest['state']['path'], args['path'], accept)
    assert read(accept / 'result.json')['accepted'] is True


def test_pagination_bound_and_frozen_execution_plan(tmp_path, monkeypatch):
    import ecarsi.stages.crosssample as cross
    monkeypatch.setattr(cross, 'INVENTORY_PAGE_BYTES', 1)
    state = inclusion_state(tmp_path, count=12)
    result = run_batch(tmp_path / 'inventory', monkeypatch, 'ecarsi.stages.crosssample', state,
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
    assert planned['args'][:2] == ['-m', 'ecarsi.stages.evidence']
    assert batch.plan(request, directory, session) == planned
    with pytest.raises(ValueError, match='execution changed'):
        batch.plan(dict(request, memory_mb=2048), directory, session)
    # A previously registered original plan must not be upgraded in place.
    directory = tmp_path / 'old'; directory.mkdir()
    from ecarsi.stages.execution import plan
    original = plan(request, directory, session['pool_root'])
    assert batch.plan(request, directory, session) == original == request


@pytest.mark.parametrize('module', ['ecarsi.stages.crosssample', 'ecarsi.stages.zoomin'])
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


def test_matrix_budget_uses_compute_receipt_and_keeps_prior_request(tmp_path):
    pool = tmp_path/'pool'
    output = pool/'requests/compute/attempt/outputs/prepared.json'
    output.parent.mkdir(parents=True);save(output, {})
    computed = reference(output)
    save(output.parent.parent/'receipt.json', dict(state='succeeded', peak_rss_bytes=2**30, outputs=[computed]))
    evidence = immutable(tmp_path/'evidence.json', dict(prepared=computed))
    state = immutable(tmp_path/'state.json', dict(evidence=evidence, phase='type'))
    tool = dict(name='check_genes', read_only=True, args=['-m','ecarsi.stages.crosssample','tool','check_genes','{state}','{arguments}'])
    req = dict(request_id='t', operation_id='check_genes', args=tool['args'], memory_mb=49152, inputs=[])
    (tmp_path/'new').mkdir()
    result = batch.budget(req, tmp_path/'new', {'pool_root':str(pool)}, tool, state)
    assert result['memory_mb'] == 3072
    assert batch.budget(req, tmp_path/'new', {'pool_root':str(pool)}, tool, state) == result
    path = pool/'requests/old/request.json';path.parent.mkdir();save(path, {})
    old = dict(req, request_id='old')
    assert batch.budget(old, tmp_path/'old', {'pool_root':str(pool)}, tool, state) == old


def test_stages_declare_their_read_only_tools(tmp_path):
    """The model-turn service batches what the stage declared; no host-side list of tool names."""
    from ecarsi.stages.persample import annotation_spec
    computed = tmp_path/'computed.json'
    save(computed, dict(prompt='p', proposal_schema='{}', sample='s1'))
    save(tmp_path/'annotation-state.json', dict(version=0))
    spec = dict(run_id='r', dataset_id='d', pool_root=str(tmp_path/'pool'), bridge_root=str(tmp_path/'bridge'),
                output_root=str(tmp_path/'out'), tool_budget=dict(cpus=1, memory_mb=64, timeout_seconds=30))
    session = annotation_spec(spec, reference(computed), 'compute')
    assert {t['name'] for t in session['tools'] if t['read_only']} == {'read_evidence', 'check_genes', 'check_qc_scores'}
    assert session['planner'] == 'ecarsi.stages.evidence'


def test_osp_table_pages_reach_the_model_compacted(tmp_path, monkeypatch):
    """Fat TSP25: 185k characters of raw DE CSV over four pages overran the provider context (2026-09-18)."""
    source = tmp_path / 'computed'; source.mkdir()
    data = ad.AnnData(np.ones((6, 3)), obs=pd.DataFrame({'cluster': pd.Categorical(['0'] * 6), 'total_counts': [3] * 6},
        index=[f'c{i}' for i in range(6)]), var=pd.DataFrame(index=['CD3D', 'LYZ', 'MS4A1']))
    data.write_h5ad(source / 'clustered.h5ad')
    (source / 'umap_clusters.png').write_bytes(PNG)
    rows = ['group,names,scores,logfoldchanges,pvals,pvals_adj,pct1,pct2'] + [
        f'{g},G{g}_{i},{30 - i},{2.5 + i / 100:.7f},1e-9,1e-8,0.9123456789,0.1234567890' for g in range(80) for i in range(25)]
    (source / 'de_top_genes_r1.csv').write_text('\n'.join(rows) + '\n')  # > 60k characters: two pages
    bundle = sealed(source, tmp_path / 'computed.json')
    state = immutable(tmp_path / 'before.json', dict(bundle=bundle, data=reference(source / 'clustered.h5ad'),
        key='cluster', version=0, seen=dict(figures=[], tables=[], genes=False, qc=False)))
    result = run_batch(tmp_path / 'batch', monkeypatch, 'ecarsi.stages.persample', state,
        'read_evidence', dict(kind='tables', offset=0), ['read_evidence', 'check_qc_scores'])
    assert verified(result['state'])['seen']['tables'] == [0, 60000]
    assert result['text'].startswith('de_top_genes_r1.csv  (top 15 markers per cluster')
    assert result['text'].count('G79_') == 15 and ' G0_15 ' not in result['text'] and len(result['text']) < 40000  # raw text is 124k
    assert all(e['result'].get('text') == '' for e in result['additional_evidence'] if e['tool'] == 'read_evidence' and e['arguments'].get('kind') == 'tables')


def test_inventory_pages_summaries_by_bytes_and_an_exclusion_needs_the_full_proposal(tmp_path, monkeypatch):
    from ecarsi.stages.crosssample import inventory_page, tool
    source = tmp_path / 'source'; source.mkdir()
    samples, files = [], {}
    for i in range(3):
        name = f's{i}'
        clusters = [dict(cluster=str(c), confidence='high' if c else 'low', label_coarse='T cell' if c else 'Doublet',
                         label_fine='x', doubts='ambient ' * 50, evidence_genes=['CD3D']) for c in range(4)]
        proposal = dict(cluster_key='leiden', clusters=clusters, overall='verdict ' * 80)
        p = source / f'{name}.json'; p.write_text(json.dumps(proposal))
        png = source / f'{name}.png'; png.write_bytes(PNG)
        files[f'{name}/annotation_proposal.json'] = reference(p)
        files[f'{name}/figures/umap_clusters.png'] = reference(png)
        samples.append(dict(sample=name, n_cells=20, qc=dict(median_genes='700.0'), annotation=proposal))
    bundle = dict(samples=samples, files=files)
    page, more = inventory_page(bundle, 0)
    assert more is None and [e['sample'] for e in page] == ['s0', 's1', 's2']
    entry = page[0]
    assert entry['n_clusters'] == 4 and entry['confidence'] == {'low': 1, 'high': 3} and entry['uncertain_fraction'] == 0.25
    assert entry['top_coarse'] == ['T cell x3', 'Doublet x1'] and len(entry['verdict']) == 240
    assert entry['proposal'] == 's0/annotation_proposal.json' and entry['umap'] == 's0/figures/umap_clusters.png'
    assert entry['qc'] == {'median_genes': 700}
    assert 'doubts' not in json.dumps(page) and len(json.dumps(page)) < 2400   # the summary, not the prose
    from ecarsi.stages.crosssample import compact_qc
    assert compact_qc({'median_pct_mt': '2.452605724334717', 'median_counts': '5996.5', 'n_cells': '2110', 'decontx_degenerate': 'False'}) == {'median_pct_mt': 2.45, 'median_counts': 6000, 'n_cells': 2110, 'decontx_degenerate': 'False'}
    monkeypatch.setattr('ecarsi.stages.crosssample.INVENTORY_PAGE_BYTES', len(json.dumps(page[0])) + 10)
    assert [e['sample'] for e in inventory_page(bundle, 0)[0]] == ['s0'] and inventory_page(bundle, 0)[1] == 1
    assert inventory_page(bundle, 2)[1] is None
    with pytest.raises(ValueError, match='past the last sample'):
        inventory_page(bundle, 3)
    # the host rule: every inventory and UMAP read, and the full proposal of a sample being excluded
    evidence = immutable(tmp_path / 'bundle.json', bundle)
    state = immutable(tmp_path / 'state.json', dict(evidence=evidence, phase='inclusion', types=None,
        inventories=['s0', 's1', 's2'], read=[f's{i}/figures/umap_clusters.png' for i in range(3)], lookups=[], qc=False))
    decision = immutable(tmp_path / 'decision.json', dict(proposal_json=json.dumps({'notes': 'n', 'samples': [
        dict(sample='s0', include=True, reason='fine'), dict(sample='s1', include=False, reason='doublets'),
        dict(sample='s2', include=True, reason='fine')]})))
    reject = tmp_path / 'reject'; reject.mkdir()
    tool('submit_decision', state['path'], decision['path'], reject)
    assert "Read the full annotation proposal" in read(reject / 'result.json')['content'] and "['s1']" in read(reject / 'result.json')['content']
    args = immutable(tmp_path / 'read.json', dict(path='s1/annotation_proposal.json', offset=0))
    seen = tmp_path / 'seen'; seen.mkdir()
    tool('read_evidence', state['path'], args['path'], seen)
    result = read(seen / 'result.json')
    assert 'ambient' in result['content'] and result['next_offset'] is None
    accept = tmp_path / 'accept'; accept.mkdir()
    tool('submit_decision', result['state']['path'], decision['path'], accept)
    assert read(accept / 'result.json')['accepted'] is True
