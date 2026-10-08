"""Scientific fixture checks; these proposals are fixtures, not live acceptance."""
import json

import anndata as an
import numpy as np
import pandas as pd
import scipy.sparse as sp

from ecarsi.agent.session import immutable, reference, verified
from ecarsi.stages.common import sealed
from ecarsi.stages.zoomin import prepare, markers, subset, compute, deg, assemble, apply_lineage, merge, tool
from ecarsi.files import save


def lineage(tmp_path):
    """One zoomed lineage (Epithelial) and one kept as is (Immune), assembled to lineage evidence; every
    type and quality decision keep."""
    rng = np.random.default_rng(12)
    n = 160
    counts=rng.poisson(1.,(n,60)).astype('float32')
    counts[:80, :15] += 6
    counts[80:, 15:30] += 6
    obs=pd.DataFrame({'msp_ann_coarse':['Epithelial']*80+['Immune']*80,
        'msp_ann_fine':['epithelial']*80+['immune']*80,'sample_id':['a','b']*80,
        'source_unit':['source']*n,'eca_source_cell_id':[f'original-{i}' for i in range(n)],
        'pct_counts_mt':[2.]*n,'n_genes_by_counts':(counts>0).sum(axis=1).astype(float),
        'total_counts':counts.sum(axis=1),'doublet_score':[.05]*n},index=[f'cell-{i}' for i in range(n)])
    data = an.AnnData(np.log1p(counts), obs=obs)
    data.layers['counts'] = counts.copy()
    data.obsp['connectivities']=sp.eye(n,format='csr')
    data.obsm['X_umap']=rng.normal(size=(n,2)).astype('float32')
    source = tmp_path / 'source'
    source.mkdir()
    data.write_h5ad(source / 'annotated.h5ad')
    publication=sealed(source,source/'final.json',state='complete',n_survived=n)
    cfg=dict(batch_col='sample_id',species='human',tissue='fixture',min_cells=20,
             max_refinements=2,compute_backend='cpu',n_top_genes=30,n_pcs=10,n_neighbors=10)
    def folder(name):
        p = tmp_path / name
        p.mkdir()
        return p

    prep = folder('prepare')
    prepare(dict(input=publication, config=cfg, run_id='test'), prep)
    prepared=reference(prep/'prepared.json')
    plan=immutable(tmp_path/'plan.json',dict(accepted=True,evidence=prepared,proposal={'lineages':[
        dict(name='Epithelial',coarse_labels=['Epithelial'],zoom=True,n_cells=80),
        dict(name='Immune',coarse_labels=['Immune'],zoom=False,n_cells=80)]}))
    shared = folder('markers')
    markers(prepared, plan, shared)
    part = folder('subset')
    subset(prepared, plan, 0, part)
    output = folder('compute')
    compute(reference(part / 'subset.json'), reference(shared / 'markers.json'), output)
    ref = reference(output / 'prepared.json')
    bundle = verified(ref)
    comparisons=[]
    for i in range(len(bundle['tasks'])):
        dest = folder('deg-' + str(i))
        deg(ref, i, dest)
        comparisons.append(reference(dest / 'result.json'))
    evidence_dir = folder('evidence')
    assemble(ref, comparisons, evidence_dir)
    evidence=reference(evidence_dir/'evidence.json')
    ad=an.read_h5ad(output/'integrated.h5ad')
    from zmip.scheduled import TYPE_KEY,QUALITY_KEY,partitions
    groups=sorted(ad.obs[TYPE_KEY].astype(str).unique())
    types=dict(cluster_key=TYPE_KEY,clusters=[dict(cluster_id=c,coarse_label='Epithelial',fine_label='type '+c,
        merge_target=None,action='keep',confidence='high',evidence={k:'fixture evidence' for k in ('distinctness','markers','foreign','merge')},
        rationale='fixture identity') for c in groups])
    quality=dict(cluster_key=QUALITY_KEY,clusters=[dict(cluster_id=str(q),decisions=[dict(type_clusters=list(row.index[row.gt(0)]),
        action='keep',confidence='high',evidence='fixture QC',rationale='fixture retention')]) for q,row in partitions(ad.obs).iterrows()])
    return dict(n=n,data=data,cfg=cfg,folder=folder,prepared=prepared,plan=plan,evidence=evidence,ad=ad,types=types,quality=quality)


