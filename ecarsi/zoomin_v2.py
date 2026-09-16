"""Versioned zoom-in worker operations, using ZMIP/MSP numerical kernels."""
import argparse
import base64
import json
import os
from pathlib import Path

from .agent_session import immutable, reference, verified
from .crosssample_v2 import artifact, publish_bundle, deg, assemble
from .persample_v2 import check_bundle, sealed
from .warm_pool.state import digest, read, save


def data_from(bundle, name='integrated.h5ad'):
    import anndata as an
    return an.read_h5ad(artifact(bundle, name))


def accepted_plan(prepared, decision):
    value = verified(decision)
    if value.get('accepted') is not True or value['evidence'] != prepared:
        raise ValueError('Plan was not accepted for this input')
    return value['proposal']


def prepare(spec, destination):
    from zmip.plan import lineage_evidence
    publication = check_bundle(spec['input'])
    if publication.get('state') != 'complete':
        raise ValueError('Zoom-in requires a completed cross-sample publication')
    data = data_from(publication, 'annotated.h5ad')
    if len(data) != publication['n_survived'] or not data.obs_names.is_unique:
        raise ValueError('Cross-sample membership changed')
    cfg = spec['config']
    counts, _, _, _ = lineage_evidence(data, 'msp_ann_coarse', cfg['batch_col'], str(destination))
    sealed(destination, destination/'prepared.json', spec=spec, input=spec['input'],
           counts={str(k): int(v) for k, v in counts.n_cells.items()}, n_input=len(data))


def markers(prepared, decision, destination):
    from zmip.foreign import lineage_markers
    plan = accepted_plan(prepared, decision)
    source = verified(prepared)
    data = data_from(verified(source['input']), 'annotated.h5ad')
    owners = {label: line['name'] for line in plan['lineages'] for label in line['coarse_labels']}
    data.obs['zmip_lineage'] = data.obs.msp_ann_coarse.astype(str).map(owners).astype('category')
    result = lineage_markers(data, 'zmip_lineage', str(destination))
    sealed(destination, destination/'markers.json', prepared=prepared, decision=decision, markers=result)


def subset(prepared, decision, index, destination):
    from zmip.lineage import subset_for
    plan = accepted_plan(prepared, decision)
    line = plan['lineages'][index]
    if not line['zoom']:
        raise ValueError('The plan did not authorize this lineage computation')
    source = verified(prepared)
    data = data_from(verified(source['input']), 'annotated.h5ad')
    sub = subset_for(data, line['coarse_labels'], 'msp_ann_coarse', 'msp_ann_fine')
    if len(sub) != line['n_cells']:
        raise ValueError('Lineage membership changed')
    sub.write_h5ad(destination/'subset.h5ad')
    sealed(destination, destination/'subset.json', prepared=prepared, decision=decision, lineage=line, index=index)


def compute(subset_ref, marker_ref, destination):
    from zmip.scheduled import compute_lineage
    source, shared = check_bundle(subset_ref), check_bundle(marker_ref)
    if any(source[key] != shared[key] for key in ('prepared', 'decision')):
        raise ValueError('Lineage and markers belong to different plans')
    cfg = verified(source['prepared'])['spec']['config']
    backend = os.environ.get('RSI_COMPUTE_BACKEND', 'cpu')
    if cfg['compute_backend'] not in {'auto', backend}:
        raise ValueError('Backend differs from the Pool grant')
    os.environ['MSP_COMPUTE_ENDPOINT'] = 'local'
    os.environ['MSP_COMPUTE_GPU'] = '1' if backend == 'rapids' else '0'
    line = source['lineage']
    data = data_from(source, 'subset.h5ad')
    foreign = compute_lineage(data, line['name'], line['coarse_labels'], shared['markers'], destination,
        batch_col=cfg['batch_col'], species=cfg['species'], n_top_genes=cfg['n_top_genes'],
        n_pcs=cfg['n_pcs'], n_neighbors=cfg['n_neighbors'])
    plan = read(destination/'deg_plan.json')
    tasks = [dict(plan_index=i, cluster=c) for i, item in enumerate(plan['plan'])
             for c in [None, *[c for c in item['valid'] if item['top3'].get(c)]]]
    sealed(destination, destination/'prepared.json', source=subset_ref, shared=marker_ref,
        planning=source['prepared'], decision=source['decision'], lineage=line, version=0,
        tasks=tasks, n_input=len(data), backend=backend, foreign_columns=foreign)


