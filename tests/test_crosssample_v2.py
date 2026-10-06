"""Worker handoffs and per-cell exclusion accounting, with real AnnData I/O."""
import json

import anndata as an
import numpy as np
import pandas as pd
import pytest

from ecarsi.agent.session import immutable,reference,validate_spec,verified
from ecarsi.stages.crosssample import BASE,agent_spec,finalize
from ecarsi.stages.contract import NO_ARGUMENTS
from ecarsi.stages.common import sealed
from ecarsi.files import save, read


def test_later_round_keeps_source_ids_and_archives_labels(tmp_path, monkeypatch):
    import ecarsi.stages.crosssample as module
    data = an.AnnData(np.ones((3, 4)), obs=pd.DataFrame({
        'source_unit': ['input'] * 3, 'eca_source_cell_id': ['01', '02', '03'],
        'sample_id': ['A'] * 3, 'msp_ann_coarse': ['old'] * 3,
        'zmip_ann_fine': ['previous fine'] * 3}, index=['a', 'b', 'c']))
    data.layers['counts'] = data.X.copy()
    data.write_h5ad(tmp_path/'annotated_zmip.h5ad')
    source = sealed(tmp_path, tmp_path/'survivors.json', state='complete', n_survived=3)
    spec = dict(input=source, previous_round=1, config={'batch_col': 'sample_id'})
    prepared = tmp_path/'prepared'; prepared.mkdir()
    module.inspect_input(spec, prepared)
    integrated = []
    monkeypatch.setattr(module, 'integrate', lambda data, *args: integrated.append(data))
    output = tmp_path/'output'; output.mkdir()
    module.compute_round(reference(prepared/'inspected.json'), output)
    assert list(integrated[0].obs_names) == ['a', 'b', 'c']
    assert 'msp_ann_coarse' not in integrated[0].obs and 'r01_msp_ann_coarse' in integrated[0].obs
    assert list(integrated[0].obs['r01_zmip_ann_fine']) == ['previous fine'] * 3
    ledger = pd.read_csv(output/'input_cells.csv.gz', dtype=str)
    assert list(ledger.source_cell_id) == ['01', '02', '03']
    assert pd.read_csv(output/'sample_exclusions.csv.gz').empty


def test_agent_tools_have_valid_worker_contracts(tmp_path):
    for name in ('pool','bridge'):
        root=tmp_path/name;root.mkdir(mode=0o700);save(root/'config.json',{})
    spec=dict(run_id='r',dataset_id='D',output_root=str(tmp_path/'run'),pool_root=str(tmp_path/'pool'),
              bridge_root=str(tmp_path/'bridge'),config={},tool_budget=dict(cpus=1,memory_mb=1024,timeout_seconds=30))
    (tmp_path/'run').mkdir(mode=0o700)
    bundle=immutable(tmp_path/'evidence.json',dict(samples=[{'sample':'a'}],files={},type_scope=['0'],type_entries={}))
    types=immutable(tmp_path/'types.json',dict(proposal={'clusters':[]}))
    for phase in ('inclusion','type','quality'):
        session=agent_spec(spec,bundle,phase,'parent',types if phase=='quality' else None)
        validate_spec(session)
        assert all(t['args'][1]=='ecarsi.stages.crosssample' for t in session['tools'])
        assert ('deg_sql' in [t['name'] for t in session['tools']])==(phase!='inclusion')
        assert 'Required order' in session['prompt'] and ('Evidence files' in session['prompt'])==(phase!='inclusion')
        assert all(t['parameters']==NO_ARGUMENTS for t in session['tools'] if t['name']=='type_context')
        assert next(t['parameters'] for t in session['tools'] if t['name']=='list_evidence')=={'type':'object','properties':{'offset':{'type':'integer','minimum':0}},'required':['offset'],'additionalProperties':False}
        reads={t['name'] for t in session['tools'] if t.get('read_only')}
        assert {'read_evidence','list_evidence'}<=reads and not any(n.startswith('submit') for n in reads)


