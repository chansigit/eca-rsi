"""Scientific fixture checks; these proposals are fixtures, not live acceptance."""
import json

import anndata as an
import numpy as np
import pandas as pd
import scipy.sparse as sp

from ecarsi.agent.session import immutable, reference, verified
from ecarsi.stages.persample import sealed
from ecarsi.stages.zoomin import prepare, markers, subset, compute, deg, assemble, apply_lineage, merge, tool
from ecarsi.warm_pool.state import save


def test_zoom_handoffs_and_exact_global_conservation(tmp_path):
    rng=np.random.default_rng(12);n=160
    counts=rng.poisson(1.,(n,60)).astype('float32')
    counts[:80,:15]+=6;counts[80:,15:30]+=6
    obs=pd.DataFrame({'msp_ann_coarse':['Epithelial']*80+['Immune']*80,
        'msp_ann_fine':['epithelial']*80+['immune']*80,'sample_id':['a','b']*80,
        'source_unit':['source']*n,'eca_source_cell_id':[f'original-{i}' for i in range(n)],
        'pct_counts_mt':[2.]*n,'n_genes_by_counts':(counts>0).sum(axis=1).astype(float),
        'total_counts':counts.sum(axis=1),'doublet_score':[.05]*n},index=[f'cell-{i}' for i in range(n)])
    data=an.AnnData(np.log1p(counts),obs=obs);data.layers['counts']=counts.copy()
    data.obsp['connectivities']=sp.eye(n,format='csr')
    data.obsm['X_umap']=rng.normal(size=(n,2)).astype('float32')
    source=tmp_path/'source';source.mkdir();data.write_h5ad(source/'annotated.h5ad')
    publication=sealed(source,source/'final.json',state='complete',n_survived=n)
    cfg=dict(batch_col='sample_id',species='human',tissue='fixture',min_cells=20,
             max_refinements=2,compute_backend='cpu',n_top_genes=30,n_pcs=10,n_neighbors=10)
    def folder(name):
        p=tmp_path/name;p.mkdir();return p
    prep=folder('prepare');prepare(dict(input=publication,config=cfg,run_id='test'),prep)
    prepared=reference(prep/'prepared.json')
    plan=immutable(tmp_path/'plan.json',dict(accepted=True,evidence=prepared,proposal={'lineages':[
        dict(name='Epithelial',coarse_labels=['Epithelial'],zoom=True,n_cells=80),
        dict(name='Immune',coarse_labels=['Immune'],zoom=False,n_cells=80)]}))
    shared=folder('markers');markers(prepared,plan,shared)
    part=folder('subset');subset(prepared,plan,0,part)
    output=folder('compute');compute(reference(part/'subset.json'),reference(shared/'markers.json'),output)
    ref=reference(output/'prepared.json');bundle=verified(ref)
    comparisons=[]
    for i in range(len(bundle['tasks'])):
        dest=folder('deg-'+str(i));deg(ref,i,dest);comparisons.append(reference(dest/'result.json'))
    evidence_dir=folder('evidence');assemble(ref,comparisons,evidence_dir)
    evidence=reference(evidence_dir/'evidence.json')
    ad=an.read_h5ad(output/'integrated.h5ad')
    from zmip.scheduled import TYPE_KEY,QUALITY_KEY,partitions
    groups=sorted(ad.obs[TYPE_KEY].astype(str).unique())
    types=dict(cluster_key=TYPE_KEY,clusters=[dict(cluster_id=c,coarse_label='Epithelial',fine_label='type '+c,
        merge_target=None,action='keep',confidence='high',evidence={k:'fixture evidence' for k in ('distinctness','markers','foreign','merge')},
        rationale='fixture identity') for c in groups])
    quality=dict(cluster_key=QUALITY_KEY,clusters=[dict(cluster_id=str(q),decisions=[dict(type_clusters=list(row.index[row.gt(0)]),
        action='keep',confidence='high',evidence='fixture QC',rationale='fixture retention')]) for q,row in partitions(ad.obs).iterrows()])
    decision=immutable(tmp_path/'decision.json',dict(accepted=True,evidence=evidence,types=types,quality=quality))
    applied=folder('apply');apply_lineage(evidence,decision,applied)
    merged=folder('merge');merge(prepared,plan,[reference(applied/'final.json')],merged)
    final=verified(reference(merged/'final.json'));kept=an.read_h5ad(merged/'annotated_zmip.h5ad')
    ledger=pd.read_csv(merged/'cell_exclusions.csv.gz',dtype=str,keep_default_na=False)
    assert final['n_input']==n and len(kept)+len(ledger)==n
    assert set(kept.obs_names)|set(ledger.cell_uid)==set(data.obs_names)
    assert set(data.obs_names[80:])<=set(kept.obs_names)  # skipped lineage stays
    state=immutable(tmp_path/'state.json',dict(evidence=evidence,kind='lineage',read=[],lookups=[],qc=False,types=None,quality=None))
    args=tmp_path/'args.json';save(args,{})
    status=folder('status');tool('annotation_status',state['path'],str(args),status)
    result=json.loads((status/'result.json').read_text());assert not result.get('is_error')
    assert result['intersections']

    import copy
    removal=copy.deepcopy(quality)
    for group in removal['clusters']:
        for item in group['decisions']:item.update(action='remove',remove_reason='dying')
    review_state=immutable(tmp_path/'review-state.json',dict(evidence=evidence,kind='lineage',types=types,
        types_complete=True,quality=None,read=['figures/test.png'],lookups=[{'key':QUALITY_KEY}],qc=True))
    save(args,{'proposal_json':json.dumps(removal)})
    missing_qc=immutable(tmp_path/'missing-qc-state.json',{**verified(review_state),'qc':False})
    missing=folder('missing-qc');tool('submit_quality',missing_qc['path'],str(args),missing)
    rejected=json.loads((missing/'result.json').read_text())
    assert rejected['is_error'] and rejected['content'].startswith('Complete required checks: check_qc_scores\n')  # a hint follows
    assert verified(rejected['state'])['types_complete']
    first=folder('first-review');tool('submit_quality',review_state['path'],str(args),first)
    warning=json.loads((first/'result.json').read_text())
    assert warning['is_error'] and 'confirmed dissociation/dying' in warning['content']
    removal['removal_review']='Fixture second review: the exact QC intersections have decisive dying-cell evidence.'
    save(args,{'proposal_json':json.dumps(removal)})
    second=folder('second-review');tool('submit_quality',warning['state']['path'],str(args),second)
    confirmed=json.loads((second/'result.json').read_text());assert not confirmed.get('is_error')
    assert verified(confirmed['state'])['quality']['removal_review']==removal['removal_review']

    from ecarsi.stages.zoomin import refine_evidence
    from ecarsi.stages.contract import NO_ARGUMENTS
    refinement_state=dict(evidence=evidence,types=types,types_complete=True,quality=quality,read=[],lookups=[{'key':QUALITY_KEY}],qc=True)
    destination=folder('refinement')
    target=str(ad.obs[QUALITY_KEY].value_counts().idxmax())
    result=refine_evidence(refinement_state,dict(target='quality',cluster=target,resolution=5.,reason='fixture mixed QC'),destination)
    assert result['version']==1 and refinement_state['types_complete']
    assert refinement_state['type_scope']==[] and refinement_state['quality'] is None
    refined_bundle=verified(refinement_state['evidence'])
    refined=an.read_h5ad(refined_bundle['files']['integrated.h5ad']['path'])
    pd.testing.assert_series_equal(refined.obs[TYPE_KEY],ad.obs[TYPE_KEY])
    assert target not in set(refined.obs[QUALITY_KEY])
    from ecarsi.stages.zoomin import agent_spec
    from ecarsi.agent.session import validate_spec
    pool=tmp_path/'pool';pool.mkdir(mode=0o700);save(pool/'config.json',{})
    bridge=tmp_path/'bridge';bridge.mkdir(mode=0o700);save(bridge/'config.json',{})
    budget=dict(cpus=1,memory_mb=4096,timeout_seconds=600)
    spec=dict(run_id='zoom-schema',dataset_id='fixture',config=cfg,pool_root=str(pool),bridge_root=str(bridge),
              output_root=str(tmp_path/'agents'),tool_budget=budget,compute_budget=budget)
    for kind,ref in [('plan',prepared),('lineage',evidence)]:
        registered=validate_spec(agent_spec(spec,ref,kind,'parent'))
        assert registered['completion_tool']==('submit_plan' if kind=='plan' else 'submit_quality')
        names=[t['name'] for t in registered['tools']]
        assert 'finalize_annotation' not in names and 'Required order' in registered['prompt']
        assert all(t['parameters']==NO_ARGUMENTS for t in registered['tools'] if t['name'] in {'list_evidence','annotation_status'})
        assert ('Pending type clusters' in registered['prompt'])==(kind=='lineage')