def numerical_reasons(bundle, data):
    import pandas as pd
    from msp.evidence import load_removal_mask
    mask = load_removal_mask(artifact(bundle, 'preannotation_removal.csv').parent, data)
    fragments = pd.read_csv(artifact(bundle, 'minor_sibling_qc.csv'), dtype=str, keep_default_na=False)
    bad = set(fragments.loc[fragments.recommend_removal.str.lower().eq('true'), 'subcluster'])
    outliers = pd.read_csv(artifact(bundle, 'cell_outliers.csv'), dtype=str, keep_default_na=False).set_index('cell') if 'cell_outliers.csv' in bundle['files'] else pd.DataFrame()
    result = {}
    for cell in data.obs_names[mask]:
        reasons = []
        if str(data.obs.loc[cell].get('standissect_product')) in bad:
            reasons.append(dict(code='fragment_qc', evidence=bundle['files']['minor_sibling_qc.csv']))
        if cell in outliers.index and str(outliers.loc[cell].get('recommend_removal')).lower() == 'true':
            reasons.append(dict(code='cell_outlier', detail=outliers.loc[cell].to_dict(), evidence=bundle['files']['cell_outliers.csv']))
        if not reasons:
            raise ValueError('Numerical removal lacks a supported reason: ' + cell)
        result[cell] = reasons
    return result


def apply_lineage(evidence, decision, destination):
    from zmip.scheduled import apply_decisions
    bundle, accepted = check_bundle(evidence), verified(decision)
    if accepted.get('accepted') is not True or accepted['evidence'] != evidence:
        raise ValueError('Lineage decision belongs to different evidence')
    data = data_from(bundle)
    own, other = lineage_labels(bundle)
    obs, removed, reassigned, _ = apply_decisions(data.obs, accepted['types'], accepted['quality'], own, other,
        bundle['lineage']['name'], numerical_reasons(bundle, data))
    data.obs = obs
    kept = data[data.obs.msp_ann_action.astype(str).eq('keep')].copy()
    removed['reasons'] = removed.reasons.map(json.dumps)
    removed.to_csv(destination/'annotation_removed.csv', index=False)
    reassigned.to_csv(destination/'annotation_reassigned.csv', index=False)
    ledger = removed.rename(columns={'cell': 'cell_uid', 'reasons': 'reason'}).copy()
    for target, source in [('source_id','source_unit'), ('source_cell_id','eca_source_cell_id')]:
        if source not in data.obs:
            raise ValueError('Lineage input lacks original cell identity: ' + source)
        ledger[target] = ledger.cell_uid.map(data.obs[source].astype(str))
    ledger['stage'] = 'zoom-in';ledger['operation'] = 'zoom-in.apply'
    ledger['input_version'] = evidence['sha256'];ledger['decision'] = json.dumps(decision)
    ledger.to_csv(destination/'cell_exclusions.csv.gz', index=False)
    save(destination/'annotation_proposal.json', accepted)
    kept.write_h5ad(destination/'annotated.h5ad')
    import anndata as an
    disk = an.read_h5ad(destination/'annotated.h5ad', backed='r')
    try:
        if not disk.obs_names.equals(kept.obs_names):
            raise ValueError('Serialized lineage cell identities changed')
    finally:
        disk.file.close()
    sealed(destination, destination/'final.json', state='complete', evidence=evidence,
        decision=decision, lineage=bundle['lineage'], n_input=len(data), n_survived=len(kept), n_removed=len(removed))


