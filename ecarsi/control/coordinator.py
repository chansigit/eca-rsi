"""Temporal Work Coordinator: durable Organize handoffs through Pool and Bridge."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import time

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

QUEUE = "ecarsi-durable-v2"
# 120 s: a shared-filesystem hiccup of a minute (2026-09-17 saw ~90 s) must not time out a host step.
SHORT = timedelta(seconds=120)
RETRY = RetryPolicy(maximum_attempts=3)
# Polls only read durable state, so a stalled shared filesystem must not end the session: on 2026-09-17
# a ~90 s Lustre stall on the coordinator node timed out three 30 s check_bridge attempts in five agent
# workflows and failed three datasets. Forty attempts at the default backoff capped at 60 s ride out ~35 min.
POLL_RETRY = RetryPolicy(maximum_attempts=40, maximum_interval=timedelta(seconds=60))


def activity_retry(fn):
    return POLL_RETRY if fn.__name__ in {"check_pool", "check_bridge"} else RETRY


def task_trace(spec, unit_id):
    return {"workflow_id": "organize/" + spec["run_id"],
            "dataset_id": spec.get("dataset_id", spec["run_id"]), "unit_id": unit_id}


@activity.defn
def submit_prepare(spec: dict) -> str:
    from ..warm_pool.state import submit
    request_id = spec["run_id"] + ".prepare"
    submit(spec["pool_root"], dict(request_id=request_id, operation_id="organize.prepare",
        trace=task_trace(spec, "organize.prepare"),
        args=["-m", "ecarsi.stages.organize", "prepare", spec["input_root"], "prepared.json"],
        cpus=spec["prepare_cpus"], memory_mb=spec["prepare_memory_mb"],
        timeout_seconds=spec["prepare_timeout_seconds"], inputs=[], outputs=["prepared.json"]))
    return request_id


@activity.defn
def submit_plan(spec: dict, prepared_path: str) -> str:
    from ..agent import submit
    from ..warm_pool.state import read
    prepared = read(prepared_path)
    if prepared is None or Path(prepared["input_root"]).resolve() != Path(spec["input_root"]).resolve():
        raise ValueError("prepared input identity does not match this dataset")
    request_id = spec["run_id"] + ".plan"
    submit(spec["bridge_root"], dict(request_id=request_id, operation_id="organize.plan",
                                   trace=task_trace(spec, "organize.plan"),
                                   profiles=prepared["profiles"], cwd=spec["input_root"]))
    return request_id


@activity.defn
def submit_execute(spec: dict, prepared_path: str, reply_path: str, plan_parent: str | None = None) -> str:
    from ..warm_pool.state import file_digest, submit
    request_id = spec["run_id"] + ".execute"
    trace = task_trace(spec, "organize.execute")
    if plan_parent:
        trace["depends_on"] = [plan_parent]
    submit(spec["pool_root"], dict(request_id=request_id, operation_id="organize.execute",
        trace=trace,
        args=["-m", "ecarsi.stages.organize", "execute", prepared_path, reply_path, "."],
        cpus=spec["execute_cpus"], memory_mb=spec["execute_memory_mb"],
        timeout_seconds=spec["execute_timeout_seconds"],
        inputs=[{"path": path, "sha256": file_digest(path)} for path in (prepared_path, reply_path)],
        outputs=["completion.json", "run/organize/manifest.json"]))
    return request_id


POLL_WAIT_SECONDS = 20  # below SHORT; a check activity waits this long before answering 'waiting'
POLL_STEP_SECONDS = 2


def long_poll(check):
    """Wait inside the activity instead of one activity per poll: a 13-turn session recorded
    ~6,500 history events, almost all poll activities and timers, and every poll was a Temporal
    round trip. The workflow loop is unchanged; it just sees far fewer 'waiting' answers."""
    deadline = time.monotonic() + POLL_WAIT_SECONDS
    while True:
        result = check()
        if result["state"] != "waiting" or time.monotonic() >= deadline:
            return result
        try:
            activity.heartbeat()
        except RuntimeError:
            pass  # not inside an activity (direct call in tests)
        time.sleep(POLL_STEP_SECONDS)


@activity.defn
def check_pool(root: str, request_id: str, output: str) -> dict:
    return long_poll(lambda: check_pool_once(root, request_id, output))


def check_pool_once(root, request_id, output):
    from ..warm_pool.state import file_digest, read, retry, status
    state = status(root, request_id)
    if state["state"] == "succeeded":
        receipt = state["receipt"]
        selected = next((item for item in receipt["outputs"] if Path(item["path"]).name == output), None)
        if selected is None or file_digest(selected["path"]) != selected["sha256"]:
            raise ValueError("Pool receipt output changed or missing")
        return {"state": "ready", "path": selected["path"],
                "attempt_id": state["attempt_id"]}
    if state["state"] == "failed":
        request = read(Path(root) / "requests" / request_id / "request.json")
        spec, error, retryable = request["spec"], str(state["receipt"].get("error", "")), state["receipt"].get("retryable") is True
        if request.get("retry_count", 0) < 2:
            # A budget kill gets one attempt at twice the budget (or on CPUs for a preferred GPU):
            # a 180 s prepare step stalled by a node's Lustre client and a 4 GiB GPU budget blown by
            # a 50k-cell PCA each failed whole datasets (2026-09-18). The worker's `retryable`
            # covers interruptions whose budget was fine.
            if error.startswith("MemoryError") and "GPU" in error and spec.get("gpu", {}).get("mode") == "preferred":
                retry(root, request_id, without_gpu=True, reason="Automatic retry on CPUs after the GPU memory budget")
            elif error.startswith("MemoryError") and retryable:
                retry(root, request_id, memory_mb=2 * spec["memory_mb"],
                      reason="Automatic retry at twice the budget after the RSS watchdog")
            elif error.startswith("TimeoutError"):
                retry(root, request_id, timeout_seconds=2 * spec["timeout_seconds"],
                      reason="Automatic retry at twice the time limit after the execution deadline")
            elif retryable:
                retry(root, request_id, reason="Automatic recovery after a confirmed local interruption")
            else:
                return {"state": "failed", "detail": error}
            return {"state": "waiting"}
    if state["state"] == "unknown_external_result":
        return {"state": "waiting", "detail": "unknown_external_result"}
    if state["state"] in {"failed", "cancelled"}:
        return {"state": state["state"], "detail": (state["receipt"] or {}).get("error")}
    return {"state": "waiting"}


@activity.defn
def check_bridge(root: str, request_id: str) -> dict:
    return long_poll(lambda: check_bridge_once(root, request_id))


def check_bridge_once(root, request_id):
    from ..agent import root_path, status
    result = status(root, request_id)
    if result["state"] == "reply_saved":
        path = root_path(root) / "requests" / request_id / "result.json"
        if not path.is_file():
            raise ValueError("Bridge reply receipt is absent")
        return {"state": "ready", "path": str(path)}
    if result["state"] == "unknown_external_result":
        return {"state": "waiting", "detail": "unknown_external_result"}
    if result["state"] == "failed":
        return {"state": result["state"], "detail": result.get("reason")}
    # 13 % of model replies arrive within 15 s; the queued state lasts only until the bridge dispatches.
    return {"state": "waiting", "poll_seconds": 5 if result['state'] == 'queued' else 3}


@activity.defn
def accept_organize(output: str, destination: str) -> str:
    from ..stages.organize import publish
    return publish(Path(output).parent, Path(destination))


@activity.defn
def organize_agent_step(action: str, args: list):
    from ..stages.organize import planning_spec, accepted_plan
    return {"spec": planning_spec, "accept": accepted_plan}[action](*args)


@workflow.defn
class OrganizeWorkflow:
    @workflow.query
    def stage(self) -> str:
        return getattr(self, "_stage", "created")

    @workflow.run
    async def run(self, spec: dict) -> str:
        async def call(fn, *args):
            try:
                return await workflow.execute_activity(fn, args=args,
                    start_to_close_timeout=SHORT, retry_policy=activity_retry(fn))
            except Exception as exc:
                raise ApplicationError(f"{fn.__name__} failed: {exc}", non_retryable=True) from exc

        async def await_pool(request_id, output):
            while True:
                result = await call(check_pool, spec["pool_root"], request_id, output)
                if result["state"] == "ready":
                    return result["path"]
                if result["state"] != "waiting":
                    raise ApplicationError(f"{request_id}: {result['state']}: {result.get('detail')}",
                                           non_retryable=True)
                await workflow.sleep(5)

        self._stage = "preparing"
        prepare_id = await call(submit_prepare, spec)
        prepared_path = await await_pool(prepare_id, "prepared.json")
        self._stage = "planning"
        if workflow.patched("organize-worker-plan-v1"):
            agent_spec = await call(organize_agent_step, "spec", [spec, prepared_path])
            result = await workflow.execute_child_workflow(AgentWorkflow.run, agent_spec,
                id=workflow.info().workflow_id + "/plan")
            accepted = await call(organize_agent_step, "accept", [spec, result, prepared_path])
            execute_args = [spec, prepared_path, accepted["path"], accepted["request_id"]]
        else:
            # Existing histories retain the original planning activities on replay.
            plan_id = await call(submit_plan, spec, prepared_path)
            while True:
                result = await call(check_bridge, spec["bridge_root"], plan_id)
                if result["state"] == "ready":
                    reply_path = result["path"]
                    break
                if result["state"] != "waiting":
                    raise ApplicationError(f"{plan_id}: {result['state']}: {result.get('detail')}",
                                           non_retryable=True)
                await workflow.sleep(5)
            execute_args = [spec, prepared_path, reply_path]
        self._stage = "executing"
        execute_id = await call(submit_execute, *execute_args)
        completion = await await_pool(execute_id, "completion.json")
        self._stage = "publishing"
        return await call(accept_organize, completion, spec["output_root"])


ACTIVITIES = [submit_prepare, submit_plan, submit_execute, check_pool, check_bridge, accept_organize, organize_agent_step]


@activity.defn
def agent_step(action: str, args: list):
    from ..agent import session
    from ..warm_pool.state import immutable, read, reference, verified
    if action == "cached_completion":
        session_ref = args[0]
        spec = verified(session_ref)["spec"]
        path = Path(spec["output_root"]) / "result.json"
        result = read(path)
        if not result or "output" not in result:
            return None
        if result["session"] != session_ref:
            raise ValueError("Completed agent result belongs to another session")
        from ..warm_pool.state import status
        state = status(spec["pool_root"], result["pool_request_id"])
        if state["state"] != "succeeded" or result["output"] not in [
                {k: item[k] for k in ("path", "sha256")} for item in state["receipt"]["outputs"]]:
            raise ValueError("Completed agent result is no longer accepted")
        if verified(result["output"]).get("accepted") is not True:
            raise ValueError("Completed agent submission is not accepted")
        return str(path)
    if action == "decision":
        reply = read(args[0])["response"]
        return {"kind": reply["kind"], "calls": len(reply["calls"])}
    if action == 'parallel':
        from ..agent.parallel import choose
        return choose(*args)
    if action == "finish":
        spec = verified(args[0])["spec"]
        if session.turn_reply(args[0], args[1])["kind"] != "final":
            raise ValueError("Cannot finish an agent with pending tools")
        return immutable(Path(spec["output_root"]) / "result.json",
                                 {"session": args[0], "reply": reference(args[1])})["path"]
    if action == "restart":
        spec, reason = args
        root = Path(spec["output_root"])
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = dict(spec, session_id=spec["session_id"] + "-r2", output_root=str(root / "restart"))
        intent = immutable(root / "restart.json", dict(reason=reason, superseded=spec["session_id"], spec=fresh))
        return verified(intent)["spec"]
    if action == "tool":
        from jsonschema import ValidationError
        from ..agent.tool_errors import reject_arguments
        try:
            return session.tool_request(*args)
        except (ValidationError, session.ToolRejection) as exc:
            # A model can correct its request; never run an invalid command, and
            # never let a malformed model call fail the whole dataset.
            return reject_arguments(*args, message=getattr(exc, "message", None) or str(exc))
    operations = {"create": session.create_session, "model": session.submit_turn,
                  "resume": session.continuation, "reset": session.reset_session,
                  "complete_tool": session.complete_tool}
    return operations[action](*args)


MAX_CONTEXT_RESETS = 2  # fresh conversations per session after the provider rejects the transcript
RESETTABLE = {"provider_error", "timeout"}


@workflow.defn
class AgentWorkflow:
    """Model turns and worker programs alternate without holding each other's capacity."""
    @workflow.query
    def stage(self) -> str:
        return getattr(self, "_stage", "created")

    @workflow.run
    async def run(self, spec: dict) -> str:
        async def call(fn, *args):
            return await workflow.execute_activity(fn, args=args,
                start_to_close_timeout=SHORT, retry_policy=activity_retry(fn))

        session = await call(agent_step, "create", [spec])
        if workflow.patched("agent-reuse-completed-submission-v1"):
            completed = await call(agent_step, "cached_completion", [session])
            if completed:
                self._stage = "complete"
                return completed
        context, parents, number, resets = None, [], 0, 0
        resettable = workflow.patched("agent-context-reset-v1")
        for turn in range(spec["max_turns"]):
            self._stage = "model"
            request = await call(agent_step, "model", [session, number, context, parents])
            while True:
                result = await call(check_bridge, spec["bridge_root"], request)
                if result["state"] == "ready":
                    break
                if result["state"] != "waiting":
                    if (resettable and context is not None and resets < MAX_CONTEXT_RESETS
                            and result.get("detail") in RESETTABLE):
                        request = None
                        break
                    raise ApplicationError(f"{request}: {result['state']}", non_retryable=True)
                await workflow.sleep(result.get('poll_seconds', 2)
                    if workflow.patched('agent-queued-poll-backoff-v1') else 2)
            if request is None:
                # The provider kept rejecting the grown transcript: the same judgement continues
                # in a fresh conversation, with the host state carried over.
                resets += 1
                session = await call(agent_step, "reset", [session, context, resets + 1, result["detail"]])
                context, parents, number = None, [], 0
                continue
            number += 1
            reply = result["path"]
            decision = await call(agent_step, "decision", [reply])
            if decision["kind"] == "final":
                self._stage = "complete"
                return await call(agent_step, "finish", [session, reply])
            wider_batch = workflow.patched('agent-read-batch-window-v1')
            if decision["kind"] != "tools" or not 1 <= decision["calls"] <= (64 if wider_batch else 16):
                raise ApplicationError("Invalid agent tool boundary", non_retryable=True)
            self._stage = "tools"
            accepted, parents = [], []
            parallel = await call(agent_step, 'parallel', [session, reply]) if wider_batch else False

            async def run_tool(index, previous):
                item = await call(agent_step, "tool", [session, reply, index, previous])
                while True:
                    result = await call(check_pool, spec["pool_root"], item["request_id"], item["result_file"])
                    if result["state"] == "ready":
                        break
                    if result["state"] != "waiting":
                        raise ApplicationError(f"{item['request_id']}: {result['state']}", non_retryable=True)
                    await workflow.sleep(2)
                return {**item, "path": result["path"]}

            if parallel:
                # Bound per-agent fan-out; Pool still enforces aggregate CPU/RAM/GPU grants.
                pending, index = {}, 0
                while index < decision['calls'] or pending:
                    while index < decision['calls'] and len(pending) < 4:
                        task = asyncio.create_task(run_tool(index, None))
                        pending[task] = index
                        index += 1
                    done, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in sorted(done, key=lambda t: pending[t]):
                        pending.pop(task)
                        accepted.append(await task)
                accepted.sort(key=lambda item: item['index'])
                parents = [item['request_id'] for item in accepted]
            else:
                for index in range(decision['calls']):
                    item = await run_tool(index, parents[-1] if parents else None)
                    accepted.append(item)
                    parents.append(item['request_id'])
            context = await call(agent_step, "resume", [session, reply, accepted, True] if parallel else [session, reply, accepted])
            if spec.get("completion_tool"):
                completed = await call(agent_step, "complete_tool", [session, context])
                if completed:
                    self._stage = "complete"
                    return completed
        raise ApplicationError("Agent model-turn budget exhausted", non_retryable=True)


