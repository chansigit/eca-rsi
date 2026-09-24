"""Durable lineage scheduling; model waits hold no numerical admission slots."""
import asyncio
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

from .persample import HISTORY_LIMIT, await_pool, call, handoff


def validate_spec(spec, *, resume=False):
    from ..warm_pool.state import verified
    from ..warm_pool.state import identifier, pool_root
    from ..agent import root_path
    budgets = {'prepare_budget','subset_budget','compute_budget','deg_budget','tool_budget','merge_budget'}
    required = {'run_id','dataset_id','input','output_root','pool_root','bridge_root','config',
                'max_in_flight_lineages','max_in_flight_deg'} | budgets
    if not isinstance(spec, dict) or set(spec) - {'depends_on'} != required:
        raise ValueError('Zoom-in needs explicit input, services and operation budgets')
    identifier(spec['run_id'])
    if 'depends_on' in spec:
        from ..warm_pool.state import validate_trace
        validate_trace(dict(workflow_id='zoom-in/' + spec['run_id'], dataset_id=spec['dataset_id'],
                            unit_id='zoom-in.prepare', depends_on=spec['depends_on']))
    if len(spec['run_id']) > 60 or not isinstance(spec['dataset_id'],str) or not spec['dataset_id'].strip():
        raise ValueError('Use a run ID up to 60 characters and a dataset label')
    if not Path(spec['output_root']).is_absolute() or (Path(spec['output_root']).exists() and not resume):
        raise ValueError('Use a fresh absolute output directory')
    source = verified(spec['input'])
    if source.get('state') != 'complete' or 'annotated.h5ad' not in source.get('files',{}):
        raise ValueError('Zoom-in requires an accepted cross-sample publication')
    prior = verified(verified(source['evidence'])['inspected'])['spec']['config']
    if any(spec['config'].get(key) != prior[key] for key in ('batch_col','species')):
        raise ValueError('Species and sample key must match accepted cross-sample input')
    pool_root(spec['pool_root']);root_path(spec['bridge_root'])
    for key in budgets:
        budget=spec[key]
        if not isinstance(budget,dict) or set(budget)!={'cpus','memory_mb','timeout_seconds'} or any(type(v) is not int or v<1 for v in budget.values()):
            raise ValueError('Budgets require positive CPU, MiB and timeout integers')
    for key in ('max_in_flight_lineages','max_in_flight_deg'):
        if type(spec[key]) is not int or spec[key]<1:
            raise ValueError('Concurrency limits must be positive integers')
    cfg=spec['config']
    required={'batch_col','species','tissue','min_cells','n_top_genes','n_pcs','n_neighbors','compute_backend','gpu_min_cells','gpu_memory_mb','max_refinements'}
    if not isinstance(cfg,dict) or set(cfg)!=required:
        raise ValueError('Explicit lineage, integration and backend settings required')
    if type(cfg['max_refinements']) is not int or cfg['max_refinements'] < 0:
        raise ValueError('max_refinements must be a nonnegative integer')
    for key in ('min_cells','n_top_genes','n_pcs','n_neighbors','gpu_min_cells','gpu_memory_mb'):
        if type(cfg[key]) is not int or cfg[key]<1:
            raise ValueError('Numerical settings must be positive integers')
    if cfg['compute_backend'] not in {'cpu','rapids','auto'}:
        raise ValueError('Backend must be cpu, rapids or auto')
    for key in ('species','tissue','batch_col'):
        if not isinstance(cfg[key],str) or not cfg[key].strip():
            raise ValueError('Species, tissue and sample key must be nonempty')
    return spec


