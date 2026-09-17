"""Worker handoffs and per-cell exclusion accounting, with real AnnData I/O."""
import json

import anndata as an
import numpy as np
import pandas as pd
import pytest

from ecarsi.bridge.session import immutable,reference,validate_spec,verified
from ecarsi.stages.crosssample import BASE,agent_spec,finalize
from ecarsi.stages.persample import sealed
from ecarsi.warm_pool.state import save,read


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
    other=immutable(tmp_path/'bad.json',dict(accepted=True,evidence=input_ref,types=types,proposal=quality))
    with pytest.raises(ValueError,match='accepted evidence'):finalize(evidence,types,other,out)


def test_compute_comparisons_and_sql_handoff(tmp_path):
    from ecarsi.stages.crosssample import inspect_input,compute,deg,assemble,tool,refine
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
    prepared=reference(computed/'prepared.json');bundle=verified(prepared);results=[]
    for i in range(len(bundle['tasks'])):
        destination=tmp_path/('deg'+str(i));destination.mkdir();deg(prepared,i,destination)
        results.append(reference(destination/'result.json'))
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
