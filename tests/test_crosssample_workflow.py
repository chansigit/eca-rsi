"""Bounded numerical work must finish before type -> quality -> publication."""
import asyncio

import pytest
from temporalio import workflow

from ecarsi.control.crosssample import CrosssampleWorkflow, crosssample_step, validate_spec
from tests.temporal_env import QUEUE, fakes, temporal
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






def test_large_units_send_smaller_deg_batches_and_more_of_them_at_once():
    from ecarsi.control.common import deg_batches
    assert deg_batches(20, 0) == ([list(range(0, 8)), list(range(8, 16)), [16, 17, 18, 19]], 1)
    assert deg_batches(20, 50_000)[1] == 1
    batches, factor = deg_batches(20, 100_000)
    assert batches[0] == [0, 1, 2, 3] and len(batches) == 5 and factor == 2
    batches, factor = deg_batches(20, 418_322)   # the scale test: one comparison per request, 8x in flight
    assert batches == [[i] for i in range(20)] and factor == 8


@workflow.defn(name="AgentWorkflow")
class Agent:
    """Every session answers at once with 'decision-<phase>'."""
    @workflow.run
    async def run(self, session: dict) -> str:
        return "decision-" + session["session_id"].split("-")[1]


def stage_fakes(tasks, events, requests, prepared=None):
    """crosssample_step: inspection of two samples, `tasks` comparisons, sessions named after their phase;
    every submitted request is named after its action and returns its name as the pool path."""
    async def step(action, args):
        if action in ("read", "session"):
            path = args[0]
            if path == "inspect":
                return {"samples": [{}, {}]}
            if path == "compute":
                return {"tasks": list(range(tasks)), **(prepared or {})}
            if path.startswith("agent"):
                return {"session_id": path}
            if path == "decision-quality":
                return {}
            raise AssertionError(path)
        if action == "accepted":
            return {"path": args[0], "parent": args[0]}
        if action == "publish":
            events.append("publish")
            return "publication"
        _, payload, parents = args
        assert action != "deg"  # single comparisons are never submitted
        name = "deg-" + str(payload["indices"][0]) if action == "deg-batch" else action + ("-" + payload["phase"] if action == "agent" else "")
        requests[name] = (payload, parents)
        events.append(name)
        return {"id": name, "output": name}
    return step


def overlapping_pool(live, on_first=None):
    """check_pool that keeps each DEG request waiting for two polls, counting the requests in flight."""
    polls = {}
    async def check_pool(pool_root, request_id, output):
        polls[request_id] = polls.get(request_id, 0) + 1
        if request_id.startswith("deg-"):
            if polls[request_id] == 1:
                live["active"] += 1
                live["peak"] = max(live["peak"], live["active"])
                if on_first:
                    await on_first(request_id)
            if polls[request_id] < 3:
                return {"state": "waiting"}
            if polls[request_id] == 3:
                live["active"] -= 1
        return {"state": "ready", "path": request_id}
    return check_pool


SPEC = {"max_in_flight_deg": 3, "max_refinements": 0, "pool_root": "pool"}


@pytest.mark.parametrize("change_limit", [False, True])
def test_workflow_fanout_and_annotation_order(change_limit):
    events, requests, live, client = [], {}, {"active": 0, "peak": 0}, {}
    async def raise_the_limit(request_id):
        if change_limit and request_id == "deg-0":
            handle = client["client"].get_workflow_handle("cross-sample/test")
            assert await handle.execute_update(CrosssampleWorkflow.set_deg_limit, 5) == 5
    async def scenario():
        activities = fakes(crosssample_step=stage_fakes(80, events, requests),
                           check_pool=overlapping_pool(live, raise_the_limit))
        async with temporal([CrosssampleWorkflow, Agent], activities) as client["client"]:
            handle = await client["client"].start_workflow(CrosssampleWorkflow.run, SPEC, id="cross-sample/test", task_queue=QUEUE)
            assert await handle.result() == "publication"
            return await handle.query(CrosssampleWorkflow.stage)
    assert asyncio.run(scenario()) == "complete"
    assert live["peak"] == (5 if change_limit else 3) and events.index("assemble") > events.index("deg-72")
    assert events.index("agent-type") < events.index("agent-quality") < events.index("finalize")
    assert len(requests["assemble"][1]) == 11  # the computed bundle and ten batches of eight
    assert requests["finalize"][0]["paths"] == ["assemble", "decision-type", "decision-quality"]