def test_list_evidence_pages_a_large_bundle_without_dropping_or_oversizing_a_page(tmp_path):
    """2026-09-21: a unit that had run many rounds accumulated 5,278 evidence paths; list_evidence
    returned them all in one call, a 534 KB result that exceeded the agent session's 256 KB tool-result
    cap and killed the dataset. Every page must stay well under that cap and no path may be skipped or
    repeated (the earlier, reverted pagination attempt used a fixed page count and dropped the tail)."""
    from ecarsi.stages.crosssample import tool
    from ecarsi.stages.contract import evidence_paths
    files={f'a/figures/f{i}.png':{} for i in range(3000)}
    evidence=immutable(tmp_path/'evidence.json',dict(samples=[{'sample':'a'}],files=files,type_scope=['0'],type_entries={}))
    state=immutable(tmp_path/'state.json',dict(evidence=evidence,phase='inclusion',read=[],lookups=[],qc=False))
    seen=[];offset=0
    for i in range(20):
        args=tmp_path/f'args{i}.json';save(args,{'offset':offset})
        destination=tmp_path/f'call{i}';destination.mkdir()
        tool('list_evidence',state['path'],args,destination)
        response=read(destination/'result.json')
        assert not response.get('is_error')
        assert len(json.dumps(response).encode())<262144
        seen.extend(response['content'])
        if response['next_offset'] is None:break
        offset=response['next_offset']
    else:
        pytest.fail('list_evidence did not terminate within 20 pages')
    assert seen==evidence_paths(verified(evidence))


def test_overlapping_removals_count_once_and_mismatched_decisions_fail(tmp_path,monkeypatch):
    import msp.annotate,msp.report
    # Plot/report presentation is tested by MSP; this check exercises filtering and persisted identities.
    monkeypatch.setattr(msp.annotate,'_plot',lambda *a:None)
    monkeypatch.setattr(msp.report,'generate_report',lambda *a:None)
    source=tmp_path/'source';source.mkdir()
    data=an.AnnData(np.ones((4,2)),obs=pd.DataFrame({BASE:pd.Categorical(['0','0','0','1']),
        'doublet_score':[.9,.8,.1,.2],'standissect_product':['0']*4},index=['c0','c1','c2','c3']))
    data.write_h5ad(source/'integrated.h5ad')
    pd.DataFrame({'cell_id':['c'+str(i) for i in range(5)],'source_id':['s']*5,'source_cell_id':['orig'+str(i) for i in range(5)]}).to_csv(source/'input_cells.csv.gz',index=False)
    pd.DataFrame({'cell':['c0','c1','c2','c3'],'recommend_removal':[True,False,False,False]}).to_csv(source/'preannotation_removal.csv',index=False)
    pd.DataFrame({'cell':['c0'],'recommend_removal':[True]}).to_csv(source/'cell_outliers.csv',index=False)
    pd.DataFrame(columns=['subcluster','recommend_removal']).to_csv(source/'minor_sibling_qc.csv',index=False)
    pd.DataFrame({'cell_id':['c4'],'reason':['sample not usable']}).to_csv(source/'sample_exclusions.csv.gz',index=False)
    pd.DataFrame(columns=['cell','reasons']).to_csv(source/'osp_removal_proposals.csv.gz',index=False)
    input_ref=immutable(tmp_path/'input.json',{})
    inspected=immutable(tmp_path/'inspected.json',{'input':input_ref,'spec':{'run_id':'test'}})
    inclusion=immutable(tmp_path/'inclusion.json',{})
    evidence=sealed(source,source/'evidence.json',inspected=inspected,inclusion=inclusion)
    entries=[dict(cluster_id=c,coarse_label='Type '+c,fine_label='Fine '+c,merge_target=None,
        action='keep' if c=='0' else 'remove',remove_reason='low-quality',confidence='high',
        evidence={k:'test evidence' for k in ('distinctness','markers','merge')},rationale='test evidence') for c in ('0','1')]
    types=immutable(tmp_path/'types.json',dict(accepted=True,evidence=evidence,proposal={'clusters':entries}))
    quality=dict(clusters=[dict(cluster=c,verdict='real',action='keep',confidence='high',
        tests={k:'test evidence' for k in ('markers','qc','composition','geometry','stability')},rationale='test evidence') for c in ('0','1')],
        cell_actions=[dict(cluster='0',metric='doublet_score',op='>',value=.5,action='drop',reason='doublet',note='specific test')])
    quality_ref=immutable(tmp_path/'quality.json',dict(accepted=True,evidence=evidence,types=types,proposal=quality))
    out=tmp_path/'out';out.mkdir();finalize(evidence,types,quality_ref,out)
    result=verified(reference(out/'final.json'));assert (result['n_input'],result['n_survived'],result['n_removed'])==(5,1,4)
    stored=an.read_h5ad(out/'annotated.h5ad');assert list(stored.obs_names)==['c2']
    ledger=pd.read_csv(out/'cell_exclusions.csv.gz').set_index('cell_uid')
    assert len(json.loads(ledger.loc['c0','reason']))==2
    assert ledger.loc['c1','source_cell_id']=='orig1'
    # a type removal records its confidence, which needs_review's 'removed' reads
    assert {'code':'low-quality','confidence':'high'}.items()<=json.loads(ledger.loc['c3','reason'])[0].items()
    assert set(stored.obs['retained_state'].astype(str))=={''}
    other=immutable(tmp_path/'bad.json',dict(accepted=True,evidence=input_ref,types=types,proposal=quality))
    with pytest.raises(ValueError,match='accepted evidence'):finalize(evidence,types,other,out)