@activity.defn
def zoomin_step(action,args):
    from ..warm_pool.state import immutable, reference, verified
    from ..warm_pool.state import digest, submit
    if action=='read':return handoff(args[0])
    if action=='session':return verified(reference(args[0]))
    if action=='accepted':
        from .persample import sample_step
        return sample_step('accepted_annotation',args)
    if action=='publish':
        spec,path=args;bundle=verified(reference(path))
        if bundle['state']!='complete' or bundle['input']!=spec['input'] or bundle['n_input']!=bundle['n_survived']+bundle['n_removed']:
            raise ValueError('Zoom-in publication does not conserve its accepted input')
        path=Path(spec['output_root'])/'publication.json'
        immutable(path,{**bundle,'result':reference(args[1])})
        from .artifacts import copy_light
        copy_light(bundle.get('files'), path.parent)
        return str(path)
    spec,payload,parents=args
    if action == 'prepare':
        parents = parents or spec.get('depends_on', [])
    root=Path(spec['output_root']);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    immutable(root/'spec.json',spec)
    refs=[reference(path) for path in payload['paths']]
    request_id=spec['run_id']+'.'+action+'-'+digest(payload)[:16]
    cfg=spec['config'];gpu={}
    budget,output={
        'prepare':('prepare_budget','prepared.json'),'markers':('compute_budget','markers.json'),
        'subset':('subset_budget','subset.json'),'compute':('compute_budget','prepared.json'),
        'deg':('deg_budget','result.json'),'assemble':('tool_budget','evidence.json'),
        'agent':('prepare_budget','agent.json'),'apply':('merge_budget','final.json'),
        'merge':('merge_budget','final.json')}[action]
    if action=='compute':
        cells=verified(refs[0])['lineage']['n_cells']
        if cfg['compute_backend']=='rapids' or cfg['compute_backend']=='auto' and cells>=cfg['gpu_min_cells']:
            gpu={'gpu':{'mode':'required' if cfg['compute_backend']=='rapids' else 'preferred','memory_mb':cfg['gpu_memory_mb']}}
    packet=immutable(root/(request_id+'.json'),dict(spec=spec,refs=refs,request_id=request_id,**{k:v for k,v in payload.items() if k!='paths'}))
    unit='zoom-in.'+(payload['kind']+'.prepare' if action=='agent' else action)
    from .. import stages
    programs=(stages.program('zoomin'),stages.program('crosssample'),stages.program('persample'),stages.program('contract'))
    module='ecarsi.stages.zoomin'
    request=dict(request_id=request_id,operation_id=unit,
        args=['-m',module,action,packet['path']],**spec[budget],**gpu,
        inputs=[packet,*[reference(path) for path in programs],spec['input'],*refs],outputs=[output],
        trace=dict(workflow_id='zoom-in/'+spec['run_id'],dataset_id=spec['dataset_id'],unit_id=unit,depends_on=parents))
    if action=='deg':
        from ..warm_pool.budget import from_deg_buffers
        request=from_deg_buffers(request,refs[0],root/(request_id+'.resources.json'),spec['pool_root'])
    elif action in {'prepare','markers','subset','merge'}:
        # These load the whole cross-sample matrix; size them from it, not a constant.
        from ..warm_pool.budget import from_artifact
        request=from_artifact(request,verified(spec['input'])['files']['annotated.h5ad'],
                              root/(request_id+'.resources.json'),spec['pool_root'])
    submit(spec['pool_root'],request)
    return {'id':request_id,'output':output}