def test_zoom_handoffs_and_exact_global_conservation(tmp_path):
    from zmip.scheduled import TYPE_KEY,QUALITY_KEY
    built=lineage(tmp_path)
    n,data,cfg,folder,prepared,plan,evidence,ad,types,quality=(built[k] for k in
        ('n','data','cfg','folder','prepared','plan','evidence','ad','types','quality'))
    decision=immutable(tmp_path/'decision.json',dict(accepted=True,evidence=evidence,types=types,quality=quality))
    applied = folder('apply')
    apply_lineage(evidence, decision, applied)
    merged = folder('merge')
    merge(prepared, plan, [reference(applied / 'final.json')], merged)
    final = verified(reference(merged / 'final.json'))
    kept = an.read_h5ad(merged / 'annotated_zmip.h5ad')
    ledger=pd.read_csv(merged/'cell_exclusions.csv.gz',dtype=str,keep_default_na=False)
    assert final['n_input']==n and len(kept)+len(ledger)==n
    assert set(kept.obs_names)|set(ledger.cell_uid)==set(data.obs_names)
    assert set(data.obs_names[80:])<=set(kept.obs_names)  # skipped lineage stays
    # the lineage page is drawn and published where the round report links it (#26)
    assert (applied/'report.html').is_file() and (merged/'Epithelial'/'report.html').is_file()
    state=immutable(tmp_path/'state.json',dict(evidence=evidence,kind='lineage',read=[],lookups=[],qc=False,types=None,quality=None))
    args = tmp_path / 'args.json'
    save(args, {})
    status = folder('status')
    tool('annotation_status', state['path'], str(args), status)
    result = json.loads((status / 'result.json').read_text())
    assert not result.get('is_error')
    assert result['intersections']

    import copy
    removal=copy.deepcopy(quality)
    for group in removal['clusters']:
        for item in group['decisions']:
            item.update(action='remove', remove_reason='low-quality')
    review_state=immutable(tmp_path/'review-state.json',dict(evidence=evidence,kind='lineage',types=types,
        types_complete=True,quality=None,read=['figures/test.png'],lookups=[{'key':QUALITY_KEY}],qc=True))
    save(args,{'proposal_json':json.dumps(removal)})
    missing_qc=immutable(tmp_path/'missing-qc-state.json',{**verified(review_state),'qc':False})
    missing = folder('missing-qc')
    tool('submit_quality', missing_qc['path'], str(args), missing)
    rejected=json.loads((missing/'result.json').read_text())
    assert rejected['is_error'] and rejected['content'].startswith('Complete required checks: check_qc_scores\n')  # a hint follows
    assert verified(rejected['state'])['types_complete']
    first = folder('first-review')
    tool('submit_quality', review_state['path'], str(args), first)
    warning=json.loads((first/'result.json').read_text())
    assert warning['is_error'] and 'under the stress policy' in warning['content']
    removal['removal_review']='Fixture second review: the exact QC intersections have decisive dying-cell evidence.'
    save(args,{'proposal_json':json.dumps(removal)})
    second = folder('second-review')
    tool('submit_quality', warning['state']['path'], str(args), second)
    confirmed = json.loads((second / 'result.json').read_text())
    assert not confirmed.get('is_error')
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

    pool = tmp_path / 'pool'
    pool.mkdir(mode=0o700)
    save(pool / 'config.json', {})
    bridge = tmp_path / 'bridge'
    bridge.mkdir(mode=0o700)
    save(bridge / 'config.json', {})
    budget=dict(cpus=1,memory_mb=4096,timeout_seconds=600)
    spec=dict(run_id='zoom-schema',dataset_id='fixture',config=cfg,pool_root=str(pool),bridge_root=str(bridge),
              output_root=str(tmp_path/'agents'),tool_budget=budget,compute_budget=budget)
    for kind,ref in [('plan',prepared),('lineage',evidence)]:
        registered=validate_spec(agent_spec(spec,ref,kind,'parent'))
        assert registered['completion_tool']==('submit_plan' if kind=='plan' else 'submit_quality')
        names=[t['name'] for t in registered['tools']]
        assert 'finalize_annotation' not in names and 'Required order' in registered['prompt']
        assert all(t['parameters']==NO_ARGUMENTS for t in registered['tools'] if t['name']=='annotation_status')
        assert next(t['parameters'] for t in registered['tools'] if t['name']=='list_evidence')=={'type':'object','properties':{'offset':{'type':'integer','minimum':0}},'required':['offset'],'additionalProperties':False}
        assert ('Pending type clusters' in registered['prompt'])==(kind=='lineage')
        assert ('Stress policy: remove' in registered['prompt'])==(kind=='lineage')
        reads={t['name'] for t in registered['tools'] if t.get('read_only')}
        assert {'read_evidence','list_evidence'}<=reads and ('annotation_status' in reads)==(kind=='lineage') and registered['completion_tool'] not in reads



