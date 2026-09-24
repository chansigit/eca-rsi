import asyncio
from types import SimpleNamespace
import pytest
from temporalio.exceptions import ApplicationError

from ecarsi.control.zoomin import ZoominWorkflow, zoomin_step
from ecarsi.warm_pool.state import save, read


def test_gpu_grant_is_selected_for_lineage_compute(tmp_path):
    pool=tmp_path/'pool';pool.mkdir(mode=0o700);(pool/'requests').mkdir()
    save(pool/'config.json',{'runtime':{}})
    save(tmp_path/'subset.json',{'lineage':{'n_cells':900}})
    save(tmp_path/'markers.json',{})
    from ecarsi.agent.session import reference
    budget=dict(cpus=2,memory_mb=4096,timeout_seconds=600)
    spec=dict(run_id='zoom-test',dataset_id='D',output_root=str(tmp_path/'out'),pool_root=str(pool),
        input=reference(tmp_path/'subset.json'),compute_budget=budget,
        config=dict(compute_backend='auto',gpu_min_cells=500,gpu_memory_mb=4096))
    request=zoomin_step('compute',[spec,dict(paths=[str(tmp_path/'subset.json'),str(tmp_path/'markers.json')]),['part','markers']])
    stored=read(pool/'requests'/request['id']/'request.json')['spec']
    assert stored['gpu']==dict(mode='preferred',memory_mb=4096)
    assert stored['trace']['depends_on']==['part','markers']


@pytest.mark.parametrize('fail_first', [False, True])
def test_model_wait_releases_lineage_compute_admission(monkeypatch, fail_first):
    import ecarsi.control.zoomin as module
    async def scenario():
        second_compute=asyncio.Event();requests={};prepared_count=0;events=[]
        async def call(fn,action,args):
            if action in ('read','session'):
                path=args[0]
                if path=='lineage-decision':return {'evidence':{'path':'evidence'}}
                if path=='plan-decision':return {'proposal':{'lineages':[{'zoom':True},{'zoom':True}]}}
                if path.startswith('compute'):return {'tasks':[]}
                if path.startswith('agent'):return {'session_id':path}
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':return 'publication'
            payload=args[1]
            identifier=action+'-'+str(len(requests));requests[identifier]=(action,payload)
            return {'id':identifier,'output':identifier}
        async def await_pool(spec,request):
            action,payload=requests[request['id']]
            events.append(action)
            if action=='compute':
                nonlocal prepared_count
                prepared_count+=1
                if fail_first and prepared_count==1:raise RuntimeError('one lineage failed')
                if prepared_count==2:second_compute.set()
            return request['id']
        async def child(fn,session,**kw):
            action,payload=requests[session['session_id']]
            if payload['kind']=='plan':return 'plan-decision'
            await asyncio.wait_for(second_compute.wait(),timeout=1)
            return 'lineage-decision'
        monkeypatch.setattr(module,'call',call);monkeypatch.setattr(module,'await_pool',await_pool)
        monkeypatch.setattr(module.workflow,'execute_child_workflow',child)
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='zoom-test',get_current_history_length=lambda:0))
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        if fail_first:
            with pytest.raises(ApplicationError,match='completed lineages retained'):
                await ZoominWorkflow().run(dict(max_in_flight_lineages=1,max_in_flight_deg=2))
            assert second_compute.is_set() and events.count('apply')==1 and 'merge' not in events
        else:
            result=await ZoominWorkflow().run(dict(max_in_flight_lineages=1,max_in_flight_deg=2))
            assert result=='publication' and events.count('compute')==2 and events[-1]=='merge'
    asyncio.run(scenario())


def test_lineage_window_continues_as_new_and_the_continued_run_finishes_the_rest(monkeypatch):
    import ecarsi.control.zoomin as module
    class Continued(BaseException):
        def __init__(self, args): self.carried = args
    async def scenario():
        requests={};events=[];history={'n':0}
        async def call(fn,action,args):
            if action in ('read','session'):
                path=args[0]
                if path=='lineage-decision':return {'evidence':{'path':'evidence'}}
                if path=='plan-decision':return {'proposal':{'lineages':[{'zoom':True,'name':n,'n_cells':1} for n in 'abc']}}
                if path.startswith('compute'):return {'tasks':[]}
                if path.startswith('agent'):return {'session_id':path}
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':return 'publication'
            payload=args[1];identifier=action+'-'+str(len(requests));requests[identifier]=(action,payload)
            if action=='merge':events.append(('merge',len(payload['paths'])))
            return {'id':identifier,'output':identifier}
        async def await_pool(spec,request):
            action,_=requests[request['id']];events.append(action)
            if action=='apply':history['n']+=10000  # a finished lineage pushes the history past the limit
            return request['id']
        async def child(fn,session,**kw):
            action,payload=requests[session['session_id']]
            return 'plan-decision' if payload['kind']=='plan' else 'lineage-decision'
        def continue_as_new(args):raise Continued(args)
        monkeypatch.setattr(module,'call',call);monkeypatch.setattr(module,'await_pool',await_pool)
        monkeypatch.setattr(module.workflow,'execute_child_workflow',child)
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='zoom-test',get_current_history_length=lambda:history['n']))
        monkeypatch.setattr(module.workflow,'wait',asyncio.wait)
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        monkeypatch.setattr(module.workflow,'continue_as_new',continue_as_new)
        spec=dict(max_in_flight_lineages=1,max_in_flight_deg=2)  # a window of two lineages
        with pytest.raises(Continued) as first:
            await ZoominWorkflow().run(spec)
        carried=first.value.carried[1]
        assert set(carried['lineages'])=={'0','1'} and carried['deg_limit']==2  # the window drained; the third never started
        assert events.count('apply')==2 and 'merge' not in [e[0] if isinstance(e,tuple) else e for e in events]
        history['n']=0
        assert await ZoominWorkflow().run(spec,carried)=='publication'
        assert events.count('apply')==3 and ('merge',5) in events  # prepared, plan and all three lineages, in order
    asyncio.run(scenario())
