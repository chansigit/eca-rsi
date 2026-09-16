import asyncio
from types import SimpleNamespace
import pytest
from temporalio.exceptions import ApplicationError

from ecarsi.zoomin_workflow import ZoominWorkflow, zoomin_step
from ecarsi.warm_pool.state import save, read


def test_gpu_grant_is_selected_for_lineage_compute(tmp_path):
    pool=tmp_path/'pool';pool.mkdir(mode=0o700);(pool/'requests').mkdir()
    save(pool/'config.json',{'runtime':{}})
    save(tmp_path/'subset.json',{'lineage':{'n_cells':900}})
    save(tmp_path/'markers.json',{})
    from ecarsi.agent_session import reference
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
    import ecarsi.zoomin_workflow as module
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
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='zoom-test'))
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        if fail_first:
            with pytest.raises(ApplicationError,match='completed lineages retained'):
                await ZoominWorkflow().run(dict(max_in_flight_lineages=1,max_in_flight_deg=2))
            assert second_compute.is_set() and events.count('apply')==1 and 'merge' not in events
        else:
            result=await ZoominWorkflow().run(dict(max_in_flight_lineages=1,max_in_flight_deg=2))
            assert result=='publication' and events.count('compute')==2 and events[-1]=='merge'
    asyncio.run(scenario())
