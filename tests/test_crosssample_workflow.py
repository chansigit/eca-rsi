"""Bounded numerical work must finish before type -> quality -> publication."""
import asyncio
from types import SimpleNamespace

import pytest

from ecarsi.control.crosssample import CrosssampleWorkflow, crosssample_step, validate_spec
from ecarsi.agent.session import reference
from ecarsi.files import save, read
from ecarsi.warm_pool.state import validate_trace


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
    import ecarsi.control.crosssample as module
    async def scenario():
        active=peak=0;events=[];requests={}
        async def call(fn,action,args):
            if action in ('read','session'):
                path=args[0]
                if path=='inspect':return {'samples':[{},{}]}
                if path=='compute':return {'tasks':list(range(8))}
                if path.startswith('agent'):return {'session_id':path}
                if path=='decision-quality':return {}
                raise AssertionError(path)
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':events.append('publish');return 'publication'
            _,payload,parents=args
            name=('deg-'+str(payload['indices'][0]) if action=='deg-batch' else
                  action+('-'+payload['phase'] if action=='agent' else '-'+str(payload['index']) if action=='deg' else ''))
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
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='cross-sample/test',get_current_history_length=lambda:0))
        monkeypatch.setattr(module.workflow,'wait',asyncio.wait)
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        monkeypatch.setattr(module,'DEG_BATCH_SIZE',1)   # one comparison per request, as before batching
        workflow=CrosssampleWorkflow()
        assert await workflow.run({'max_in_flight_deg':3,'max_refinements':0})=='publication'
        assert peak==(5 if change_limit else 3) and events.index('assemble')>events.index('deg-7')
        assert events.index('agent-type')<events.index('agent-quality')<events.index('finalize')
        assert len(requests['assemble'][1])==9
        assert requests['finalize'][0]['paths']==['assemble','decision-type','decision-quality']
        assert workflow.stage()=='complete'
    asyncio.run(scenario())


def test_confirmed_worker_interruption_recovers_with_a_finite_attempt_budget(tmp_path):
    from ecarsi.control.coordinator import check_pool
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700);(tmp_path/'requests').mkdir();save(tmp_path/'config.json',{'runtime':{}})
    submit(tmp_path,dict(request_id='r',operation_id='compute',args=['-c','pass'],cpus=1,memory_mb=64,timeout_seconds=30,outputs=['result.json']))
    folder=tmp_path/'requests/r'
    for retry_number in range(3):
        request=read(folder/'request.json')
        assert request.get('retry_count',0)==retry_number
        save(folder/request['attempt_id']/'receipt.json',dict(outputs=[], state='failed',retryable=True,finished_at=1,
            attempt_id=request['attempt_id'],request_digest=request['digest'],runtime_digest=request['runtime_digest']))
        assert check_pool(str(tmp_path),'r','result.json')['state']==('waiting' if retry_number<2 else 'failed')


def test_a_blocked_pool_request_shows_why_it_waits(tmp_path, monkeypatch):
    # #18: the scheduler's infeasible reason reaches the waiting workflow's stage query.
    from ecarsi.control.coordinator import check_pool
    import ecarsi.control.persample as module
    from ecarsi.warm_pool.backend import observe
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700); (tmp_path/'requests').mkdir(); save(tmp_path/'config.json', {'runtime': {}})
    submit(tmp_path, dict(request_id='r', operation_id='compute', args=['-c', 'pass'], cpus=1,
        memory_mb=64, timeout_seconds=30, outputs=['result.json']))
    folder, why = tmp_path/'requests/r', 'no worker that holds 1 cpus / 64 MB has 60 s left'
    observe(folder, read(folder/'request.json'), dict(state='queued', infeasible=why))
    assert check_pool(str(tmp_path), 'r', 'result.json') == dict(state='waiting', detail='infeasible: ' + why)
    owner, seen = SimpleNamespace(_stage='DEG comparisons'), []
    answers = iter([check_pool(str(tmp_path), 'r', 'result.json'), dict(state='ready', path='/done')])
    async def call(fn, *args):
        return next(answers)
    async def sleep(seconds):
        seen.append(module.stage_with_waits(owner))
    monkeypatch.setattr(module, 'call', call)
    monkeypatch.setattr(module.workflow, 'instance', lambda: owner)
    monkeypatch.setattr(module.workflow, 'sleep', sleep)
    assert asyncio.run(module.await_pool(dict(pool_root=str(tmp_path)), dict(id='r', output='result.json'))) == '/done'
    assert seen == ['DEG comparisons; waiting: infeasible: ' + why]
    assert module.stage_with_waits(owner) == 'DEG comparisons'


