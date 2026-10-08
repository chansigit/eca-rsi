import asyncio

import pytest
from temporalio.client import WorkflowFailureError

from ecarsi.control.zoomin import ZoominWorkflow, zoomin_step
from ecarsi.files import save, read
from tests.fake_agent import Agent
from tests.temporal_env import QUEUE, fakes, temporal


def test_gpu_grant_is_selected_for_lineage_compute(tmp_path):
    pool = tmp_path / 'pool'
    pool.mkdir(mode=0o700)
    (pool / 'requests').mkdir()
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


def zoom_fakes(lines, requests, events, fail=lambda action, n: False, on_ready=lambda action, n: None):
    """zoomin_step and check_pool for a plan of `lines`: each request is named after its action (a session after
    its kind and, for a lineage, its index), and the pool records (action, payload) when it hands the result."""
    async def step(action, args):
        if action in ("read", "session"):
            path = args[0]
            if path == "lineage-decision":
                return {"evidence": {"path": "evidence"}}
            if path == "plan-decision":
                return {"proposal": {"lineages": lines}}
            if path.startswith("compute"):
                return {"tasks": []}
            if path.startswith("agent"):
                return {"session_id": path}
            raise AssertionError(path)
        if action == "accepted":
            return {"path": args[0], "parent": args[0]}
        if action == "publish":
            return "publication"
        payload = args[1]
        n = len(requests)
        if action == "agent":
            lineage = payload["kind"] == "lineage" and requests[payload["paths"][0]][1]["lineage"]
            identifier = f"agent-{payload['kind']}-{lineage}-{n}"
        else:
            identifier = f"{action}-{n}"
        if action == "subset":
            payload = dict(payload, lineage=payload["index"])
        elif action in ("compute", "assemble"):
            payload = dict(payload, lineage=requests[payload["paths"][0]][1].get("lineage"))
        requests[identifier] = (action, payload)
        return {"id": identifier, "output": identifier}
    counts = {}
    async def check_pool(pool_root, request_id, output):
        action, payload = requests[request_id]
        counts[action] = counts.get(action, 0) + 1
        if fail(action, counts[action]):
            return {"state": "failed", "detail": "one lineage failed"}
        events.append((action, payload))
        on_ready(action, counts[action])
        return {"state": "ready", "path": request_id}
    return fakes(zoomin_step=step, check_pool=check_pool)


def sessions(answer):
    """agent_outcome: the plan session decides at once; a lineage session gets answer(session, workflow id)."""
    async def agent_outcome(session, workflow_id):
        if session["session_id"].startswith("agent-plan"):
            return {"result": "plan-decision"}
        return answer(session, workflow_id)
    return fakes(agent_outcome=agent_outcome)


def run_zoom(activities, progress=None, spec=None):
    async def scenario():
        async with temporal([ZoominWorkflow, Agent], activities) as client:
            return await client.execute_workflow(ZoominWorkflow.run, args=[spec or SPEC] + ([progress] if progress else []),
                                                 id="zoom-test", task_queue=QUEUE)
    return asyncio.run(scenario())


SPEC = dict(max_in_flight_lineages=1, max_in_flight_deg=2, pool_root="pool")


@pytest.mark.parametrize("fail_first", [False, True])
def test_model_wait_releases_lineage_compute_admission(fail_first):
    # One compute slot: a lineage's session waits until the second lineage has computed, which only
    # happens if the first lineage gave its compute admission back before its model wait.
    requests, events, computed = {}, [], {"n": 0}
    def counted(action, n):
        if action == "compute":
            computed["n"] = n
    def answer(session, workflow_id):
        return {"result": "lineage-decision"} if computed["n"] >= 2 else {"wait": True}
    activities = zoom_fakes([{"zoom": True}, {"zoom": True}], requests, events,
                            fail=lambda action, n: fail_first and action == "compute" and n == 1, on_ready=counted)
    actions = lambda: [a for a, _ in events]
    if fail_first:
        with pytest.raises(WorkflowFailureError) as failed:
            run_zoom(activities + sessions(answer))
        assert "completed lineages retained" in str(failed.value.cause)
        assert computed["n"] == 2 and actions().count("apply") == 1 and "merge" not in actions()
    else:
        assert run_zoom(activities + sessions(answer)) == "publication"
        assert actions().count("compute") == 2 and actions()[-1] == "merge"


def test_lineage_window_continues_as_new_and_the_continued_run_finishes_the_rest():
    requests, events = {}, []
    lines = [{"zoom": True, "name": n, "n_cells": 1} for n in "abc"]
    activities = zoom_fakes(lines, requests, events) + sessions(lambda session, workflow_id: {"result": "lineage-decision"})
    # A window of two lineages (one compute slot) and a history budget the first window outgrows: the
    # first run starts lineages 0 and 1, never 2, and continues as new once both are applied; the continued
    # run has the normal budget (the limit is never carried) and finishes lineage 2. Measured 2026-10-05: the
    # first window check sees fewer than 60 events, one finished lineage far more than 200.
    assert run_zoom(activities, progress={"history_limit": 100}) == "publication"
    actions = [a for a, _ in events]
    second = [i for i, a in enumerate(actions) if a == "prepare"][1]
    first_run = [p["lineage"] for a, p in events[:second] if a == "subset"]
    continued = [p["lineage"] for a, p in events[second:] if a == "subset"]
    assert sorted(first_run) == [0, 1] and continued == [2] and actions.count("apply") == 3
    assert "merge" not in actions[:second] and len(events[-1][1]["paths"]) == 5  # prepared, plan and all three lineages