def test_an_unsupported_stress_removal_stays_labelled_through_merge(tmp_path, monkeypatch):
    """Decision 0017: a stress removal of at least 10 cells whose cluster the stress-gene rule does not mark
    (the fixture has no stress genes) stays; its cells carry retained_state into annotated_zmip.h5ad, and the
    tool reply says so. A smaller one stands on the agent's reason. The fixture's clusters hold at most 8
    cells, so the floor is 5 here."""
    import copy
    import ecarsi.stages.common as common
    monkeypatch.setattr(common,'CHECK_MIN_CELLS',5)
    from zmip.scheduled import QUALITY_KEY,TYPE_KEY,partitions

    built = lineage(tmp_path)
    folder, evidence, ad, types = built['folder'], built['evidence'], built['ad'], built['types']
    table = partitions(ad.obs)
    sizes = table.stack()
    sizes = sizes[sizes.gt(0)].sort_values()
    big,small=sizes.index[-1],sizes.index[0]
    assert sizes.iloc[-1]>=5 and sizes.iloc[0]<5
    quality=copy.deepcopy(built['quality'])
    for group in quality['clusters']:
        row = table.loc[group['cluster_id']]
        present = list(row.index[row.gt(0)])
        group['decisions']=[dict(type_clusters=[t],action='remove' if (group['cluster_id'],t) in (big,small) else 'keep',
            **({'remove_reason':'stress'} if (group['cluster_id'],t) in (big,small) else {}),
            confidence='high',evidence='fixture',rationale='fixture') for t in present]
    state=immutable(tmp_path/'stress-state.json',dict(evidence=evidence,kind='lineage',types=types,types_complete=True,
        quality=None,read=['figures/test.png'],lookups=[{'key':QUALITY_KEY}],qc=True))
    args = tmp_path / 'stress-args.json'
    save(args, {'proposal_json': json.dumps(quality)})
    out = folder('stress-review')
    tool('submit_quality', state['path'], str(args), out)
    reply = json.loads((out / 'result.json').read_text())
    assert not reply.get('is_error'), reply
    assert 'Kept under the stress policy' in reply['content']
    accepted=verified(reply['state'])['quality']
    decided={(g['cluster_id'],e['type_clusters'][0]):e for g in accepted['clusters'] for e in g['decisions']}
    assert decided[big]['action']=='keep' and decided[big]['host_adjustment']['state']=='stress'
    assert decided[small]['action']=='remove'
    decision=immutable(tmp_path/'stress-decision.json',dict(accepted=True,evidence=evidence,types=types,quality=accepted))
    applied = folder('stress-apply')
    apply_lineage(evidence, decision, applied)
    target=set(ad.obs_names[ad.obs[QUALITY_KEY].astype(str).eq(big[0])&ad.obs[TYPE_KEY].astype(str).eq(big[1])])
    retained=pd.read_csv(applied/'annotation_retained.csv',dtype=str)
    assert set(retained.cell)<=target and set(retained.state)=={'stress'} and len(retained)
    merged = folder('stress-merge')
    merge(built['prepared'], built['plan'], [reference(applied / 'final.json')], merged)
    kept=an.read_h5ad(merged/'annotated_zmip.h5ad',backed='r')
    states=kept.obs['retained_state'].astype(str)
    assert set(states[states.ne('')].index)==set(retained.cell)
    kept.file.close()