async def run_agent(spec, identity, call):
    """One session's AgentWorkflow child. A session that dies (turn budget, provider, host error)
    runs once more from the same evidence as a fresh session with a new id and directory before
    the failure reaches the stage; resume treats the dead session's requests as superseded."""
    try:
        return await workflow.execute_child_workflow(AgentWorkflow.run, spec, id=identity)
    except Exception as exc:
        fresh = await call(agent_step, "restart", [spec, str(exc)])
        return await workflow.execute_child_workflow(AgentWorkflow.run, fresh, id=identity + "/restart")


def validate_spec(spec):
    from ..warm_pool.state import identifier, pool_root
    from ..agent import root_path
    required = {
        "run_id", "input_root", "output_root", "pool_root", "bridge_root",
        "prepare_cpus", "prepare_memory_mb", "prepare_timeout_seconds",
        "execute_cpus", "execute_memory_mb", "execute_timeout_seconds"}
    if not isinstance(spec, dict) or not required <= spec.keys() or spec.keys() - required - {"dataset_id"}:
        raise ValueError("Organize Workflow requires explicit input, output, services and resource budgets")
    identifier(spec["run_id"])
    if "dataset_id" in spec:
        from ..warm_pool.state import validate_trace
        validate_trace(task_trace(spec, "organize"))
    src = Path(spec["input_root"])
    dst = Path(spec["output_root"])
    if not src.is_absolute() or not src.is_dir() or not dst.is_absolute() or dst.exists() or src in dst.parents:
        raise ValueError("input must exist and output must be a fresh absolute path outside the input tree")
    if not dst.parent.is_dir():
        raise ValueError("output parent directory must exist")
    pool_root(spec["pool_root"])
    root_path(spec["bridge_root"])
    for name in ("prepare_cpus", "prepare_memory_mb", "prepare_timeout_seconds",
                 "execute_cpus", "execute_memory_mb", "execute_timeout_seconds"):
        if type(spec[name]) is not int or spec[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    return spec


ACTIVITY_SLOTS = 256  # check activities hold a slot for up to POLL_WAIT_SECONDS; one per in-flight request/turn


async def run_worker(client, task_queue, workflow_slots=None, activity_slots=None):
    from .persample import PersampleWorkflow, SampleWorkflow, sample_step
    from .crosssample import CrosssampleWorkflow, crosssample_step
    from .zoomin import ZoominWorkflow, zoomin_step
    from .dataset import DatasetWorkflow, AnalysisUnitWorkflow, dataset_step
    if workflow_slots is None:
        # One slot also constrains long-poll admission: idle coordinators left
        # sticky continuations waiting almost 10s. Two keeps admission moving
        # while still bounding GIL contention during large cold history replays.
        workflow_slots = 2
    if workflow_slots < 1:
        raise ValueError("Workflow slots must be positive")
    activity_slots = ACTIVITY_SLOTS if activity_slots is None else activity_slots
    if activity_slots < 1:
        raise ValueError("Activity slots must be positive")
    # The SDK default permits 500 concurrent replays; cold recovery must fit this host.
    # Check activities now wait up to POLL_WAIT_SECONDS each, so slots must cover every
    # in-flight pool request and model turn of this coordinator's workflows, not just bursts.
    with ThreadPoolExecutor(max_workers=activity_slots) as executor:
        async with Worker(client, task_queue=task_queue, max_concurrent_workflow_tasks=workflow_slots,
                workflows=[OrganizeWorkflow, AgentWorkflow, PersampleWorkflow, SampleWorkflow, CrosssampleWorkflow, ZoominWorkflow, DatasetWorkflow, AnalysisUnitWorkflow],
                activities=ACTIVITIES + [agent_step, sample_step, crosssample_step, zoomin_step, dataset_step],
                activity_executor=executor, max_concurrent_activities=activity_slots):
            await asyncio.Future()


async def follow_service(root, task_queue, workflow_slots=None, activity_slots=None):
    """Reconnect after a service handoff without cancelling Pool or Bridge work."""
    from .temporal import endpoint
    last_state = None
    while True:
        try:
            current = endpoint(root)
            client = await asyncio.wait_for(Client.connect(current['endpoint']), 10)
        except (ConnectionError, RuntimeError, TimeoutError) as exc:
            message = 'Waiting for Temporal service: ' + str(exc)
            if message != last_state:
                print(message, flush=True)
                last_state = message
            await asyncio.sleep(5)
            continue
        print('Connected to Temporal service generation ' + current['generation'], flush=True)
        last_state = None
        running = asyncio.create_task(run_worker(client, task_queue, workflow_slots, activity_slots))
        try:
            while True:
                done, _ = await asyncio.wait([running], timeout=5)
                if done:
                    await running  # Code/configuration errors must remain visible.
                    return
                try:
                    if endpoint(root)['generation'] == current['generation']:
                        continue
                except ConnectionError:
                    pass
                break
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_mutually_exclusive_group(required=True)
    connection.add_argument("--temporal", help="explicit Temporal Service host:port")
    connection.add_argument("--service-root", type=Path, help="shared Temporal service discovery directory")
    parser.add_argument("--task-queue", help="default %s; resume commands default to the queue of the run they resume" % QUEUE)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("worker")
    p.add_argument("--workflow-slots", type=int, help="concurrent workflow activations; default 2 to keep polling responsive and bound Python history replay; activities and Pool tasks remain concurrent")
    p.add_argument("--activity-slots", type=int, help="concurrent activities; default %d, one per in-flight pool request or model turn because check activities long-poll" % ACTIVITY_SLOTS)
    p = commands.add_parser("start")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-agent")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-persample")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-crosssample")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-zoomin")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-dataset")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("resume-dataset")
    p.add_argument("run_id")
    p.add_argument("--reason", required=True)
    p = commands.add_parser("set-persample-limit")
    p.add_argument("run_id")
    p.add_argument("limit", type=int)
    p = commands.add_parser('set-deg-limit')
    p.add_argument('stage', choices=['cross-sample', 'zoom-in'])
    p.add_argument('run_id')
    p.add_argument('limit', type=int)
    for name in ("status", "status-agent", "status-persample", "resume-persample", "status-crosssample", "resume-crosssample", "status-zoomin", "resume-zoomin", "status-dataset"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
    args = parser.parse_args()
    if args.task_queue is None and not args.command.startswith("resume-"):
        args.task_queue = QUEUE
    if args.command == 'worker' and args.service_root:
        await follow_service(args.service_root, args.task_queue, args.workflow_slots, args.activity_slots)
        return
    if args.service_root:
        from .temporal import endpoint
        args.temporal = endpoint(args.service_root)['endpoint']
    runtime = None
    if args.command != 'worker':
        from temporalio.runtime import Runtime, TelemetryConfig
        # Client-only commands have no worker to report. Starting the optional
        # SDK heartbeat thread can race native teardown in short-lived clients.
        runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=None)
    client = await Client.connect(args.temporal, runtime=runtime)
    if args.command == "worker":
        await run_worker(client, args.task_queue, args.workflow_slots, args.activity_slots)
    elif args.command in {"start", "start-agent", "start-persample", "start-crosssample", "start-zoomin", "start-dataset"}:
        if args.command == "start-dataset":
            from .dataset import DatasetWorkflow, validate_spec as validate_dataset
            spec = validate_dataset(json.loads(args.spec.read_text()))
            run, identity = DatasetWorkflow.run, 'dataset/' + spec['run_id']
        elif args.command == "start-agent":
            from ..agent.session import validate_spec as validate_agent
            spec = validate_agent(json.loads(args.spec.read_text()))
            run, identity = AgentWorkflow.run, "agent/" + spec["session_id"]
        elif args.command == "start-persample":
            from .persample import PersampleWorkflow, validate_spec as validate_samples
            spec = validate_samples(json.loads(args.spec.read_text()))
            run, identity = PersampleWorkflow.run, "persample/" + spec["run_id"]
        elif args.command == "start-zoomin":
            from .zoomin import ZoominWorkflow, validate_spec as validate_zoomin
            spec = validate_zoomin(json.loads(args.spec.read_text()))
            run, identity = ZoominWorkflow.run, "zoom-in/" + spec["run_id"]
        elif args.command == "start-crosssample":
            from .crosssample import CrosssampleWorkflow, validate_spec as validate_crosssample
            spec = validate_crosssample(json.loads(args.spec.read_text()))
            run, identity = CrosssampleWorkflow.run, "cross-sample/" + spec["run_id"]
        else:
            spec = validate_spec(json.loads(args.spec.read_text()))
            run, identity = OrganizeWorkflow.run, "organize/" + spec["run_id"]
        handle = await client.start_workflow(run, spec,
                      id=identity, task_queue=args.task_queue,
                      id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        print(handle.id)
    elif args.command == 'set-persample-limit':
        from .persample import PersampleWorkflow
        from ..warm_pool.state import identifier
        handle = client.get_workflow_handle('persample/' + identifier(args.run_id))
        limit = await handle.execute_update(PersampleWorkflow.set_in_flight_limit, args.limit)
        print(json.dumps(dict(workflow_id=handle.id, max_in_flight_samples=limit)))
    elif args.command == 'set-deg-limit':
        from .crosssample import CrosssampleWorkflow
        from .zoomin import ZoominWorkflow
        from ..warm_pool.state import identifier
        kind = CrosssampleWorkflow if args.stage == 'cross-sample' else ZoominWorkflow
        handle = client.get_workflow_handle(args.stage + '/' + identifier(args.run_id))
        limit = await handle.execute_update(kind.set_deg_limit, args.limit)
        print(json.dumps(dict(workflow_id=handle.id, max_in_flight_deg=limit)))
    elif args.command == 'resume-dataset':
        from .dataset import resume_dataset
        from ..warm_pool.state import identifier
        handle = await resume_dataset(client, 'dataset/' + identifier(args.run_id), args.task_queue, args.reason)
        print(json.dumps(dict(workflow_id=handle.id, run_id=handle.result_run_id)))
    elif args.command in {"resume-persample", "resume-crosssample", "resume-zoomin"}:
        from .persample import PersampleWorkflow
        from .crosssample import CrosssampleWorkflow
        from ..warm_pool.state import verified
        from ..warm_pool.state import identifier, read, status
        from ..agent import status as bridge_status
        from .zoomin import ZoominWorkflow
        prefix, run = {"resume-persample": ("persample/", PersampleWorkflow.run),
                       "resume-crosssample": ("cross-sample/", CrosssampleWorkflow.run),
                       "resume-zoomin": ("zoom-in/", ZoominWorkflow.run)}[args.command]
        identity = prefix + identifier(args.run_id)
        previous = client.get_workflow_handle(identity)
        from .dataset import UNFINISHED
        if (await previous.describe()).status.name not in UNFINISHED:
            raise ValueError("Resume requires a failed, terminated, cancelled or timed-out workflow")
        history = await previous.fetch_history()
        spec, = await client.data_converter.decode(history.events[0].workflow_execution_started_event_attributes.input.payloads)
        if read(Path(spec["output_root"]) / "spec.json") != spec:
            raise ValueError("Saved workflow specification changed")
        verified(spec["input_manifest" if args.command == "resume-persample" else "input"])
        from .dataset import request_states, superseded_sessions
        request_states(spec["pool_root"], spec["bridge_root"], {identity}, superseded_sessions(spec["output_root"]))
        handle = await client.start_workflow(run, spec, id=identity,
            task_queue=args.task_queue or history.events[0].workflow_execution_started_event_attributes.task_queue.name,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        print(handle.id)
    else:
        from ..warm_pool.state import identifier
        from .persample import PersampleWorkflow
        from .crosssample import CrosssampleWorkflow
        from .zoomin import ZoominWorkflow
        from .dataset import DatasetWorkflow
        kind = {"status": ("organize/", OrganizeWorkflow), "status-agent": ("agent/", AgentWorkflow),
                "status-persample": ("persample/", PersampleWorkflow),
                "status-crosssample": ("cross-sample/", CrosssampleWorkflow),
                "status-zoomin": ("zoom-in/", ZoominWorkflow),
                "status-dataset": ('dataset/', DatasetWorkflow)}[args.command]
        handle = client.get_workflow_handle(kind[0] + identifier(args.run_id))
        info = await handle.describe()
        stage = await handle.query(kind[1].stage) if info.status.name == "RUNNING" else None
        print(json.dumps({"workflow_id": handle.id, "status": info.status.name,
                          "stage": stage}, sort_keys=True))
