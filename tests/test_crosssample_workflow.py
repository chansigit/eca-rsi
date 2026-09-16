"""Bounded numerical work must finish before type -> quality -> publication."""
import asyncio
from types import SimpleNamespace

import pytest

from ecarsi.crosssample_workflow import CrosssampleWorkflow, crosssample_step, validate_spec
from ecarsi.agent_session import reference
from ecarsi.warm_pool.state import save, read, validate_trace


def test_gpu_selection_and_large_fanin(tmp_path):
    pool=tmp_path/'pool';pool.mkdir(mode=0o700);(pool/'requests').mkdir()
    save(pool/'config.json',{'runtime':{}})
    save(tmp_path/'inspected.json',{'n_input':10000})
    save(tmp_path/'inclusion.json',{})
    budget=dict(cpus=1,memory_mb=1024,timeout_seconds=100)
    spec=dict(run_id='cross-test',dataset_id='D',output_root=str(tmp_path/'out'),pool_root=str(pool),
              compute_budget=budget,config=dict(compute_backend='auto',gpu_min_cells=5000,gpu_memory_mb=4096))
    task=crosssample_step('compute',[spec,{'paths':[str(tmp_path/'inspected.json'),str(tmp_path/'inclusion.json')]},['included']])
    request=read(pool/'requests'/task['id']/'request.json')['spec']
    assert request['gpu']==dict(mode='preferred',memory_mb=4096)
    later=crosssample_step('compute-round',[spec,{'paths':[str(tmp_path/'inspected.json')]},['prior-zoom']])
    repeated=read(pool/'requests'/later['id']/'request.json')['spec']
    assert repeated['gpu']==request['gpu'] and repeated['trace']['depends_on']==['prior-zoom']
    trace={**request['trace'],'depends_on':['deg-'+str(i) for i in range(100)]}
    assert validate_trace(trace)==trace
    with pytest.raises(ValueError):validate_trace({**trace,'depends_on':['d'+str(i) for i in range(4097)]})


@pytest.mark.parametrize('change_limit', [False, True])
def test_workflow_fanout_and_annotation_order(monkeypatch, change_limit):
    import ecarsi.crosssample_workflow as module
    async def scenario():
        active=peak=0;events=[];requests={}
        async def call(fn,action,args):
            if action=='read':
                path=args[0]
                if path=='inspect':return {'samples':[{},{}]}
                if path=='compute':return {'tasks':list(range(8))}
                if path.startswith('agent'):return {'session_id':path}
                if path=='decision-quality':return {}
                raise AssertionError(path)
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':events.append('publish');return 'publication'
            _,payload,parents=args
            name=action+('-'+payload['phase'] if action=='agent' else '-'+str(payload['index']) if action=='deg' else '')
            requests[name]=(payload,parents);events.append(name)
            return {'id':name,'output':name}
        async def await_pool(spec,request):
            nonlocal active,peak
            if request['id'].startswith('deg-'):
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.001)
                if change_limit and request['id']=='deg-0':
                    assert workflow.set_deg_limit(5) == 5
                active-=1
            return request['id']
        async def child(fn,session,**kwargs):return 'decision-'+session['session_id'].split('-')[1]
        monkeypatch.setattr(module,'call',call);monkeypatch.setattr(module,'await_pool',await_pool)
        monkeypatch.setattr(module.workflow,'execute_child_workflow',child)
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='cross-sample/test'))
        monkeypatch.setattr(module.workflow,'wait',asyncio.wait)
        workflow=CrosssampleWorkflow()
        assert await workflow.run({'max_in_flight_deg':3,'max_refinements':0})=='publication'
        assert peak==(5 if change_limit else 3) and events.index('assemble')>events.index('deg-7')
        assert events.index('agent-type')<events.index('agent-quality')<events.index('finalize')
        assert len(requests['assemble'][1])==9
        assert requests['finalize'][0]['paths']==['assemble','decision-type','decision-quality']
        assert workflow.stage()=='complete'
    asyncio.run(scenario())


def test_confirmed_worker_interruption_recovers_with_a_finite_attempt_budget(tmp_path):
    from ecarsi.work_coordinator import check_pool
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700);(tmp_path/'requests').mkdir();save(tmp_path/'config.json',{'runtime':{}})
    submit(tmp_path,dict(request_id='r',operation_id='compute',args=['-c','pass'],cpus=1,memory_mb=64,timeout_seconds=30,outputs=['result.json']))
    folder=tmp_path/'requests/r'
    for retry_number in range(3):
        request=read(folder/'request.json')
        assert request.get('retry_count',0)==retry_number
        save(folder/request['attempt_id']/'receipt.json',dict(state='failed',retryable=True,finished_at=1,
            attempt_id=request['attempt_id'],request_digest=request['digest'],runtime_digest=request['runtime_digest']))
        assert check_pool(str(tmp_path),'r','result.json')['state']==('waiting' if retry_number<2 else 'failed')


def test_uncertain_observation_waits_for_same_attempt_receipt(tmp_path):
    from ecarsi.work_coordinator import check_pool, check_bridge
    from ecarsi.warm_pool.state import submit
    from ecarsi.agent_session import reference
    tmp_path.chmod(0o700); (tmp_path/'requests').mkdir(); save(tmp_path/'config.json', {'runtime':{}})
    submit(tmp_path, dict(request_id='r', operation_id='compute', args=['-c','pass'], cpus=1,
        memory_mb=64, timeout_seconds=30, outputs=['result.json']))
    folder=tmp_path/'requests/r'; request=read(folder/'request.json'); attempt=folder/request['attempt_id']
    save(folder/'backend.json', dict(state='unknown_external_result'))
    assert check_pool(str(tmp_path),'r','result.json') == dict(state='waiting', detail='unknown_external_result')
    save(attempt/'accepted.json', dict(started_at=1))
    assert check_pool(str(tmp_path),'r','result.json')['state']=='waiting'
    output=attempt/'outputs/result.json'; save(output, dict(value=7))
    save(attempt/'receipt.json', dict(state='succeeded', outputs=[reference(output)]))
    assert check_pool(str(tmp_path),'r','result.json')['path']==str(output)
    assert read(folder/'request.json')==request
    # An uncertain legacy Bridge execution can also publish a late reply.
    save(folder/'state.json', dict(state='unknown_external_result'))
    assert check_bridge(str(tmp_path),'r')['state']=='waiting'
    save(folder/'result.json', dict(state='reply_saved'))
    assert check_bridge(str(tmp_path),'r')['state']=='ready'
