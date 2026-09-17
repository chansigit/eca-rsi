"""Temporal Work Coordinator: durable Organize handoffs through Pool and Bridge."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

QUEUE = "ecarsi-organize-v2"
SHORT = timedelta(seconds=30)
RETRY = RetryPolicy(maximum_attempts=3)


def task_trace(spec, unit_id):
    return {"workflow_id": "organize/" + spec["run_id"],
            "dataset_id": spec.get("dataset_id", spec["run_id"]), "unit_id": unit_id}


@activity.defn
def submit_prepare(spec: dict) -> str:
    from .warm_pool.state import submit
    request_id = spec["run_id"] + ".prepare"
    submit(spec["pool_root"], dict(request_id=request_id, operation_id="organize.prepare",
        trace=task_trace(spec, "organize.prepare"),
        args=["-m", "ecarsi.organize_v2", "prepare", spec["input_root"], "prepared.json"],
        cpus=spec["prepare_cpus"], memory_mb=spec["prepare_memory_mb"],
        timeout_seconds=spec["prepare_timeout_seconds"], inputs=[], outputs=["prepared.json"]))
    return request_id


@activity.defn
def submit_plan(spec: dict, prepared_path: str) -> str:
    from .agent_bridge import submit
    from .warm_pool.state import read
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
    from .warm_pool.state import file_digest, submit
    request_id = spec["run_id"] + ".execute"
    trace = task_trace(spec, "organize.execute")
    if plan_parent:
        trace["depends_on"] = [plan_parent]
    submit(spec["pool_root"], dict(request_id=request_id, operation_id="organize.execute",
        trace=trace,
        args=["-m", "ecarsi.organize_v2", "execute", prepared_path, reply_path, "."],
        cpus=spec["execute_cpus"], memory_mb=spec["execute_memory_mb"],
        timeout_seconds=spec["execute_timeout_seconds"],
        inputs=[{"path": path, "sha256": file_digest(path)} for path in (prepared_path, reply_path)],
        outputs=["completion.json", "run/organize/manifest.json"]))
    return request_id


@activity.defn
def check_pool(root: str, request_id: str, output: str) -> dict:
    from .warm_pool.state import file_digest, read, retry, status
    state = status(root, request_id)
    if state["state"] == "succeeded":
        receipt = state["receipt"]
        selected = next((item for item in receipt["outputs"] if Path(item["path"]).name == output), None)
        if selected is None or file_digest(selected["path"]) != selected["sha256"]:
            raise ValueError("Pool receipt output changed or missing")
        return {"state": "ready", "path": selected["path"],
                "attempt_id": state["attempt_id"]}
    if state["state"] == "failed" and state["receipt"].get("retryable") is True:
        request = read(Path(root) / "requests" / request_id / "request.json")
        if request.get("retry_count", 0) < 2:
            if str(state["receipt"].get("error", "")).startswith("MemoryError"):
                retry(root, request_id, memory_mb=2 * request["spec"]["memory_mb"],
                      reason="Automatic retry at twice the budget after the RSS watchdog")
            else:
                retry(root, request_id, reason="Automatic recovery after a confirmed local interruption")
            return {"state": "waiting"}
    if state["state"] == "unknown_external_result":
        return {"state": "waiting", "detail": "unknown_external_result"}
    if state["state"] in {"failed", "cancelled"}:
        return {"state": state["state"], "detail": (state["receipt"] or {}).get("error")}
    return {"state": "waiting"}


@activity.defn
def check_bridge(root: str, request_id: str) -> dict:
    from .agent_bridge import root_path, status
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
    from .organize_v2 import publish
    return publish(Path(output).parent, Path(destination))


@activity.defn
def organize_agent_step(action: str, args: list):
    from .organize_v2 import planning_spec, accepted_plan
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
                    start_to_close_timeout=SHORT, retry_policy=RETRY)
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
    from . import agent_session as session
    from .warm_pool.state import read
    if action == "cached_completion":
        reference = args[0]
        spec = session.verified(reference)["spec"]
        path = Path(spec["output_root"]) / "result.json"
        result = read(path)
        if not result or "output" not in result:
            return None
        if result["session"] != reference:
            raise ValueError("Completed agent result belongs to another session")
        from .warm_pool.state import status
        state = status(spec["pool_root"], result["pool_request_id"])
        if state["state"] != "succeeded" or result["output"] not in [
                {k: item[k] for k in ("path", "sha256")} for item in state["receipt"]["outputs"]]:
            raise ValueError("Completed agent result is no longer accepted")
        if session.verified(result["output"]).get("accepted") is not True:
            raise ValueError("Completed agent submission is not accepted")
        return str(path)
    if action == "decision":
        reply = read(args[0])["response"]
        return {"kind": reply["kind"], "calls": len(reply["calls"])}
    if action == 'parallel':
        from .agent_parallel import choose
        return choose(*args)
    if action == "finish":
        spec = session.verified(args[0])["spec"]
        if session.turn_reply(args[0], args[1])["kind"] != "final":
            raise ValueError("Cannot finish an agent with pending tools")
        return session.immutable(Path(spec["output_root"]) / "result.json",
                                 {"session": args[0], "reply": session.reference(args[1])})["path"]
    if action == "tool":
        from jsonschema import ValidationError
        from .agent_tool_errors import reject_arguments
        try:
            return session.tool_request(*args)
        except (ValidationError, session.ToolRejection) as exc:
            # A model can correct its request; never run an invalid command, and
            # never let a malformed model call fail the whole dataset.
            return reject_arguments(*args, message=getattr(exc, "message", None) or str(exc))
    operations = {"create": session.create_session, "model": session.submit_turn,
                  "resume": session.continuation,
                  "complete_tool": session.complete_tool}
    return operations[action](*args)


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
                start_to_close_timeout=SHORT, retry_policy=RETRY)

        session = await call(agent_step, "create", [spec])
        if workflow.patched("agent-reuse-completed-submission-v1"):
            completed = await call(agent_step, "cached_completion", [session])
            if completed:
                self._stage = "complete"
                return completed
        context = None
        parents = []
        for turn in range(spec["max_turns"]):
            self._stage = "model"
            request = await call(agent_step, "model", [session, turn, context, parents])
            while True:
                result = await call(check_bridge, spec["bridge_root"], request)
                if result["state"] == "ready":
                    break
                if result["state"] != "waiting":
                    raise ApplicationError(f"{request}: {result['state']}", non_retryable=True)
                await workflow.sleep(result.get('poll_seconds', 2)
                    if workflow.patched('agent-queued-poll-backoff-v1') else 2)
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


def validate_spec(spec):
    from .warm_pool.state import identifier, pool_root
    from .agent_bridge import root_path
    required = {
        "run_id", "input_root", "output_root", "pool_root", "bridge_root",
        "prepare_cpus", "prepare_memory_mb", "prepare_timeout_seconds",
        "execute_cpus", "execute_memory_mb", "execute_timeout_seconds"}
    if not isinstance(spec, dict) or not required <= spec.keys() or spec.keys() - required - {"dataset_id"}:
        raise ValueError("Organize Workflow requires explicit input, output, services and resource budgets")
    identifier(spec["run_id"])
    if "dataset_id" in spec:
        from .warm_pool.state import validate_trace
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


async def run_worker(client, task_queue, workflow_slots=None):
    from .persample_workflow import PersampleWorkflow, SampleWorkflow, sample_step
    from .crosssample_workflow import CrosssampleWorkflow, crosssample_step
    from .zoomin_workflow import ZoominWorkflow, zoomin_step
    from .dataset_workflow import DatasetWorkflow, AnalysisUnitWorkflow, dataset_step
    if workflow_slots is None:
        # One slot also constrains long-poll admission: idle coordinators left
        # sticky continuations waiting almost 10s. Two keeps admission moving
        # while still bounding GIL contention during large cold history replays.
        workflow_slots = 2
    if workflow_slots < 1:
        raise ValueError("Workflow slots must be positive")
    # The SDK default permits 500 concurrent replays; cold recovery must fit this host.
    with ThreadPoolExecutor(max_workers=16) as executor:
        async with Worker(client, task_queue=task_queue, max_concurrent_workflow_tasks=workflow_slots,
                workflows=[OrganizeWorkflow, AgentWorkflow, PersampleWorkflow, SampleWorkflow, CrosssampleWorkflow, ZoominWorkflow, DatasetWorkflow, AnalysisUnitWorkflow],
                activities=ACTIVITIES + [agent_step, sample_step, crosssample_step, zoomin_step, dataset_step],
                activity_executor=executor, max_concurrent_activities=16):
            await asyncio.Future()


async def follow_service(root, task_queue, workflow_slots=None):
    """Reconnect after a service handoff without cancelling Pool or Bridge work."""
    from .temporal_service import endpoint
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
        running = asyncio.create_task(run_worker(client, task_queue, workflow_slots))
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
    parser.add_argument("--task-queue", default=QUEUE)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("worker")
    p.add_argument("--workflow-slots", type=int, help="concurrent workflow activations; default 2 to keep polling responsive and bound Python history replay; activities and Pool tasks remain concurrent")
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
    if args.command == 'worker' and args.service_root:
        await follow_service(args.service_root, args.task_queue, args.workflow_slots)
        return
    if args.service_root:
        from .temporal_service import endpoint
        args.temporal = endpoint(args.service_root)['endpoint']
    runtime = None
    if args.command != 'worker':
        from temporalio.runtime import Runtime, TelemetryConfig
        # Client-only commands have no worker to report. Starting the optional
        # SDK heartbeat thread can race native teardown in short-lived clients.
        runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=None)
    client = await Client.connect(args.temporal, runtime=runtime)
    if args.command == "worker":
        await run_worker(client, args.task_queue, args.workflow_slots)
    elif args.command in {"start", "start-agent", "start-persample", "start-crosssample", "start-zoomin", "start-dataset"}:
        if args.command == "start-dataset":
            from .dataset_workflow import DatasetWorkflow, validate_spec as validate_dataset
            spec = validate_dataset(json.loads(args.spec.read_text()))
            run, identity = DatasetWorkflow.run, 'dataset/' + spec['run_id']
        elif args.command == "start-agent":
            from .agent_session import validate_spec as validate_agent
            spec = validate_agent(json.loads(args.spec.read_text()))
            run, identity = AgentWorkflow.run, "agent/" + spec["session_id"]
        elif args.command == "start-persample":
            from .persample_workflow import PersampleWorkflow, validate_spec as validate_samples
            spec = validate_samples(json.loads(args.spec.read_text()))
            run, identity = PersampleWorkflow.run, "persample/" + spec["run_id"]
        elif args.command == "start-zoomin":
            from .zoomin_workflow import ZoominWorkflow, validate_spec as validate_zoomin
            spec = validate_zoomin(json.loads(args.spec.read_text()))
            run, identity = ZoominWorkflow.run, "zoom-in/" + spec["run_id"]
        elif args.command == "start-crosssample":
            from .crosssample_workflow import CrosssampleWorkflow, validate_spec as validate_crosssample
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
        from .persample_workflow import PersampleWorkflow
        from .warm_pool.state import identifier
        handle = client.get_workflow_handle('persample/' + identifier(args.run_id))
        limit = await handle.execute_update(PersampleWorkflow.set_in_flight_limit, args.limit)
        print(json.dumps(dict(workflow_id=handle.id, max_in_flight_samples=limit)))
    elif args.command == 'set-deg-limit':
        from .crosssample_workflow import CrosssampleWorkflow
        from .zoomin_workflow import ZoominWorkflow
        from .warm_pool.state import identifier
        kind = CrosssampleWorkflow if args.stage == 'cross-sample' else ZoominWorkflow
        handle = client.get_workflow_handle(args.stage + '/' + identifier(args.run_id))
        limit = await handle.execute_update(kind.set_deg_limit, args.limit)
        print(json.dumps(dict(workflow_id=handle.id, max_in_flight_deg=limit)))
    elif args.command == 'resume-dataset':
        from .dataset_workflow import resume_dataset
        from .warm_pool.state import identifier
        handle = await resume_dataset(client, 'dataset/' + identifier(args.run_id), args.task_queue, args.reason)
        print(json.dumps(dict(workflow_id=handle.id, run_id=handle.result_run_id)))
    elif args.command in {"resume-persample", "resume-crosssample", "resume-zoomin"}:
        from .persample_workflow import PersampleWorkflow
        from .crosssample_workflow import CrosssampleWorkflow
        from .agent_session import verified
        from .warm_pool.state import identifier, read, status
        from .agent_bridge import status as bridge_status
        from .zoomin_workflow import ZoominWorkflow
        prefix, run = {"resume-persample": ("persample/", PersampleWorkflow.run),
                       "resume-crosssample": ("cross-sample/", CrosssampleWorkflow.run),
                       "resume-zoomin": ("zoom-in/", ZoominWorkflow.run)}[args.command]
        identity = prefix + identifier(args.run_id)
        previous = client.get_workflow_handle(identity)
        if (await previous.describe()).status.name != "FAILED":
            raise ValueError("Resume requires a failed workflow")
        history = await previous.fetch_history()
        spec, = await client.data_converter.decode(history.events[0].workflow_execution_started_event_attributes.input.payloads)
        if read(Path(spec["output_root"]) / "spec.json") != spec:
            raise ValueError("Saved workflow specification changed")
        verified(spec["input_manifest" if args.command == "resume-persample" else "input"])
        for root, inspect, allowed in ((spec["pool_root"], status, {"queued", "running", "succeeded"}),
                (spec["bridge_root"], bridge_status, {"queued", "running", "reply_saved"})):
            for path in (Path(root) / "requests").glob("*/request.json"):
                if read(path)["spec"].get("trace", {}).get("workflow_id") == identity:
                    state = inspect(root, path.parent.name)["state"]
                    if state not in allowed:
                        raise ValueError(f"Reconcile {path.parent.name} ({state}) before resume")
        handle = await client.start_workflow(run, spec, id=identity,
            task_queue=args.task_queue, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        print(handle.id)
    else:
        from .warm_pool.state import identifier
        from .persample_workflow import PersampleWorkflow
        from .crosssample_workflow import CrosssampleWorkflow
        from .zoomin_workflow import ZoominWorkflow
        from .dataset_workflow import DatasetWorkflow
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


if __name__ == "__main__":
    asyncio.run(main())
