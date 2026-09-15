"""Versioned cross-sample worker operations; scientific kernels live in MSP."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil

from .agent_session import immutable, reference, verified
from .persample_v2 import check_bundle, sealed
from .warm_pool.state import digest, read, save

BASE = 'msp_leiden_r2.0'


def artifact(bundle, name):
    ref = bundle['files'][name]
    if reference(ref['path']) != ref:
        raise ValueError('Evidence artifact changed: ' + name)
    return Path(ref['path'])


def publish_bundle(destination, name, parent=None, **metadata):
    files = {k:v for k,v in verified(parent)['files'].items() if not any(p.startswith('.') for p in Path(k).parts)} if parent else {}
    files.update({str(p.relative_to(destination)): reference(p) for p in sorted(destination.rglob('*'))
                  if p.is_file() and p.name != name and not any(part.startswith('.') for part in p.relative_to(destination).parts)})
    return immutable(destination / name, {**metadata, 'files': files})


def inspect_input(spec, destination):
    """Verify OSP publication and prepare bounded inclusion evidence, not matrices."""
    publication = verified(spec['input'])
    if publication['state'] != 'complete' or publication['failed_samples']:
        raise ValueError('Cross-sample requires a complete per-sample publication')
    samples, empty, files = [], [], {}
    for ref in publication['samples']:
        bundle = check_bundle(ref)
        if bundle['empty']:
            empty.append(ref)
            continue
        proposal = read(artifact(bundle, 'annotation_proposal.json'))
        label = bundle['sample']
        samples.append(dict(sample=label, bundle=ref, n_cells=bundle['validation']['n_survived'],
                            qc=bundle['validation']['qc_summary'], annotation=proposal))
        for name, item in bundle['files'].items():
            if name.endswith('.png'):
                files[label + '/' + name] = item
    if not samples:
        raise ValueError('No surviving samples to integrate')
    if len({s['sample'] for s in samples}) != len(samples):
        raise ValueError('Repeated sample in per-sample publication')
    if sum(s['n_cells'] for s in samples) != publication['n_survived']:
        raise ValueError('Per-sample publication cell total changed')
    # All later operations retain this accepted source chain instead of copying historical ledgers.
    immutable(destination/'inspected.json', dict(samples=samples, empty=empty, files=files,
              input=spec['input'], n_input=publication['n_survived'], spec=spec))


def compute(inspected_ref, inclusion_ref, destination):
    import pandas as pd
    from msp.integrate import load_and_merge, integrate_adata
    from .crosssample import validate_inclusion
    inspected, inclusion = verified(inspected_ref), verified(inclusion_ref)
    if inclusion.get('accepted') is not True or inclusion['evidence'] != inspected_ref:
        raise ValueError('Inclusion is not accepted for this input')
    decision = inclusion['proposal']
    validate_inclusion(decision, [s['sample'] for s in inspected['samples']])
    selected = {s['sample'] for s in decision['samples'] if s['include']}
    if not selected:
        raise ValueError('Inclusion rejected all samples; no integration is authorized')
    inputs, sources, exclusions, osp_reasons = [], [], [], {}
    reasons = {s['sample']: s['reason'] for s in decision['samples']}
    for sample in inspected['samples']:
        bundle = verified(sample['bundle'])
        import anndata as an
        data = an.read_h5ad(artifact(bundle, 'clustered.h5ad'), backed='r')
        ids = data.obs_names.copy()
        from osp.annotate import _OPS
        proposal = read(artifact(bundle,'annotation_proposal.json'))
        for action in proposal.get('qc_actions',[]):
            if action['action'] != 'drop':continue
            affected = data.obs[proposal['cluster_key']].astype(str).eq(str(action['cluster']))
            if action['scope']=='cells':
                affected &= _OPS[action['op']](data.obs[action['metric']],float(action['value']))
            for cid in data.obs_names[affected]:
                osp_reasons.setdefault(cid,[]).append({'action':action,'evidence':bundle['files']['annotation_proposal.json']})
        data.file.close()
        table = pd.read_csv(artifact(bundle, 'input_cells.csv.gz'), dtype=str, keep_default_na=False).set_index('cell_id').loc[ids].rename_axis('cell_id').reset_index()
        table['sample_id'] = sample['sample']; sources.append(table)
        if sample['sample'] in selected:
            inputs.append(str(artifact(bundle, 'clustered.h5ad')))
        else:
            table = table.copy();table['reason'] = reasons[sample['sample']]
            table['reason_code'] = 'sample_excluded';exclusions.append(table)
    origin = pd.concat(sources, ignore_index=True)
    if origin.cell_id.duplicated().any() or len(origin) != inspected['n_input']:
        raise ValueError('Cross-sample source cells must be unique and conserved')
    origin.to_csv(destination/'input_cells.csv.gz', index=False)
    pd.DataFrame([{'cell':c,'reasons':json.dumps(r)} for c,r in osp_reasons.items()],columns=['cell','reasons']).to_csv(destination/'osp_removal_proposals.csv.gz',index=False)
    excluded = pd.concat(exclusions, ignore_index=True) if exclusions else pd.DataFrame(columns=[*origin.columns,'reason','reason_code'])
    excluded.to_csv(destination/'sample_exclusions.csv.gz', index=False)
    pd.DataFrame(decision['samples']).to_csv(destination/'sample_decisions.csv',index=False)
    spec = inspected['spec'];cfg=spec['config'];backend=os.environ.get('RSI_COMPUTE_BACKEND','cpu')
    if cfg['compute_backend'] not in {'auto',backend}:
        raise ValueError('Backend does not match the Pool grant')
    os.environ['MSP_COMPUTE_ENDPOINT']='local';os.environ['MSP_COMPUTE_GPU']='1' if backend=='rapids' else '0'
    data=load_and_merge(inputs,cfg['batch_col'])
    if len(data)+len(excluded)!=len(origin):raise ValueError('Sample selection lost cells')
    integrate_adata(data,cfg['batch_col'],str(destination),species=cfg['species'],
                    resolutions=(.3,1.,2.),n_top_genes=cfg['n_top_genes'],n_pcs=cfg['n_pcs'],
                    n_neighbors=cfg['n_neighbors'],defer_deg=True,inputs=inputs,
                    meta_extra={'compute_backend':backend,'workflow':'cross-sample-v2'})
    plan=read(destination/'deg_plan.json');tasks=[]
    for index,item in enumerate(plan['plan']):
        tasks.append({'plan_index':index,'cluster':None})
        tasks.extend({'plan_index':index,'cluster':c} for c in item['valid'] if item['top3'].get(c))
    sealed(destination,destination/'prepared.json',inspected=inspected_ref,inclusion=inclusion_ref,
           version=0,tasks=tasks,n_input=len(origin),n_selected=len(data),backend=backend,
           type_entries={},quality_entries={},type_scope=sorted(data.obs[BASE].astype(str).unique()))


def deg(prepared_ref, index, destination):
    from msp.integrate.deg import load_deg_input,compute_deg_task
    bundle=verified(prepared_ref);task=bundle['tasks'][index]
    # Verify the shared read-only buffers; each process maps them without loading counts or graphs.
    for name in bundle['files']:
        if name.startswith('deg_input/'):
            artifact(bundle,name)
    directory=artifact(bundle,'deg_input/metadata.h5ad').parent
    item=read(artifact(bundle,'deg_plan.json'))['plan'][task['plan_index']]
    frame=compute_deg_task(load_deg_input(directory),item,task['cluster'])
    if frame is None:raise ValueError('A planned DEG comparison has no eligible reference')
    # Stress uses only top 10; annotation keeps the documented top 50 per view.
    frame.groupby('group',observed=True).head(50).to_csv(destination/'deg.csv',index=False)
    immutable(destination/'result.json',dict(prepared=prepared_ref,index=index,task=task,table=reference(destination/'deg.csv')))


def assemble(prepared_ref, results, destination):
    import pandas as pd
    from msp.integrate.deg import write_deg_results
    from msp.evidence import DegTables
    bundle=verified(prepared_ref);plan=read(artifact(bundle,'deg_plan.json'));out={**plan,'results':[]}
    rows=[verified(r) for r in results]
    if sorted(r['index'] for r in rows)!=list(range(len(bundle['tasks']))):
        raise ValueError('Missing or duplicate DEG results')
    by_index={r['index']:r for r in rows}
    for i,task in enumerate(bundle['tasks']):
        r=by_index[i]
        if r['prepared']!=prepared_ref or r['task']!=task or reference(r['table']['path'])!=r['table']:
            raise ValueError('DEG result belongs to different evidence')
        item=plan['plan'][task['plan_index']]
        out['results'].append((item['key'],'global' if task['cluster'] is None else 'local',task['cluster'],pd.read_csv(r['table']['path'],dtype={'group':str},keep_default_na=False)))
    for name in bundle['files']:
        if '/' not in name and name.endswith('.csv') and not name.startswith(('deg_','stress_clusters')):
            shutil.copyfile(artifact(bundle,name),destination/name)
    write_deg_results(out,plan['keys'],str(destination),top_n_de=50)
    with DegTables(destination,BASE) as tables:
        tables.write_database(destination/'deg.sqlite',provenance={'prepared':prepared_ref,'mask':bundle['files']['preannotation_removal.csv'],'coverage':'top 50 per cluster and view'})
    publish_bundle(destination,'evidence.json',prepared_ref,**{k:v for k,v in bundle.items() if k!='files'},prepared=prepared_ref,comparisons=results)


def _data(bundle):
    import anndata as an
    return an.read_h5ad(artifact(bundle,'integrated.h5ad'))


def refine(evidence_ref, types_ref, decision_ref, destination):
    from msp.inspect import _subcluster_once
    from msp.evidence import load_removal_mask
    from msp.integrate.deg import prepare_deg,save_deg_input
    from msp.integrate.qc import _qc_outputs
    from msp.plots import save_single_umap,slug
    bundle, typed, decision = verified(evidence_ref), verified(types_ref), verified(decision_ref)
    if (decision.get('accepted') is not True or not decision.get('refinement') or
            decision['evidence'] != evidence_ref or decision['types'] != types_ref or
            typed.get('accepted') is not True or typed['evidence'] != evidence_ref):
        raise ValueError('Refinement requires a validated request and matching accepted types')
    data = _data(bundle);request = decision['refinement'];parent = request['cluster']
    mask = load_removal_mask(artifact(bundle,'preannotation_removal.csv').parent,data)
    version = bundle['version'] + 1
    if version > verified(bundle['inspected'])['spec']['max_refinements']:
        raise ValueError('Refinement exceeds the configured limit')
    key = 'cross_sub_' + str(version)
    n, message = _subcluster_once(data,BASE,parent,request['resolution'],key,mask,compute_markers=False)
    if n < 2:
        raise ValueError(message + '; no new evidence version published')
    affected = set(data.obs.loc[data.obs[BASE].astype(str).eq(parent),key].astype(str))
    entries = {str(e['cluster_id']):e for e in typed['proposal']['clusters']}
    # Recheck merge partners too: their references cannot point at a retired parent.
    from msp.annotate import _components
    affected.update(c for c in _components(entries)[parent] if c != parent)
    preserved = {c:e for c,e in entries.items() if c != parent and c not in affected}
    data.obs['cross_base_v'+str(bundle['version'])] = data.obs[BASE].copy()
    data.obs[BASE] = data.obs.pop(key).cat.remove_unused_categories()
    eligible = data[~mask];keys = read(artifact(bundle,'deg_plan.json'))['keys']
    labels = {k:(eligible.obs[k].cat.codes.to_numpy(),list(eligible.obs[k].cat.categories)) for k in keys}
    values, plan = prepare_deg(eligible.X,list(data.var_names),labels,dict(data.uns.get('log1p',{})),eligible.obsm['X_pca_harmony'],keys)
    values.obs_names = eligible.obs_names.copy();save_deg_input(values,destination/'deg_input')
    save(destination/'deg_plan.json',{**plan,'keys':keys,'top_n_de':50})
    data.write_h5ad(destination/'integrated.h5ad')
    figures = destination/'figures';figures.mkdir()
    _qc_outputs(data,data.uns['msp']['batch_col'],'standissect_product',str(destination),str(figures),keys,[1.,2.])
    save_single_umap(data,BASE,str(figures/('umap_'+slug(BASE)+'.png')),repel=True)
    tasks = [dict(plan_index=i,cluster=c) for i,item in enumerate(plan['plan']) for c in [None,*[c for c in item['valid'] if item['top3'].get(c)]]]
    metadata = {k:v for k,v in bundle.items() if k not in {'files','prepared','comparisons'}}
    metadata.update(version=version,tasks=tasks,type_entries=preserved,type_scope=sorted(affected),
                    prior_types=types_ref,refinement=decision_ref)
    publish_bundle(destination,'prepared.json',evidence_ref,**metadata)


def _proposal_schema(phase):
    if phase=='inclusion':
        from .crosssample import INCLUSION_SCHEMA
        return json.dumps(INCLUSION_SCHEMA)
    if phase=='type':
        from msp.annotate import _CLUSTER_SCHEMA_DOC
        return 'Return {"clusters": [entries], "boundary_reviews": [{"coarse_labels": ["A","B"], "evidence": "...", "uncertain": false}]}. Each cluster entry: '+_CLUSTER_SCHEMA_DOC
    from msp.inspect import _PROPOSAL_SCHEMA_DOC
    return (_PROPOSAL_SCHEMA_DOC + '\nInstead of a final proposal, you may submit '
        '{"refinement": {"cluster": "id", "resolution": 1.0, "reason": "evidence and why a split is necessary"}}. '
        'The coordinator schedules this split, fresh DEG, and targeted type review before restarting quality. '
        'Do not claim a split was performed by submitting this request.')


def agent_spec(spec, evidence_ref, phase, parent, types_ref=None):
    from msp.evidence import DEG_TOOL_DOC,DEG_SQL_DOC
    bundle=verified(evidence_ref)
    props={
      'read_evidence':({'path':{'type':'string'},'offset':{'type':'integer','minimum':0}},'Read an assigned figure or up to 16000 characters of a table; use only listed paths.',True),
      'submit_decision':({'proposal_json':{'type':'string'}},'Submit a validated '+phase+' decision. '+_proposal_schema(phase),False)}
    if phase=='inclusion':
        prompt=Path(__file__).with_name('prompts').joinpath('sample_inclusion.md').read_text()
        prompt+='\nRead every sample inventory and cluster UMAP before deciding. Sample count: '+str(len(bundle['samples']))
        props['sample_inventory']=({'offset':{'type':'integer','minimum':0}},'Read the next sample inventory and its figure paths. Follow next_offset until null.',False)
    else:
        prompts=Path(__file__).with_name('prompts')
        prompt=(prompts/'crosssample-deg.md').read_text()
        prompt+='\n'+(prompts/('crosssample-'+phase+'.md')).read_text()
        props.update({
          'deg_lookup':({'key':{'type':'string'},'cluster':{'type':'string'},'gene':{'type':'string'},'view':{'type':'string','enum':['global','local','both']},'top_n':{'type':'integer','minimum':1,'maximum':200}},DEG_TOOL_DOC,False),
          'deg_sql':({'query':{'type':'string'}},DEG_SQL_DOC,False),
          'check_genes':({'genes':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':80},'cluster':{'type':'string'}},'Expression evidence on the assigned clustering; does not run DEG.',False),
          'check_deg':({'cluster':{'type':'string'},'reference':{'type':'string'},'reason':{'type':'string'}},'Request uncovered comparison only. Explain what the immutable database lacks. Results are saved on the worker.',False),
          'check_qc_scores':({},'Per-cluster QC, composition and accepted type labels.',False)})
        prompt+='\nContext: '+json.dumps(spec['config'])+'\nEvidence version: '+evidence_ref['sha256']
        prompt+='\nAssigned type clusters: '+json.dumps(bundle['type_scope'])+'\nBase key: '+BASE
        if bundle.get('type_entries'):
            prompt+='\nUse type_context for preserved type entries outside your assignment; do not resubmit them.'
        if types_ref:prompt+='\nRead the accepted type labels with type_context before assessing quality.'
        props['type_context']=({'offset':{'type':'integer','minimum':0}},'Read up to 10 accepted or preserved type entries. Follow next_offset until null.',False)
    props['list_evidence']=({'offset':{'type':'integer','minimum':0}},'List up to 30 assigned evidence paths. Follow next_offset to see remaining paths.',False)
    prompt+='\nUse worker tools for all evidence. Read figures and use the database before submission. Finish by calling submit_decision; no local execution is available.'
    state=immutable(Path(spec['output_root'])/f'{phase}-{evidence_ref["sha256"][:12]}-state.json',dict(evidence=evidence_ref,phase=phase,types=types_ref,read=[],lookups=[],qc=False))
    tools=[]
    for name,(fields,description,multimodal) in props.items():
        tools.append(dict(name=name,description=description,parameters={'type':'object','properties':fields,'required':list(fields),'additionalProperties':False},
          args=['-m','ecarsi.crosssample_v2','tool',name,'{state}','{arguments}'],**spec['tool_budget'],
          inputs=[reference(Path(__file__))],outputs=['result.json'],result_file='result.json',multimodal=multimodal))
    return dict(session_id='cross-'+digest([spec['run_id'],phase,evidence_ref,types_ref])[:24],dataset_id=spec['dataset_id'],prompt=prompt,tools=tools,
      max_turns=80,pool_root=spec['pool_root'],bridge_root=spec['bridge_root'],output_root=str(Path(spec['output_root'])/(phase+'-'+evidence_ref['sha256'][:12])),
      completion_tool='submit_decision',tool_state=state,
      trace=dict(workflow_id='cross-sample/'+spec['run_id'],dataset_id=spec['dataset_id'],unit_id='cross-sample.'+phase,depends_on=[parent]))


def tool(name,state_path,args_path,destination):
    from msp.evidence import DegTables,gene_table,qc_table,DegCache,load_removal_mask
    state=read(state_path);args=read(args_path);bundle=verified(state['evidence']);phase=state['phase'];response={}
    try:
        if name=='read_evidence':
            path=artifact(bundle,args['path'])
            if path.suffix=='.png':
                if path.stat().st_size>8*2**20:raise ValueError('Figure exceeds the image budget')
                response.update(content=args['path'],images=['data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()])
            elif path.suffix in {'.csv','.json','.md','.txt'}:
                with path.open() as stream:
                    stream.seek(args['offset']);text=stream.read(16000);nxt=stream.tell() if stream.read(1) else None
                response['content']=text;response['next_offset']=nxt
            else:raise ValueError('Use registered matrix/database tools for this artifact')
            state['read']=sorted(set(state['read'])|{args['path']})
        elif name=='list_evidence':
            names=[n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]
            offset=args['offset'];response.update(content=names[offset:offset+30],next_offset=offset+30 if offset+30<len(names) else None)
        elif name=='sample_inventory':
            offset=args['offset'];sample=bundle['samples'][offset]
            response.update(content=sample,figures=[n for n in bundle['files'] if n.startswith(sample['sample']+'/')],next_offset=offset+1 if offset+1<len(bundle['samples']) else None)
            state['inventories']=sorted(set(state.get('inventories',[]))|{sample['sample']})
        elif name=='type_context':
            entries=verified(state['types'])['proposal']['clusters'] if state['types'] else list(bundle['type_entries'].values())
            offset=args['offset'];response.update(content=entries[offset:offset+10],next_offset=offset+10 if offset+10<len(entries) else None)
            state['type_read']=sorted(set(state.get('type_read',[]))|{str(e['cluster_id']) for e in entries[offset:offset+10]})
        elif name in {'deg_lookup','deg_sql'}:
            with DegTables(database=artifact(bundle,'deg.sqlite'),base_key=BASE) as tables:
                response['content']=tables.lookup(**args) if name=='deg_lookup' else tables.sql(**args)
            state['lookups'].append(args)
        elif name=='check_genes':
            response['content']=gene_table(_data(bundle),args['genes'],BASE,[args['cluster']] if args['cluster'] else None)
        elif name=='check_qc_scores':
            data=_data(bundle);response['content']=qc_table(data,BASE,data.uns['msp']['batch_col']);state['qc']=True
        elif name=='check_deg':
            if not state['lookups'] or not args['reason'].strip():raise ValueError('Query existing evidence and explain the gap first')
            key=digest([args['cluster'],args['reference']])
            cached=state.setdefault('additional_deg',{}).get(key)
            if cached:
                response.update(verified(cached))
            else:
                data=_data(bundle);folder=artifact(bundle,'deg.sqlite').parent
                cache=DegCache(data,folder,load_removal_mask(folder,data))
                response['content']=cache.table(BASE,args['cluster'],args['reference'],30)
                response['source']='computed' if cache.n_computed else 'precomputed'
                state['additional_deg'][key]=immutable(destination/'additional_deg.json',response)
        elif name=='submit_decision':
            proposal=json.loads(args['proposal_json'])
            if phase=='inclusion':
                from .crosssample import validate_inclusion
                validate_inclusion(proposal,[s['sample'] for s in bundle['samples']])
                if set(state.get('inventories',[]))!={s['sample'] for s in bundle['samples']}:raise ValueError('Read every sample inventory before inclusion')
                missing=[s['sample'] for s in bundle['samples'] if not any(p.startswith(s['sample']+'/figures/') and 'umap_clusters' in p for p in state['read'])]
                if missing:raise ValueError('Read each sample cluster UMAP before inclusion: '+str(missing))
            else:
                data=_data(bundle);clusters=sorted(data.obs[BASE].astype(str).unique())
                if not state['lookups'] or not any(p.endswith('.png') for p in state['read']):raise ValueError('Query DEG and read a figure before submission')
                if phase=='type':
                    from msp.annotate import _validate_cluster,_validate_final,_guard_batch_annotation,_check_coarse_boundaries
                    from msp.evidence import load_paga_neighbors
                    if not isinstance(proposal.get('clusters'),list):raise ValueError('clusters must be a list')
                    problems=[p for e in proposal['clusters'] for p in _validate_cluster(e,clusters)]
                    if problems:raise ValueError('; '.join(problems))
                    proposed={str(e['cluster_id']):_guard_batch_annotation(e) for e in proposal['clusters']}
                    if set(proposed)!=set(bundle['type_scope']) or len(proposed)!=len(proposal['clusters']):raise ValueError('Cover each assigned type cluster exactly once')
                    entries={**bundle['type_entries'],**proposed}
                    problems=_validate_final(entries,clusters)
                    problems+=_check_coarse_boundaries(entries,load_paga_neighbors(artifact(bundle,'deg.sqlite').parent,BASE),proposal.get('boundary_reviews',[]))
                    proposal['clusters']=[entries[c] for c in sorted(entries)]
                else:
                    from msp.inspect import _validate_proposal,_guard_batch_actions
                    if not state['qc']:raise ValueError('Read QC evidence before quality submission')
                    accepted=verified(state['types'])
                    if accepted['evidence']!=state['evidence'] or accepted.get('accepted') is not True:raise ValueError('Type evidence is incompatible')
                    if set(state.get('type_read',[]))!=set(clusters):raise ValueError('Read accepted type context for every cluster before assessing quality')
                    if 'refinement' in proposal:
                        import math
                        request=proposal['refinement']
                        if set(request)!={'cluster','resolution','reason'} or request['cluster'] not in clusters:raise ValueError('Refinement must name a current cluster and explain the split')
                        if type(request['resolution']) not in (int,float) or not math.isfinite(request['resolution']) or request['resolution']<=0:raise ValueError('Resolution must be finite and positive')
                        if not isinstance(request['reason'],str) or not request['reason'].strip():raise ValueError('Explain the refinement evidence')
                        if bundle['version']>=verified(bundle['inspected'])['spec']['max_refinements']:raise ValueError('Refinement limit reached; submit an uncertain quality assessment using available evidence')
                        response['refinement']=request;problems=[]
                    else:
                        problems=_validate_proposal(proposal,clusters,data.obs)
                        if not problems:_guard_batch_actions(proposal)
                if problems:raise ValueError('; '.join(problems))
            response.update(accepted=True,proposal=proposal,evidence=state['evidence'],types=state['types'])
        else:raise ValueError('Unknown worker tool')
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        response={'is_error':True,'content':str(exc)[:8000]}
    response['state']=immutable(destination/'state.json',state);save(destination/'result.json',response)


def finalize(evidence_ref,types_ref,quality_ref,destination):
    import numpy as np
    import pandas as pd
    from msp.annotate import _apply,_plot,_validate_final
    from msp.inspect import _apply_proposal,_validate_proposal
    from msp.evidence import load_removal_mask
    from msp.report import generate_report
    bundle=verified(evidence_ref);typed=verified(types_ref);quality=verified(quality_ref)
    if any(r.get('accepted') is not True or r['evidence']!=evidence_ref for r in (typed,quality)) or quality['types']!=types_ref:
        raise ValueError('Decisions do not refer to this accepted evidence')
    data=_data(bundle);clusters=sorted(data.obs[BASE].astype(str).unique())
    problems=_validate_final({str(e['cluster_id']):e for e in typed['proposal']['clusters']},clusters)
    problems+=_validate_proposal(quality['proposal'],clusters,data.obs)
    if problems:raise ValueError('; '.join(problems))
    _apply_proposal(data,BASE,quality['proposal'])
    pre=load_removal_mask(artifact(bundle,'preannotation_removal.csv').parent,data)
    inspect_drop=data.obs['_msp_action'].astype(str).eq('drop').to_numpy()
    archive=_apply(data,typed['proposal'],pre|inspect_drop,{'preannotation':pre,'inspect_drop':inspect_drop})
    origin=pd.read_csv(artifact(bundle,'input_cells.csv.gz'),dtype=str,keep_default_na=False).set_index('cell_id')
    reasons={c:[] for c in archive.cell}
    # Preserve distinct numerical and inherited sources instead of a generic "filtered" reason.
    fragments=pd.read_csv(artifact(bundle,'minor_sibling_qc.csv'),keep_default_na=False)
    bad_frag=set(fragments.loc[fragments.recommend_removal.astype(str).str.lower().eq('true'),'subcluster']) if 'recommend_removal' in fragments else set()
    outliers=(pd.read_csv(artifact(bundle,'cell_outliers.csv'),dtype={'cell':str},keep_default_na=False).set_index('cell')
              if 'cell_outliers.csv' in bundle['files'] else pd.DataFrame())
    typed_entries={str(e['cluster_id']):e for e in typed['proposal']['clusters']}
    quality_entries={str(e['cluster']):e for e in quality['proposal']['clusters']}
    osp_reasons=pd.read_csv(artifact(bundle,'osp_removal_proposals.csv.gz'),dtype=str,keep_default_na=False).set_index('cell')['reasons'].to_dict()
    for cid in reasons:
        row=data.obs.loc[cid];group=str(row[BASE]);r=reasons[cid]
        if str(row.get('standissect_product')) in bad_frag:r.append({'code':'fragment_qc','evidence':bundle['files']['minor_sibling_qc.csv']})
        if cid in outliers.index and str(outliers.loc[cid].get('recommend_removal')).lower()=='true':r.append({'code':'cell_outlier','detail':outliers.loc[cid].astype(str).to_dict(),'evidence':bundle['files']['cell_outliers.csv']})
        if str(row.get('_qc_action'))=='drop':
            if cid not in osp_reasons:raise ValueError('OSP drop lacks its original decision: '+cid)
            r.append({'code':'osp_proposal','detail':json.loads(osp_reasons[cid])})
        if str(row['_msp_action'])=='drop':
            from msp.inspect import _OPS
            matching=[a for a in quality['proposal'].get('cell_actions',[]) if str(a['cluster'])==group
                      and a['action']=='drop' and _OPS[a['op']](row[a['metric']],float(a['value']))]
            r.append({'code':'quality_decision','detail':quality_entries[group],
                      'cell_actions':matching,'evidence':quality_ref})
        if typed_entries[group]['action']=='remove':r.append({'code':typed_entries[group]['remove_reason'],'detail':typed_entries[group]['rationale'],'evidence':types_ref})
        if not r:raise ValueError('An excluded cell has no reason: '+cid)
    excluded=pd.read_csv(artifact(bundle,'sample_exclusions.csv.gz'),dtype=str,keep_default_na=False)
    for row in excluded.to_dict('records'):
        reasons[row['cell_id']]=[{'code':'sample_excluded','detail':row['reason'],'evidence':bundle['inclusion']}]
    kept=data[data.obs.msp_ann_action.astype(str).eq('keep')].copy()
    if set(kept.obs_names)&set(reasons) or set(kept.obs_names)|set(reasons)!=set(origin.index):raise ValueError('Cross-sample cell conservation failed')
    ledger=origin.loc[list(reasons)].reset_index().rename(columns={'cell_id':'cell_uid'})
    ledger['reason']=ledger.cell_uid.map(lambda c:json.dumps(reasons[c],ensure_ascii=False));ledger['stage']='cross-sample';ledger['operation']='cross-sample.finalize'
    ledger['input_version']=evidence_ref['sha256'];ledger['run_id']=verified(bundle['inspected'])['spec']['run_id']
    ledger.to_csv(destination/'cell_exclusions.csv.gz',index=False)
    for name in bundle['files']:
        if name.endswith(('.csv','.png')) and name!='cell_exclusions.csv.gz':
            path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(artifact(bundle,name),path)
    save(destination/'annotation_proposal.json',typed['proposal']);save(destination/'inspection_proposal.json',{**quality['proposal'],'cluster_key':BASE})
    archive.to_csv(destination/'annotation_removed.csv',index=False);_plot(data,kept,str(destination/'figures'))
    kept.write_h5ad(destination/'annotated.h5ad');generate_report(str(destination))
    # Verify serialized identities too, before a completion bundle is emitted.
    import anndata as an
    check=an.read_h5ad(destination/'annotated.h5ad',backed='r')
    try:
        stored=pd.read_csv(destination/'cell_exclusions.csv.gz',dtype=str,keep_default_na=False)
        if (not check.obs_names.equals(kept.obs_names) or stored.cell_uid.duplicated().any() or
                set(check.obs_names)&set(stored.cell_uid) or set(check.obs_names)|set(stored.cell_uid)!=set(origin.index)
                or stored.reason.eq('').any()):
            raise ValueError('Serialized cross-sample output/ledger conservation failed')
    finally:check.file.close()
    sealed(destination,destination/'final.json',state='complete',input=verified(bundle['inspected'])['input'],evidence=evidence_ref,types=types_ref,quality=quality_ref,
           n_input=len(origin),n_survived=kept.n_obs,n_removed=len(ledger))


def main():
    import logging
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(name)s %(levelname)s %(message)s')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation');p.add_argument('args',nargs='+');a=p.parse_args();dest=Path.cwd()
    ref=lambda n:reference(a.args[n])
    if a.operation=='inspect':inspect_input(read(a.args[0]),dest)
    elif a.operation=='agent':
        spec,evidence,phase,parent,types=read(a.args[0])
        immutable(dest/'agent.json',agent_spec(spec,evidence,phase,parent,types))
    elif a.operation=='include-single':
        bundle=verified(ref(0))
        if len(bundle['samples'])!=1:raise ValueError('Automatic inclusion requires exactly one sample')
        from .crosssample import SINGLE_SAMPLE_NOTE
        immutable(dest/'decision.json',dict(accepted=True,evidence=ref(0),proposal={'samples':[dict(sample=bundle['samples'][0]['sample'],include=True,reason=SINGLE_SAMPLE_NOTE)],'notes':SINGLE_SAMPLE_NOTE}))
    elif a.operation=='compute':compute(ref(0),ref(1),dest)
    elif a.operation=='refine':refine(ref(0),ref(1),ref(2),dest)
    elif a.operation=='deg':deg(ref(0),int(a.args[1]),dest)
    elif a.operation=='assemble':assemble(ref(0),read(a.args[1]),dest)
    elif a.operation=='tool':tool(a.args[0],Path(a.args[1]),Path(a.args[2]),dest)
    elif a.operation=='finalize':finalize(ref(0),ref(1),ref(2),dest)
    else:raise ValueError('Unknown cross-sample operation')


if __name__=='__main__':main()