def test_compute_comparisons_and_sql_handoff(tmp_path):
    from ecarsi.stages.crosssample import inspect_input,compute,tool,refine
    from ecarsi.stages.common import deg,deg_batch,assemble
    rng=np.random.default_rng(2024);samples=[]
    for name in ('a','b'):
        folder=tmp_path/name;folder.mkdir()
        counts=rng.poisson(1.,(75,60)).astype('float32')
        for group in range(3):counts[group*25:(group+1)*25,group*10:(group+1)*10]+=8
        data=an.AnnData(counts,obs=pd.DataFrame({'sample_id':[name]*75,'pct_counts_mt':[2.]*75,
            'n_genes_by_counts':(counts>0).sum(axis=1).astype(float),'total_counts':counts.sum(axis=1),
            'doublet_score':[.05]*75},index=[name+str(i) for i in range(75)]))
        data.layers['counts']=counts.copy();data.write_h5ad(folder/'clustered.h5ad')
        pd.DataFrame({'cell_id':data.obs_names,'source_id':[name]*75,'source_cell_id':data.obs_names}).to_csv(folder/'input_cells.csv.gz',index=False)
        save(folder/'annotation_proposal.json',{'qc_actions':[]})
        samples.append(sealed(folder,folder/'final.json',sample=name,empty=False,validation={'n_survived':75,'qc_summary':{}}))
    publication=immutable(tmp_path/'publication.json',dict(state='complete',failed_samples=[],samples=samples,n_survived=150))
    cfg=dict(batch_col='sample_id',species='human',compute_backend='cpu',n_top_genes=30,n_pcs=10,n_neighbors=10)
    inspected=tmp_path/'inspected';inspected.mkdir()
    inspect_input(dict(input=publication,config=cfg,run_id='test',max_refinements=2),inspected)
    inspected_ref=reference(inspected/'inspected.json')
    inclusion=immutable(tmp_path/'inclusion.json',dict(accepted=True,evidence=inspected_ref,
        proposal={'samples':[dict(sample=n,include=True,reason='test fixture') for n in ('a','b')],'notes':'test fixture'}))
    computed=tmp_path/'computed';computed.mkdir();compute(inspected_ref,inclusion,computed)
    prepared=reference(computed/'prepared.json');bundle=verified(prepared);n=len(bundle['tasks'])
    # the last comparison as a single request, the rest as one batch: assemble takes both shapes
    single=tmp_path/'deg-last';single.mkdir();deg(prepared,n-1,single)
    batch=tmp_path/'deg-batch';batch.mkdir();deg_batch(prepared,range(n-1),batch)
    assert sorted(p.name for p in batch.iterdir() if p.is_dir())==sorted('deg-'+str(i) for i in range(n-1)) and (batch/'results.json').is_file()
    results=[reference(batch/'results.json'),reference(single/'result.json')]
    destination=tmp_path/'evidence';destination.mkdir();assemble(prepared,results,destination)
    evidence=reference(destination/'evidence.json')
    state=immutable(tmp_path/'state.json',dict(evidence=evidence,phase='type',types=None,read=[],lookups=[],qc=False))
    args=tmp_path/'args.json';save(args,{'query':'SELECT key, count(*) FROM deg GROUP BY key'})
    result=tmp_path/'query';result.mkdir();tool('deg_sql',state['path'],args,result)
    response=read(result/'result.json');assert not response.get('is_error') and BASE in response['content']
    bad=tmp_path/'bad';bad.mkdir()
    with pytest.raises(ValueError,match='Missing or duplicate'):assemble(prepared,results[:-1],bad)
    original=an.read_h5ad(computed/'integrated.h5ad');clusters=sorted(original.obs[BASE].astype(str).unique())
    real_types=immutable(tmp_path/'report-types.json',dict(accepted=True,evidence=evidence,proposal={'clusters':[
        dict(cluster_id=c,coarse_label='Fixture',fine_label='Fixture '+c,merge_target=None,action='keep',confidence='high',
             evidence={k:'fixture evidence' for k in ('distinctness','markers','merge')},rationale='fixture evidence') for c in clusters]}))
    proposal=dict(clusters=[dict(cluster=c,verdict='real',action='keep',confidence='high',
        tests={k:'fixture evidence' for k in ('markers','qc','composition','geometry','stability')},rationale='fixture evidence') for c in clusters],cell_actions=[])
    real_quality=immutable(tmp_path/'report-quality.json',dict(accepted=True,evidence=evidence,types=real_types,proposal=proposal))
    published=tmp_path/'published';published.mkdir();finalize(evidence,real_types,real_quality,published)
    assert (published/'report.html').is_file()
    final=verified(reference(published/'final.json'));assert final['n_input']==150 and final['n_survived']+final['n_removed']==150
    typed=immutable(tmp_path/'types.json',dict(accepted=True,evidence=evidence,proposal={'clusters':[
        dict(cluster_id=c,merge_target=None,action='keep') for c in clusters]}))
    request=immutable(tmp_path/'refinement.json',dict(accepted=True,evidence=evidence,types=typed,
        refinement=dict(cluster=clusters[0],resolution=5.,reason='test refinement')))
    refined=tmp_path/'refined';refined.mkdir();refine(evidence,typed,request,refined)
    refined_bundle=verified(reference(refined/'prepared.json'))
    assert refined_bundle['version']==1 and clusters[0] not in refined_bundle['type_scope']
    assert all(c.startswith(clusters[0]+',') for c in refined_bundle['type_scope'])
    assert set(refined_bundle['type_entries'])==set(clusters)-{clusters[0]}
    assert reference(computed/'integrated.h5ad')==verified(evidence)['files']['integrated.h5ad']