@workflow.defn
class ZoominWorkflow:
    @workflow.update
    def set_deg_limit(self, limit: int) -> int:
        self.validate_deg_limit(limit)
        self._deg_limit = limit
        return limit

    @set_deg_limit.validator
    def validate_deg_limit(self, limit: int):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError('DEG submission window must be an integer from 1 to 256')

    @workflow.query
    def deg_limit(self):
        return getattr(self, '_deg_limit', None)

    @workflow.query
    def stage(self):
        return getattr(self,'_stage','created')

    @workflow.run
    async def run(self,spec,progress=None):
        from .coordinator import run_agent
        from .persample import SKIPPED_CELL_LIMIT
        progress=progress or {}  # from continue_as_new: finished lineages and the DEG window
        self._deg_limit=progress.get('deg_limit') or getattr(self,'_deg_limit',spec['max_in_flight_deg'])
        async def run(action,paths,parents,**details):
            request=await call(zoomin_step,action,[spec,dict(paths=paths,**details),parents])
            return await await_pool(spec,request),request['id']
        async def judge(kind,evidence,parent):
            path,_=await run('agent',[evidence],[parent],kind=kind)
            policy='session' if workflow.patched('agent-session-policy-v1') else 'read'
            session=await call(zoomin_step,policy,[path])
            result=await run_agent(session,workflow.info().workflow_id+'/'+session['session_id'],call)
            accepted=await call(zoomin_step,'accepted',[result])
            return accepted['path'],accepted['parent']
        self._stage='preparing lineage evidence'
        prepared,parent=await run('prepare',[],[])
        self._stage='planning lineages'
        plan,plan_parent=await judge('plan',prepared,parent)
        decision=await call(zoomin_step,'read',[plan])
        lines=decision['proposal']['lineages']
        chosen=[i for i,line in enumerate(lines) if line['zoom']]
        results,skipped=[],[]
        if chosen:
            self._stage='computing and annotating lineages'
            shared=asyncio.create_task(run('markers',[prepared,plan],[plan_parent]))
            compute_slots=asyncio.Semaphore(spec['max_in_flight_lineages'])
            async def lineage(index):
                async with compute_slots:
                    part,part_parent=await run('subset',[prepared,plan],[plan_parent],index=index)
                    markers,marker_parent=await shared
                    computed,compute_parent=await run('compute',[part,markers],[part_parent,marker_parent])
                    bundle=await call(zoomin_step,'read',[computed])
                    pending,comparisons,next_index={},{},0
                    while next_index<len(bundle['tasks']) or pending:
                        while next_index<len(bundle['tasks']) and len(pending)<self._deg_limit:
                            task=asyncio.create_task(run('deg',[computed],[compute_parent],index=next_index))
                            pending[task]=next_index;next_index+=1
                        done,_=await workflow.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                        for task in sorted(done,key=lambda t:pending[t]):
                            comparisons[pending.pop(task)]=await task
                    ordered=[comparisons[i] for i in sorted(comparisons)]
                    evidence,evidence_parent=await run('assemble',[computed]+[v[0] for v in ordered],[compute_parent]+[v[1] for v in ordered])
                # Free numerical admission before the model session, across every lineage.
                try:
                    accepted,accepted_parent=await judge('lineage',evidence,evidence_parent)
                except Exception as exc:
                    # Two sessions died: this lineage keeps its cross-sample labels for the round.
                    return dict(skipped=dict(index=index,name=lines[index]['name'],n_cells=lines[index]['n_cells'],error=str(exc)))
                decision=await call(zoomin_step,'read',[accepted])
                return await run('apply',[decision['evidence']['path'],accepted],[accepted_parent])
            async def windowed():
                """A bounded window of lineages in flight (sessions included); past HISTORY_LIMIT events no
                new one starts and, once the window drains, the run continues as new carrying the finished
                ones, so no round's history outgrows what a coordinator can replay or cache."""
                done={int(k):v for k,v in progress.get('lineages',{}).items()}
                remaining=[i for i in chosen if i not in done]
                window=2*spec['max_in_flight_lineages']
                running,failures={},[]
                while remaining or running:
                    full=workflow.info().get_current_history_length()>HISTORY_LIMIT
                    while remaining and len(running)<window and not full:
                        index=remaining.pop(0)
                        running[asyncio.create_task(lineage(index))]=index
                    if not running:
                        await shared
                        workflow.continue_as_new(args=[spec,dict(lineages={str(k):v for k,v in done.items()},deg_limit=self._deg_limit)])
                    finished,_=await workflow.wait(list(running),return_when=asyncio.FIRST_COMPLETED)
                    for task in finished:
                        index=running.pop(task)
                        try:
                            result=task.result()
                            done[index]=result if isinstance(result,dict) else list(result)
                        except Exception as exc:  # the round fails once every lineage has settled, as before
                            failures.append(exc)
                if failures:
                    raise ApplicationError(f'{len(failures)} lineages failed; completed lineages retained: {failures[0]}',
                                           non_retryable=True)
                return [done[i] for i in chosen]
            if workflow.patched('zoomin-continue-as-new-v1'):
                results=await windowed()
            elif workflow.patched('zoomin-preserve-independent-lineages-v1'):
                results=await asyncio.gather(*[lineage(i) for i in chosen],return_exceptions=True)
                failures=[r for r in results if isinstance(r,BaseException)]
                if failures:
                    raise ApplicationError(f'{len(failures)} lineages failed; completed lineages retained: {failures[0]}',
                                           non_retryable=True)
            else:
                results=await asyncio.gather(*[lineage(i) for i in chosen])
            skipped=[r['skipped'] for r in results if isinstance(r,dict)]
            results=[r for r in results if not isinstance(r,dict)]
            lost=sum(s['n_cells'] for s in skipped)
            if skipped and lost>SKIPPED_CELL_LIMIT*sum(line['n_cells'] for line in lines):
                raise ApplicationError(f'{len(skipped)} lineages skipped after their agent sessions died hold {lost} cells, '
                                       f'over {SKIPPED_CELL_LIMIT:.0%} of the round input',non_retryable=True)
        self._stage='merging lineage results'
        result,_=await run('merge',[prepared,plan]+[r[0] for r in results],[plan_parent]+[r[1] for r in results],skipped=skipped)
        publication=await call(zoomin_step,'publish',[spec,result])
        self._stage='complete'
        return publication