@workflow.defn
class WaitsForOneRequest:
    @workflow.query
    def stage(self) -> str:
        from ecarsi.control.common import stage_with_waits
        return stage_with_waits(self)

    @workflow.run
    async def run(self, pool_root: str) -> str:
        from ecarsi.control.common import await_pool
        self._stage = "DEG comparisons"
        return await await_pool({"pool_root": pool_root}, {"id": "r", "output": "result.json"})


def test_a_blocked_pool_request_shows_why_it_waits(tmp_path):
    # #18: the scheduler's infeasible reason reaches the waiting workflow's stage query.
    from ecarsi.control.coordinator import check_pool
    from ecarsi.warm_pool.backend import observe
    from ecarsi.warm_pool.state import submit
    tmp_path.chmod(0o700); (tmp_path/"requests").mkdir(); save(tmp_path/"config.json", {"runtime": {}})
    submit(tmp_path, dict(request_id="r", operation_id="compute", args=["-c", "pass"], cpus=1,
        memory_mb=64, timeout_seconds=30, outputs=["result.json"]))
    folder, why = tmp_path/"requests/r", "no worker that holds 1 cpus / 64 MB has 60 s left"
    observe(folder, read(folder/"request.json"), dict(state="queued", infeasible=why))
    assert check_pool(str(tmp_path), "r", "result.json") == dict(state="waiting", detail="infeasible: " + why)
    seen, client = [], {}
    async def blocked_then_ready(pool_root, request_id, output):
        if not seen:
            seen.append(None)
            return check_pool(pool_root, request_id, output)
        seen.append(await client["client"].get_workflow_handle("wait-test").query(WaitsForOneRequest.stage))
        return {"state": "ready", "path": "/done"}
    async def scenario():
        async with temporal([WaitsForOneRequest], fakes(check_pool=blocked_then_ready)) as client["client"]:
            handle = await client["client"].start_workflow(WaitsForOneRequest.run, str(tmp_path), id="wait-test", task_queue=QUEUE)
            assert await handle.result() == "/done"
            return await handle.query(WaitsForOneRequest.stage)
    assert asyncio.run(scenario()) == "DEG comparisons"
    assert seen[1] == "DEG comparisons; waiting: infeasible: " + why


def test_a_long_history_continues_as_new_once_the_comparisons_are_in():
    events, requests = [], {}
    async def scenario():
        activities = fakes(crosssample_step=stage_fakes(4, events, requests), check_pool=overlapping_pool({"active": 0, "peak": 0}))
        async with temporal([CrosssampleWorkflow, Agent], activities) as client:
            # A history budget of one event: the first run continues as new right after its comparisons and
            # carries only the DEG window, so the continued run has the normal budget and finishes.
            return await client.execute_workflow(CrosssampleWorkflow.run, args=[SPEC, {"history_limit": 1}],
                                                 id="cross-sample/test", task_queue=QUEUE)
    assert asyncio.run(scenario()) == "publication"
    second = [i for i, e in enumerate(events) if e == "inspect"][1]
    assert events.count("inspect") == 2 and "deg-0" in events[:second] and "assemble" not in events[:second]
    assert events.count("assemble") == 1 and events.count("publish") == 1


@pytest.mark.parametrize("cells", [None, 418_322])
def test_comparisons_are_batched_eight_per_request(cells):
    events, requests, live = [], {}, {"active": 0, "peak": 0}
    async def scenario():
        activities = fakes(crosssample_step=stage_fakes(20, events, requests, {"n_selected": cells} if cells else None),
                           check_pool=overlapping_pool(live))
        async with temporal([CrosssampleWorkflow, Agent], activities) as client:
            return await client.execute_workflow(CrosssampleWorkflow.run, dict(SPEC, max_in_flight_deg=2),
                                                 id="cross-sample/test", task_queue=QUEUE)
    assert asyncio.run(scenario()) == "publication"
    # submitted in any order: Temporal runs the concurrent submit activities in parallel
    degs = sorted((e for e in events if e.startswith("deg-")), key=lambda d: int(d[4:]))
    if cells:   # 418k cells: one comparison per request, 2 x 8 requests in flight
        assert degs == ["deg-" + str(i) for i in range(20)] and live["peak"] == 16
        return
    assert degs == ["deg-0", "deg-8", "deg-16"] and live["peak"] == 2
    assert requests["deg-8"][0]["indices"] == list(range(8, 16)) and requests["deg-16"][0]["indices"] == [16, 17, 18, 19]
    assert requests["assemble"][0]["paths"] == ["compute", "deg-0", "deg-8", "deg-16"] and len(requests["assemble"][1]) == 4