@pytest.mark.parametrize('batch_col,batches,harmony_runs', [
    ('mouse', ('m1', 'm2'), True),                       # the owner names the batch column (sample map batch_key)
    ('eca_batch', ('single_batch',) * 2, False),         # batch_key false: one batch, no correction
])
def test_the_batch_column_decides_harmony_and_the_experiment_stays_the_sample(tmp_path, batch_col, batches, harmony_runs):
    """Two experiments with a separate batch column, as the per-sample stage writes them: MSP corrects by that
    column (or skips Harmony for one batch value), and the next round still records the experiment."""
    from ecarsi.sample_mapping import SAMPLE_KEY
    from ecarsi.stages.crosssample import inspect_input, compute, compute_round
    rng = np.random.default_rng(7); samples = []
    for name, batch in zip(('a', 'b'), batches):
        folder = tmp_path/name; folder.mkdir()
        counts = rng.poisson(1., (60, 50)).astype('float32')
        for group in range(2): counts[group*30:(group+1)*30, group*10:(group+1)*10] += 8
        data = an.AnnData(counts, obs=pd.DataFrame({SAMPLE_KEY: [name]*60, batch_col: [batch]*60,
            'source_unit': ['src']*60, 'eca_source_cell_id': [name+str(i) for i in range(60)],
            'pct_counts_mt': [2.]*60, 'n_genes_by_counts': (counts > 0).sum(axis=1).astype(float),
            'total_counts': counts.sum(axis=1), 'doublet_score': [.05]*60}, index=[name+str(i) for i in range(60)]))
        data.layers['counts'] = counts.copy(); data.write_h5ad(folder/'clustered.h5ad')
        pd.DataFrame({'cell_id': data.obs_names, 'source_id': ['src']*60, 'source_cell_id': data.obs_names}).to_csv(folder/'input_cells.csv.gz', index=False)
        save(folder/'annotation_proposal.json', {'qc_actions': []})
        samples.append(sealed(folder, folder/'final.json', sample=name, empty=False, validation={'n_survived': 60, 'qc_summary': {}}))
    publication = immutable(tmp_path/'publication.json', dict(state='complete', failed_samples=[], samples=samples, n_survived=120))
    cfg = dict(batch_col=batch_col, species='human', compute_backend='cpu', n_top_genes=30, n_pcs=10, n_neighbors=10)
    inspected = tmp_path/'inspected'; inspected.mkdir()
    inspect_input(dict(input=publication, config=cfg, run_id='test', max_refinements=2), inspected)
    inspected_ref = reference(inspected/'inspected.json')
    inclusion = immutable(tmp_path/'inclusion.json', dict(accepted=True, evidence=inspected_ref,
        proposal={'samples': [dict(sample=n, include=True, reason='fixture') for n in ('a', 'b')], 'notes': 'fixture'}))
    computed = tmp_path/'computed'; computed.mkdir(); compute(inspected_ref, inclusion, computed)
    msp = an.read_h5ad(computed/'integrated.h5ad').uns['msp']
    assert msp['batch_col'] == batch_col
    assert (msp['harmony'] != 'skipped: single batch') == harmony_runs
    # a later round: the survivors' experiment, not their batch, is the sample of record
    survivors = an.read_h5ad(computed/'integrated.h5ad'); survivors.write_h5ad(tmp_path/'annotated_zmip.h5ad')
    source = sealed(tmp_path, tmp_path/'survivors.json', state='complete', n_survived=120)
    later = tmp_path/'later'; later.mkdir()
    inspect_input(dict(input=source, previous_round=1, config=cfg), later)
    output = tmp_path/'round2'; output.mkdir()
    import ecarsi.stages.crosssample as module
    original = module.integrate
    module.integrate = lambda *args: None
    try:
        compute_round(reference(later/'inspected.json'), output)
    finally:
        module.integrate = original
    assert set(pd.read_csv(output/'input_cells.csv.gz', dtype=str).sample_id) == {'a', 'b'}