def lineage_labels(bundle):
    plan = verified(bundle['decision'])['proposal']
    own = bundle['lineage']['coarse_labels']
    return own, sorted({label for line in plan['lineages'] for label in line['coarse_labels']} - set(own))


def merge(prepared, decision, results, destination):
    import pandas as pd
    from zmip.merge import merge_back
    plan = accepted_plan(prepared, decision)
    source = verified(prepared)
    data = data_from(verified(source['input']), 'annotated.h5ad')
    accepted, ledgers = {}, []
    for ref in results:
        result = check_bundle(ref)
        evidence = verified(result['evidence'])
        if evidence['planning'] != prepared or evidence['decision'] != decision or result['state'] != 'complete':
            raise ValueError('A lineage belongs to a different plan or is incomplete')
        name = result['lineage']['name']
        if name in accepted:
            raise ValueError('Repeated lineage result')
        accepted[name] = dict(dir=str(artifact(result, 'annotated.h5ad').parent),
            removed=pd.read_csv(artifact(result, 'annotation_removed.csv'), dtype=str, keep_default_na=False),
            reassigned=pd.read_csv(artifact(result, 'annotation_reassigned.csv'), dtype=str, keep_default_na=False))
        ledgers.append(pd.read_csv(artifact(result, 'cell_exclusions.csv.gz'), dtype=str, keep_default_na=False))
    save(destination/'zmip_plan.json', plan)
    kept, removed, _ = merge_back(data, plan, accepted, str(destination))
    ledger = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame(columns=['cell_uid','source_id','source_cell_id','reason','stage','operation','input_version'])
    if (ledger.cell_uid.duplicated().any() or set(ledger.cell_uid) != set(removed.cell)
            or set(kept.obs_names) & set(ledger.cell_uid) or set(kept.obs_names) | set(ledger.cell_uid) != set(data.obs_names)):
        raise ValueError('Global zoom-in cell conservation failed')
    ledger.to_csv(destination/'cell_exclusions.csv.gz', index=False)
    sealed(destination, destination/'final.json', state='complete', input=source['input'],
        planning=prepared, decision=decision, lineages=results, n_input=len(data), n_survived=len(kept), n_removed=len(ledger))