def test_a_time_limit_is_doubled_once(tmp_path):
    # #18: doubled twice, a DEG batch asked for a whole node's time and waited as infeasible.
    from ecarsi.control.coordinator import check_pool
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700);(tmp_path/'requests').mkdir();save(tmp_path/'config.json',{'runtime':{}})
    submit(tmp_path,dict(request_id='r',operation_id='deg',args=['-c','pass'],cpus=1,memory_mb=64,timeout_seconds=30,outputs=['result.json']))
    folder=tmp_path/'requests/r'
    for expected in ('waiting','failed'):
        request=read(folder/'request.json')
        save(folder/request['attempt_id']/'receipt.json',dict(outputs=[], state='failed',retryable=False,finished_at=1,
            error='TimeoutError: execution time limit reached',attempt_id=request['attempt_id'],
            request_digest=request['digest'],runtime_digest=request['runtime_digest']))
        assert check_pool(str(tmp_path),'r','result.json')['state']==expected
    assert read(folder/'request.json')['spec']['timeout_seconds']==60


def test_a_changed_output_or_pinned_input_fails_at_once(tmp_path):
    # #22: both raised ValueError, which the poll activity retried 40 times over ~35 min.
    from ecarsi.control.coordinator import check_pool
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700);(tmp_path/'requests').mkdir();save(tmp_path/'config.json',{'runtime':{}})
    pinned=tmp_path/'input.json';save(pinned,{'v':1})
    submit(tmp_path,dict(request_id='r',operation_id='compute',args=['-c','pass'],cpus=1,memory_mb=64,timeout_seconds=30,
        inputs=[reference(pinned)],outputs=['result.json']))
    folder=tmp_path/'requests/r';request=read(folder/'request.json');attempt=folder/request['attempt_id']
    save(attempt/'receipt.json',dict(outputs=[], state='failed',retryable=True,finished_at=1,
        attempt_id=request['attempt_id'],request_digest=request['digest'],runtime_digest=request['runtime_digest']))
    save(pinned,{'v':2})
    result=check_pool(str(tmp_path),'r','result.json')
    assert result['state']=='failed' and 'Retry input changed' in result['detail']
    output=attempt/'outputs/result.json';save(output,{'v':1})
    save(attempt/'receipt.json',dict(state='succeeded',outputs=[reference(output)]))
    save(output,{'v':2})
    assert check_pool(str(tmp_path),'r','result.json')==dict(state='failed',detail='Pool receipt output changed or missing')


def test_uncertain_observation_waits_for_same_attempt_receipt(tmp_path):
    from ecarsi.control.coordinator import check_pool, check_bridge
    from ecarsi.warm_pool.state import submit
    from ecarsi.agent.session import reference
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


def test_a_workflow_holds_the_handoff_without_the_sample_manifest(tmp_path):
    """Two 200-sample PanSci datasets failed at cross-sample with PayloadsTooLarge (2026-09-21):
    `read` returned the whole inspected.json into Temporal's history, and that file grows with
    the sample count -- 4.3 MB at 215 samples. The workflow counts samples and reads
    previous_round; nothing else may cross into history. A 14-sample dataset's file is 2 KB,
    so the bound below is loose for it and tight only where it matters."""
    import json
    from ecarsi.control.zoomin import zoomin_step
    fat = {'sample': None, 'bundle': {'path': 'p', 'sha256': '0' * 64}, 'n_cells': 100,
           'qc': {'k' + str(i): float(i) for i in range(40)},
           'annotation': {'clusters': [{'cluster_id': str(c), 'evidence': 'x' * 400} for c in range(40)]}}
    doc = {'samples': [{**fat, 'sample': f's{i}'} for i in range(215)],
           'files': {f's{i}/figures/umap_{j}.png': {'path': 'p', 'sha256': '0' * 64} for i in range(215) for j in range(30)},
           'empty': [], 'input': {'path': 'p', 'sha256': '0' * 64}, 'n_input': 21500,
           'spec': {'config': {'tissue': 'liver'}}}
    path = tmp_path / 'inspected.json'
    path.write_text(json.dumps(doc))
    assert path.stat().st_size > 2 * 1024 * 1024, 'the fixture must be over the payload limit'

    for step in (crosssample_step, zoomin_step):
        held = step('read', [str(path)])
        assert 'files' not in held
        assert len(held['samples']) == 215 and held['samples'][7] == {'sample': 's7', 'n_cells': 100}
        assert held['n_input'] == 21500 and held['spec'] == doc['spec'] and 'previous_round' not in held
        assert len(json.dumps(held)) < 64 * 1024

    # A later round's document has no sample list at all and passes through unchanged.
    later = {'previous_round': 1, 'input': doc['input'], 'n_input': 9000, 'spec': doc['spec']}
    path.write_text(json.dumps(later))
    assert crosssample_step('read', [str(path)]) == later