def test_chunks_of_one_sample_skip_the_inclusion_agent(tmp_path, monkeypatch):
    """Decision 0016: chunks are random slices of a sample; nobody judges which to exclude."""
    import sys
    from ecarsi.stages import crosssample as module
    rng=np.random.default_rng(0);samples=[]
    names=['A__all__0123456789.chunk01','A__all__0123456789.chunk02']
    for name in names:
        folder=tmp_path/name;folder.mkdir()
        counts=rng.poisson(1.,(20,10)).astype('float32')
        data=an.AnnData(counts,obs=pd.DataFrame({'sample_id':[name]*20},index=[name+str(i) for i in range(20)]))
        data.layers['counts']=counts.copy();data.write_h5ad(folder/'clustered.h5ad')
        pd.DataFrame({'cell_id':data.obs_names,'source_id':['A']*20,'source_cell_id':data.obs_names}).to_csv(folder/'input_cells.csv.gz',index=False)
        save(folder/'annotation_proposal.json',{'qc_actions':[]})
        samples.append(sealed(folder,folder/'final.json',sample=name,empty=False,validation={'n_survived':20,'qc_summary':{}}))
    publication=immutable(tmp_path/'publication.json',dict(state='complete',failed_samples=[],samples=samples,n_survived=40))
    inspected=tmp_path/'inspected';inspected.mkdir()
    module.inspect_input(dict(input=publication,config={},run_id='test',max_refinements=2),inspected)
    assert read(inspected/'inspected.json')['chunked'] is True
    out=tmp_path/'include';out.mkdir();monkeypatch.chdir(out)
    monkeypatch.setattr(sys,'argv',['crosssample','include-single',str(inspected/'inspected.json')])
    module.main()
    decision=read(out/'decision.json')
    assert sorted(s['sample'] for s in decision['proposal']['samples'])==names
    assert all(s['include'] for s in decision['proposal']['samples']) and 'chunked' in decision['proposal']['notes']