def agent_spec(spec, evidence, kind, parent):
    from zmip.plan import _PLAN_SCHEMA_DOC
    from zmip.annotate import _CLUSTER_SCHEMA_DOC
    from msp.evidence import DEG_TOOL_DOC, DEG_SQL_DOC
    bundle = verified(evidence)
    props = {
        'list_evidence': ({'offset': {'type':'integer','minimum':0}}, 'List 30 evidence paths per page.', False),
        'read_evidence': ({'path': {'type':'string'}, 'offset': {'type':'integer','minimum':0}}, 'Read an assigned figure or 16000 characters of text.', True)}
    if kind == 'plan':
        prompt = Path(__file__).with_name('prompts').joinpath('zoomin-plan.md').read_text()
        prompt += '\nConfirmed counts: ' + json.dumps(bundle['counts'])
        prompt += '\nMinimum lineage size: ' + str(spec['config']['min_cells'])
        props['submit_plan'] = ({'proposal_json': {'type':'string'}}, 'Submit the lineage plan: '+_PLAN_SCHEMA_DOC, False)
        completion = 'submit_plan'
    else:
        prompt = Path(__file__).with_name('prompts').joinpath('zoomin-annotation.md').read_text()
        own, other = lineage_labels(bundle)
        prompt += '\nLineage labels: '+json.dumps(own)+'\nOther permitted labels: '+json.dumps(other)
        prompt += '\nEvidence version: '+evidence['sha256']
        props.update({
            'subcluster': ({'target':{'type':'string','enum':['type','quality']},'cluster':{'type':'string'},'resolution':{'type':'number','exclusiveMinimum':0},'reason':{'type':'string'}}, 'Refine the selected partition only when evidence is inadequate; compute matching DEG and return a new evidence version. Quality refinement preserves accepted types.', False),
            'annotation_status': ({'offset':{'type':'integer','minimum':0}}, 'Read up to 10 accepted type/quality entries and exact QC intersections per page; follow next_offset.', False),
            'deg_lookup': ({'key':{'type':'string'},'cluster':{'type':'string'},'gene':{'type':'string'},'view':{'type':'string','enum':['global','local','both']},'top_n':{'type':'integer','minimum':1,'maximum':200}}, DEG_TOOL_DOC, False),
            'deg_sql': ({'query':{'type':'string'}}, DEG_SQL_DOC, False),
            'check_genes': ({'key':{'type':'string'},'cluster':{'type':'string'},'genes':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':80}}, 'Read expression by the explicitly selected clustering.', False),
            'check_qc_scores': ({}, 'Read QC for resolution 2.0.', False),
            'submit_types': ({'proposal_json':{'type':'string'}}, 'Save {"cluster_key":"msp_leiden_r1.0","clusters":[entries]}. Use action=keep for identity; quality controls removals/reassignments. Each entry: '+_CLUSTER_SCHEMA_DOC, False),
            'submit_quality': ({'proposal_json':{'type':'string'}}, 'Save {"cluster_key":"msp_leiden_r2.0","clusters":[{"cluster_id":"QC id","decisions":[{"type_clusters":["type ids"],"action":"keep|remove|reassign","confidence":"high|medium|low","evidence":"specific evidence","rationale":"reason"}]}]}. Each QC group must cover its present 1.0 intersections exactly once. remove needs remove_reason (doublet|low-quality|ambient|stress|dissociation|dying|batch|other). reassign needs reassign_to and fine_label. After a budget warning add removal_review explaining evidence and scope.', False),
            'finalize_annotation': ({}, 'Finish only after accepted type and quality coverage. Decisions are applied by the host.', False)})
        completion = 'finalize_annotation'
    prompt += '\nTissue/species and integration context: '+json.dumps(spec['config'])
    prompt += '\nUse the registered tools; no local execution or direct file editing is available.'
    session = 'zoom-'+digest([spec['run_id'], kind, evidence])[:24]
    root = Path(spec['output_root'])/session
    root.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    state = immutable(root.with_suffix('.state.json'), dict(evidence=evidence, kind=kind, read=[], lookups=[], qc=False, types=None, quality=None, types_complete=False))
    tools = []
    for name, (fields, description, multimodal) in props.items():
        tools.append(dict(name=name, description=description,
            read_only=name in {'read_evidence','list_evidence','deg_lookup','deg_sql','check_genes','check_qc_scores','type_context'},
            parameters={'type':'object','properties':fields,'required':list(fields),'additionalProperties':False},
            args=['-m','ecarsi.zoomin_v2','tool',name,'{state}','{arguments}'], **spec['compute_budget' if name=='subcluster' else 'tool_budget'],
            inputs=[reference(Path(__file__).with_name(n)) for n in ('zoomin_v2.py','crosssample_v2.py','persample_v2.py')], outputs=['result.json'], result_file='result.json', multimodal=multimodal))
    return dict(session_id=session, dataset_id=spec['dataset_id'], prompt=prompt, tools=tools,
        max_turns=100, pool_root=spec['pool_root'], bridge_root=spec['bridge_root'], output_root=str(root),
        completion_tool=completion, tool_state=state,
        trace=dict(workflow_id='zoom-in/'+spec['run_id'],dataset_id=spec['dataset_id'],unit_id='zoom-in.'+kind,depends_on=[parent]))


def refine_evidence(state, args, destination):
    from msp.inspect import _subcluster_once
    from msp.evidence import load_removal_mask
    from msp.integrate.deg import prepare_deg, save_deg_input
    from msp.integrate.qc import _qc_outputs
    from msp.plots import save_single_umap, slug
    from zmip.scheduled import TYPE_KEY, QUALITY_KEY, partitions
    from zmip.annotate import components
    import math
    if type(args['resolution']) not in (int,float) or not math.isfinite(args['resolution']) or args['resolution'] <= 0:
        raise ValueError('Resolution must be finite and positive')
    bundle = verified(state['evidence']);data = data_from(bundle)
    cfg = verified(bundle['planning'])['spec']['config']
    if bundle['version'] >= cfg['max_refinements']:
        raise ValueError('Refinement limit reached; use available evidence and explicit uncertainty')
    if not args['reason'].strip() or not state['lookups']:
        raise ValueError('Query existing evidence and explain why refinement is necessary')
    key = TYPE_KEY if args['target']=='type' else QUALITY_KEY
    if args['cluster'] not in set(data.obs[key].astype(str)):
        raise ValueError('Refinement must name a cluster of the selected partition')
    mask = load_removal_mask(artifact(bundle,'preannotation_removal.csv').parent,data)
    count, message = _subcluster_once(data,key,args['cluster'],args['resolution'],'_zoom_refined',mask,compute_markers=False)
    if not count:
        return message
    previous = {str(e['cluster_id']):e for e in (state['types'] or {}).get('clusters',[])}
    affected = {args['cluster']} if args['target']=='type' else set()
    if args['target']=='type' and args['cluster'] in previous:
        affected.update(components(previous)[args['cluster']])
    data.obs[key] = data.obs.pop('_zoom_refined')
    kept_types = {c:e for c,e in previous.items() if c not in affected}
    scope = sorted(set(data.obs[TYPE_KEY].astype(str))-set(kept_types))
    output = destination/'refined';output.mkdir()
    keys = [TYPE_KEY,QUALITY_KEY];eligible=data[~mask]
    labels={k:(eligible.obs[k].cat.codes.to_numpy(),list(eligible.obs[k].cat.categories)) for k in keys}
    values,plan=prepare_deg(eligible.X,list(data.var_names),labels,dict(data.uns.get('log1p',{})),eligible.obsm['X_pca_harmony'],keys)
    values.obs_names=eligible.obs_names.copy();save_deg_input(values,output/'deg_input')
    save(output/'deg_plan.json',{**plan,'keys':keys,'top_n_de':50})
    figures=output/'figures';figures.mkdir()
    _qc_outputs(data,data.uns['msp']['batch_col'],'standissect_product',str(output),str(figures),keys,[1.,2.])
    from zmip.foreign import score_foreign
    shared=verified(bundle['shared'])
    score_foreign(data,shared['markers'],bundle['lineage']['name'],keys,str(output),str(figures))
    for k in keys:save_single_umap(data,k,str(figures/('umap_'+slug(k)+'.png')),repel=True)
    partitions(data.obs).to_csv(output/'type_quality_intersections.csv')
    data.obs[keys].rename_axis('cell').to_csv(output/'cell_partitions.csv.gz')
    data.write_h5ad(output/'integrated.h5ad')
    tasks=[dict(plan_index=i,cluster=c) for i,item in enumerate(plan['plan']) for c in [None,*[c for c in item['valid'] if item['top3'].get(c)]]]
    metadata={k:v for k,v in bundle.items() if k not in {'files','prepared','comparisons','tasks','version'}}
    prepared=publish_bundle(output,'prepared.json',state['evidence'],**metadata,version=bundle['version']+1,tasks=tasks)
    comparisons=[]
    for i in range(len(tasks)):
        folder=destination/('deg-'+str(i));folder.mkdir();deg(prepared,i,folder);comparisons.append(reference(folder/'result.json'))
    assembled=destination/'evidence';assembled.mkdir();assemble(prepared,comparisons,assembled)
    state['evidence']=reference(assembled/'evidence.json')
    state['types']={'cluster_key':TYPE_KEY,'clusters':list(kept_types.values())}
    state['type_scope']=scope;state['types_complete']=not scope
    state['quality']=None;state['lookups']=[];state['read']=[];state['qc']=False
    state.pop('budget_warning',None)
    return {'message':message,'version':bundle['version']+1,'type_scope':scope,'evidence':state['evidence']}


def tool(name, state_path, args_path, destination):
    from zmip.scheduled import TYPE_KEY, QUALITY_KEY, partitions, validate_types, validate_quality, apply_decisions
    from msp.evidence import DegTables, gene_table, qc_table
    state, args = read(state_path), read(args_path)
    bundle = verified(state['evidence'])
    response = {}
    try:
        if name == 'list_evidence':
            paths = [n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]
            offset = args['offset'];response.update(content=paths[offset:offset+30],next_offset=offset+30 if offset+30<len(paths) else None)
        elif name == 'read_evidence':
            path = artifact(bundle, args['path'])
            if path.suffix == '.png':
                if path.stat().st_size > 8*2**20:
                    raise ValueError('Figure exceeds image budget')
                response.update(content=args['path'], images=['data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()])
            elif path.suffix in {'.csv','.json','.md','.txt'}:
                with path.open() as stream:
                    stream.seek(args['offset']);response['content']=stream.read(16000)
                    offset=stream.tell();response['next_offset']=offset if stream.read(1) else None
            else:
                raise ValueError('Use the registered matrix/database tools')
            state['read'] = sorted(set(state['read']) | {args['path']})
        elif name == 'submit_plan':
            import pandas as pd
            from zmip.plan import validate_plan
            if not any(p.endswith('.png') for p in state['read']):
                raise ValueError('Read the lineage UMAP before planning')
            def frame(name):
                return pd.read_csv(artifact(bundle,name),index_col=0) if name in bundle['files'] else None
            counts = frame('lineage_counts.csv')
            problems, plan = validate_plan(json.loads(args['proposal_json']),list(counts.index),counts,
                bundle['spec']['config']['min_cells'],frame('lineage_islands.csv'),frame('lineage_knn.csv'))
            if problems:
                raise ValueError('; '.join(problems))
            response.update(accepted=True,evidence=state['evidence'],proposal=plan)
        elif name in {'deg_lookup','deg_sql'}:
            if name == 'deg_lookup' and args['key'] not in {TYPE_KEY,QUALITY_KEY}:
                raise ValueError('Use this lineage version and its explicit 1.0 or 2.0 key')
            with DegTables(database=artifact(bundle,'deg.sqlite'),base_key=TYPE_KEY) as tables:
                response['content'] = tables.lookup(**args) if name=='deg_lookup' else tables.sql(**args)
            state['lookups'].append(args)
        elif name == 'check_genes':
            if args['key'] not in {TYPE_KEY,QUALITY_KEY}:
                raise ValueError('Unknown clustering key')
            response['content'] = gene_table(data_from(bundle),args['genes'],args['key'],[args['cluster']] if args['cluster'] else None)
        elif name == 'check_qc_scores':
            data = data_from(bundle)
            response['content'] = qc_table(data,QUALITY_KEY,data.uns['msp']['batch_col']);state['qc']=True
        elif name == 'subcluster':
            response['content'] = refine_evidence(state,args,destination)
        elif name == 'annotation_status':
            data = data_from(bundle)
            offset=args.get('offset',0);table=partitions(data.obs)
            types=(state['types'] or {}).get('clusters',[]);quality=(state['quality'] or {}).get('clusters',[])
            length=max(len(types),len(quality),len(table))
            response.update(types=types[offset:offset+10],quality=quality[offset:offset+10],
                type_scope=state.get('type_scope',sorted(data.obs[TYPE_KEY].astype(str).unique())),
                intersections={str(q):{str(t):int(n) for t,n in row.items() if n} for q,row in table.iloc[offset:offset+10].iterrows()},
                next_offset=offset+10 if offset+10<length else None,version=bundle['version'])
        elif name == 'submit_types':
            data = data_from(bundle);own, _ = lineage_labels(bundle)
            missing = [name for name, done in [('deg_lookup or deg_sql', bool(state['lookups'])),
                ('read_evidence on a lineage PNG figure', any(p.endswith('.png') for p in state['read']))] if not done]
            if missing:
                raise ValueError('Complete required checks: ' + ', '.join(missing))
            proposal = json.loads(args['proposal_json'])
            if not isinstance(proposal,dict):raise ValueError('Type proposal must be an object')
            submitted=proposal.get('clusters',[])
            scope=([str(e['cluster_id']) for e in submitted] if state.get('types_complete')
                   else state.get('type_scope',sorted(data.obs[TYPE_KEY].astype(str).unique())))
            if len(submitted)!=len(scope) or {str(e['cluster_id']) for e in submitted}!=set(scope):
                raise ValueError('Submit exactly the pending type_scope clusters; preserved types remain valid')
            preserved={str(e['cluster_id']):e for e in (state['types'] or {}).get('clusters',[]) if str(e['cluster_id']) not in scope}
            proposal['clusters']=list(preserved.values())+submitted
            validate_types(proposal,data.obs,own)
            state['types'] = proposal;state['types_complete']=True;state['type_scope']=[]
            state['quality'] = None;state.pop('budget_warning',None)
            response['content'] = 'Type coverage accepted; continue quality review at resolution 2.0.'
        elif name == 'submit_quality':
            missing = [name for name, done in [('submit_types', state.get('types_complete')),
                ('check_qc_scores', state['qc']), ('deg_lookup or deg_sql', bool(state['lookups'])),
                ('read_evidence on a lineage PNG figure', any(p.endswith('.png') for p in state['read']))] if not done]
            if missing:
                raise ValueError('Complete required checks: ' + ', '.join(missing))
            data = data_from(bundle);own, other = lineage_labels(bundle)
            proposal = json.loads(args['proposal_json'])
            proposal['clusters'] = validate_quality(proposal,data.obs,other)
            pre = numerical_reasons(bundle,data)
            _, removed, _, _ = apply_decisions(data.obs,state['types'],proposal,own,other,bundle['lineage']['name'],pre)
            from zmip.annotate import REMOVE_BUDGET
            fraction = len(set(removed.cell)-set(pre))/len(data)
            if fraction > REMOVE_BUDGET:
                if not state.get('budget_warning'):
                    state['budget_warning'] = True
                    raise ValueError(f'Removal review required: {fraction:.1%} beyond numerical exclusions. Review evidence and exact scope; confirmed dissociation/dying subclusters still default to removal. Resubmit with a specific removal_review explanation.')
                if not isinstance(proposal.get('removal_review'),str) or not proposal['removal_review'].strip():
                    raise ValueError('Explain the reviewed removal evidence and scope in removal_review')
            state['quality'] = proposal;state['removal_fraction'] = fraction
            response['content'] = 'Quality coverage accepted; finalize or revise the proposals.'
        elif name == 'finalize_annotation':
            if not state.get('types_complete') or state['quality'] is None:
                raise ValueError('Both type and quality coverage must be accepted')
            response.update(accepted=True,evidence=state['evidence'],types=state['types'],quality=state['quality'],removal_fraction=state['removal_fraction'])
        else:
            raise ValueError('Unknown zoom-in tool')
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        response = {'is_error':True,'content':str(exc)[:8000]}
    response['state'] = immutable(destination/'state.json',state)
    save(destination/'result.json',response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action');parser.add_argument('args',nargs='+');args=parser.parse_args()
    dest = Path.cwd()
    if args.action == 'tool':
        tool(*args.args,dest);return
    packet = read(args.args[0]);refs=packet['refs'];spec=packet['spec']
    action = args.action
    if action == 'prepare':prepare(spec,dest)
    elif action == 'markers':markers(*refs,dest)
    elif action == 'subset':subset(*refs,packet['index'],dest)
    elif action == 'compute':compute(*refs,dest)
    elif action == 'deg':deg(refs[0],packet['index'],dest)
    elif action == 'assemble':assemble(refs[0],refs[1:],dest)
    elif action == 'agent':save(dest/'agent.json',agent_spec(spec,refs[0],packet['kind'],packet['request_id']))
    elif action == 'apply':apply_lineage(*refs,dest)
    elif action == 'merge':merge(refs[0],refs[1],refs[2:],dest)
    else:raise ValueError('Unknown zoom-in operation')


if __name__ == '__main__':
    main()