def test_a_long_history_continues_as_new_once_the_comparisons_are_in(monkeypatch):
    import ecarsi.control.crosssample as module
    class Continued(BaseException):
        def __init__(self, args): self.carried = args
    async def scenario():
        events=[];requests={};history={'n':10000}
        async def call(fn,action,args):
            if action in ('read','session'):
                path=args[0]
                if path=='inspect':return {'samples':[{},{}]}
                if path=='compute':return {'tasks':list(range(4))}
                if path.startswith('agent'):return {'session_id':path}
                if path=='decision-quality':return {}
                raise AssertionError(path)
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':events.append('publish');return 'publication'
            _,payload,parents=args
            name=('deg-'+str(payload['indices'][0]) if action=='deg-batch' else
                  action+('-'+payload['phase'] if action=='agent' else '-'+str(payload['index']) if action=='deg' else ''))
            requests[name]=(payload,parents);events.append(name)
            return {'id':name,'output':name}
        async def await_pool(spec,request):return request['id']
        async def child(fn,session,**kwargs):return 'decision-'+session['session_id'].split('-')[1]
        def continue_as_new(args):raise Continued(args)
        monkeypatch.setattr(module,'call',call);monkeypatch.setattr(module,'await_pool',await_pool)
        monkeypatch.setattr(module.workflow,'execute_child_workflow',child)
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='cross-sample/test',get_current_history_length=lambda:history['n']))
        monkeypatch.setattr(module.workflow,'wait',asyncio.wait)
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        monkeypatch.setattr(module.workflow,'continue_as_new',continue_as_new)
        monkeypatch.setattr(module,'DEG_BATCH_SIZE',1)
        spec={'max_in_flight_deg':3,'max_refinements':0}
        with pytest.raises(Continued) as stop:
            await CrosssampleWorkflow().run(spec)
        assert stop.value.carried==[spec,{'deg_limit':3}] and 'deg-3' in events and 'assemble' not in events
        history['n']=0
        assert await CrosssampleWorkflow().run(spec,{'deg_limit':3})=='publication'
    asyncio.run(scenario())


def test_comparisons_are_batched_eight_per_request(monkeypatch):
    import ecarsi.control.crosssample as module
    async def scenario():
        active=peak=0;events=[];requests={}
        async def call(fn,action,args):
            if action in ('read','session'):
                path=args[0]
                if path=='inspect':return {'samples':[{},{}]}
                if path=='compute':return {'tasks':list(range(20))}
                if path.startswith('agent'):return {'session_id':path}
                if path=='decision-quality':return {}
                raise AssertionError(path)
            if action=='accepted':return {'path':args[0],'parent':args[0]}
            if action=='publish':events.append('publish');return 'publication'
            _,payload,parents=args
            assert action!='deg'   # the patched path never submits single comparisons
            name=('deg-'+str(payload['indices'][0]) if action=='deg-batch' else action+('-'+payload['phase'] if action=='agent' else ''))
            requests[name]=(payload,parents);events.append(name)
            return {'id':name,'output':name}
        async def await_pool(spec,request):
            nonlocal active,peak
            if request['id'].startswith('deg-'):
                active+=1;peak=max(peak,active);await asyncio.sleep(.001);active-=1
            return request['id']
        async def child(fn,session,**kwargs):return 'decision-'+session['session_id'].split('-')[1]
        monkeypatch.setattr(module,'call',call);monkeypatch.setattr(module,'await_pool',await_pool)
        monkeypatch.setattr(module.workflow,'execute_child_workflow',child)
        monkeypatch.setattr(module.workflow,'info',lambda:SimpleNamespace(workflow_id='cross-sample/test',get_current_history_length=lambda:0))
        monkeypatch.setattr(module.workflow,'wait',asyncio.wait)
        monkeypatch.setattr(module.workflow,'patched',lambda name:True)
        assert await CrosssampleWorkflow().run({'max_in_flight_deg':2,'max_refinements':0})=='publication'
        assert [e for e in events if e.startswith('deg-')]==['deg-0','deg-8','deg-16'] and peak==2
        assert requests['deg-8'][0]['indices']==list(range(8,16)) and requests['deg-16'][0]['indices']==[16,17,18,19]
        assert requests['assemble'][0]['paths']==['compute','deg-0','deg-8','deg-16'] and len(requests['assemble'][1])==4
    asyncio.run(scenario())