def finalize_fixture(tmp_path, monkeypatch, policy, base=None, entries=None, extra_obs=None):
    """Eight cells in two clusters; fragment f1 was removed only by its dissociation test, f2 by its doublet
    test, and OSP advised dropping c4 for dissociation stress. compute() turns that advice into a flag under
    the policy keep, so the pre-annotation mask holds c4 only under remove."""
    import msp.annotate,msp.report
    monkeypatch.setattr(msp.annotate,'_plot',lambda *a:None)
    monkeypatch.setattr(msp.report,'generate_report',lambda *a:None)
    source=tmp_path/'source';source.mkdir()
    cells=['c'+str(i) for i in range(8)]
    obs=pd.DataFrame({BASE:pd.Categorical(base or ['0']*4+['1']*4),'doublet_score':[.1]*8,
        'standissect_product':['f1','f1','f2']+['core']*5,'_qc_action':['keep']*4+['flag' if policy=='keep' else 'drop']+['keep']*3,
        **(extra_obs or {})},index=cells)
    an.AnnData(np.ones((8,2)),obs=obs).write_h5ad(source/'integrated.h5ad')
    pd.DataFrame({'cell_id':cells,'source_id':['s']*8,'source_cell_id':['orig'+c for c in cells]}).to_csv(source/'input_cells.csv.gz',index=False)
    pre=['c0','c1','c2']+(['c4'] if policy=='remove' else [])
    pd.DataFrame({'cell':cells,'recommend_removal':[c in pre for c in cells]}).to_csv(source/'preannotation_removal.csv',index=False)
    pd.DataFrame({'subcluster':['f1','f2'],'recommend_removal':[True,True],'dissociation_significant':[True,False],
        'doublet_significant':[False,True],'decontX_significant':[False,False],'mt_significant':[False,False],
        'pct_drop_upstream':[0.,0.]}).to_csv(source/'minor_sibling_qc.csv',index=False)
    pd.DataFrame(columns=['cell_id','reason']).to_csv(source/'sample_exclusions.csv.gz',index=False)
    pd.DataFrame({'cell':['c4'],'reasons':[json.dumps([{'action':{'reason':'dissociation-stress'},'evidence':{}}])]}).to_csv(source/'osp_removal_proposals.csv.gz',index=False)
    spec={'run_id':'test','config':{'stress_policy':policy}}
    inspected=immutable(tmp_path/'inspected.json',{'input':immutable(tmp_path/'input.json',{}),'spec':spec})
    return source,sealed(source,source/'evidence.json',inspected=inspected,inclusion=immutable(tmp_path/'inclusion.json',{}))


def keep_entry(c, **changes):
    return {**dict(cluster_id=c,coarse_label='Type '+c,fine_label='Fine '+c,merge_target=None,action='keep',remove_reason=None,
        confidence='high',evidence={k:'test evidence' for k in ('distinctness','markers','merge')},rationale='test evidence'),**changes}


@pytest.mark.parametrize('policy',['remove','keep'])
def test_the_stress_policy_keep_retains_dissociation_fragments_and_osp_stress_drops(tmp_path,monkeypatch,policy):
    """Decision 0017: under keep, cells removed only by a fragment's dissociation test or by OSP's
    dissociation-stress advice stay, labelled; a doublet fragment goes either way."""
    source,evidence=finalize_fixture(tmp_path,monkeypatch,policy)
    types=immutable(tmp_path/'types.json',dict(accepted=True,evidence=evidence,proposal={'clusters':[keep_entry('0'),keep_entry('1')]}))
    quality=dict(clusters=[dict(cluster=c,verdict='real',action='keep',confidence='high',
        tests={k:'test evidence' for k in ('markers','qc','composition','geometry','stability')},rationale='test evidence') for c in ('0','1')],cell_actions=[])
    quality_ref=immutable(tmp_path/'quality.json',dict(accepted=True,evidence=evidence,types=types,proposal=quality))
    out=tmp_path/'out';out.mkdir();finalize(evidence,types,quality_ref,out)
    stored=an.read_h5ad(out/'annotated.h5ad').obs['retained_state'].astype(str)
    ledger=pd.read_csv(out/'cell_exclusions.csv.gz').set_index('cell_uid')
    codes={c:[r['code'] for r in json.loads(ledger.loc[c,'reason'])] for c in ledger.index}
    assert codes['c2']==['fragment_qc']
    if policy=='keep':
        assert set(ledger.index)=={'c2'} and stored[['c0','c1','c4']].eq('dissociation').all() and stored.drop(['c0','c1','c4']).eq('').all()
    else:
        assert set(ledger.index)=={'c0','c1','c2','c4'} and codes['c4']==['osp_proposal'] and stored.eq('').all()


def test_a_type_removal_the_code_cannot_support_stays_and_leaves_its_merge(tmp_path,monkeypatch):
    """Decision 0017 in the type phase: a stress removal of a cluster stress_clusters.csv marks stands; one it
    does not mark stays, and drops its merge into the cluster still removed; a removal under 10 cells stands."""
    from ecarsi.stages.crosssample import _stress_guard
    base=['0']*12+['1']*12+['2']*5
    source,evidence=finalize_fixture(tmp_path,monkeypatch,'remove')
    pd.DataFrame({'key':[BASE,BASE],'cluster':['0','1'],'view':['global']*2,'n_hits':[5,1],'hit_genes':['','']
        ,'stress':[True,False],'recommend_removal':[True,False]}).to_csv(source/'stress_clusters.csv',index=False)
    bundle=verified(sealed(source,source/'evidence2.json',inspected=verified(evidence)['inspected']))
    data=an.AnnData(np.ones((29,2)),obs=pd.DataFrame({BASE:base},index=['x'+str(i) for i in range(29)]))
    entries={'0':keep_entry('0',action='remove',remove_reason='stress'),
             '1':keep_entry('1',action='remove',remove_reason='stress',merge_target='0',fine_label='Fine 0',coarse_label='Type 0'),
             '2':keep_entry('2',action='remove',remove_reason='stress')}
    converted=_stress_guard(bundle,data,entries,dict(entries))
    assert converted==['1'] and entries['1']['action']=='keep' and entries['1']['merge_target'] is None
    assert entries['1']['requested_merge_target']=='0' and entries['1']['host_adjustment']['n_cells']==12
    assert entries['0']['action']=='remove' and 'host_evidence' in entries['0'] and entries['2']['action']=='remove'


@pytest.mark.parametrize('policy',['remove','keep'])
def test_compute_turns_osp_dissociation_stress_drops_into_flags_under_keep(tmp_path,monkeypatch,policy):
    from ecarsi.stages import crosssample as module
    folder=tmp_path/'a';folder.mkdir()
    counts=np.ones((6,4),dtype='float32')
    data=an.AnnData(counts,obs=pd.DataFrame({'sample_id':['a']*6,'leiden':['0','0','1','1','2','2'],
        '_qc_action':['drop']*4+['keep']*2},index=['a'+str(i) for i in range(6)]))
    data.layers['counts']=counts.copy();data.write_h5ad(folder/'clustered.h5ad')
    pd.DataFrame({'cell_id':data.obs_names,'source_id':['src']*6,'source_cell_id':data.obs_names}).to_csv(folder/'input_cells.csv.gz',index=False)
    save(folder/'annotation_proposal.json',{'cluster_key':'leiden','qc_actions':[
        dict(cluster='0',scope='cluster',action='drop',reason='dissociation-stress',note='fixture'),
        dict(cluster='1',scope='cluster',action='drop',reason='doublet',note='fixture')]})
    sample=sealed(folder,folder/'final.json',sample='a',empty=False,validation={'n_survived':6,'qc_summary':{}})
    publication=immutable(tmp_path/'publication.json',dict(state='complete',failed_samples=[],samples=[sample],n_survived=6))
    inspected=tmp_path/'inspected';inspected.mkdir()
    module.inspect_input(dict(input=publication,config={'batch_col':'sample_id','stress_policy':policy},run_id='test',max_refinements=2),inspected)
    ref=reference(inspected/'inspected.json')
    inclusion=immutable(tmp_path/'inclusion.json',dict(accepted=True,evidence=ref,proposal={'samples':[dict(sample='a',include=True,reason='fixture')],'notes':'fixture'}))
    seen={}
    monkeypatch.setattr(module,'integrate',lambda data,*a:seen.update(action=data.obs['_qc_action'].astype(str).to_dict()))
    out=tmp_path/'out';out.mkdir();module.compute(ref,inclusion,out)
    assert [seen['action']['a'+str(i)] for i in range(6)]==(['flag','flag'] if policy=='keep' else ['drop','drop'])+['drop','drop','keep','keep']
