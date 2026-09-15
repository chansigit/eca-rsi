"""Temporal Work Coordinator: durable Organize handoffs through Pool and Bridge."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
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
    from .warm_pool.state import file_digest, status
    state = status(root, request_id)
    if state["state"] == "succeeded":
        receipt = state["receipt"]
        selected = next((item for item in receipt["outputs"] if Path(item["path"]).name == output), None)
        if selected is None or file_digest(selected["path"]) != selected["sha256"]:
            raise ValueError("Pool receipt output changed or missing")
        return {"state": "ready", "path": selected["path"],
                "attempt_id": state["attempt_id"]}
    if state["state"] in {"failed", "cancelled", "unknown_external_result"}:
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
    if result["state"] in {"failed", "unknown_external_result"}:
        return {"state": result["state"], "detail": result.get("reason")}
    return {"state": "waiting"}


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
    if action == "decision":
        reply = read(args[0])["response"]
        return {"kind": reply["kind"], "calls": len(reply["calls"])}
    if action == "finish":
        spec = session.verified(args[0])["spec"]
        if session.turn_reply(args[0], args[1])["kind"] != "final":
            raise ValueError("Cannot finish an agent with pending tools")
        return session.immutable(Path(spec["output_root"]) / "result.json",
                                 {"session": args[0], "reply": session.reference(args[1])})["path"]
    operations = {"create": session.create_session, "model": session.submit_turn,
                  "tool": session.tool_request, "resume": session.continuation,
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
                await workflow.sleep(2)
            reply = result["path"]
            decision = await call(agent_step, "decision", [reply])
            if decision["kind"] == "final":
                self._stage = "complete"
                return await call(agent_step, "finish", [session, reply])
            if decision["kind"] != "tools" or not 1 <= decision["calls"] <= 16:
                raise ApplicationError("Invalid agent tool boundary", non_retryable=True)
            self._stage = "tools"
            accepted, parents = [], []
            # Ordered tools are the conservative default; never infer independence from a model batch.
            for index in range(decision["calls"]):
                item = await call(agent_step, "tool", [session, reply, index, parents[-1] if parents else None])
                while True:
                    result = await call(check_pool, spec["pool_root"], item["request_id"], item["result_file"])
                    if result["state"] == "ready":
                        break
                    if result["state"] != "waiting":
                        raise ApplicationError(f"{item['request_id']}: {result['state']}", non_retryable=True)
                    await workflow.sleep(2)
                accepted.append({**item, "path": result["path"]})
                parents.append(item["request_id"])
            context = await call(agent_step, "resume", [session, reply, accepted])
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


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporal", required=True, help="explicit Temporal Service host:port")
    parser.add_argument("--task-queue", default=QUEUE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker")
    p = commands.add_parser("start")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-agent")
    p.add_argument("spec", type=Path)
    p = commands.add_parser("start-persample")
    p.add_argument("spec", type=Path)
    for name in ("status", "status-agent", "status-persample", "resume-persample"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
    args = parser.parse_args()
    client = await Client.connect(args.temporal)
    if args.command == "worker":
        from .persample_workflow import PersampleWorkflow, SampleWorkflow, sample_step
        with ThreadPoolExecutor(max_workers=16) as executor:
            async with Worker(client, task_queue=args.task_queue, workflows=[OrganizeWorkflow, AgentWorkflow, PersampleWorkflow, SampleWorkflow],
                              activities=ACTIVITIES + [agent_step, sample_step], activity_executor=executor,
                              max_concurrent_activities=16):
                await asyncio.Future()
    elif args.command in {"start", "start-agent", "start-persample"}:
        if args.command == "start-agent":
            from .agent_session import validate_spec as validate_agent
            spec = validate_agent(json.loads(args.spec.read_text()))
            run, identity = AgentWorkflow.run, "agent/" + spec["session_id"]
        elif args.command == "start-persample":
            from .persample_workflow import PersampleWorkflow, validate_spec as validate_samples
            spec = validate_samples(json.loads(args.spec.read_text()))
            run, identity = PersampleWorkflow.run, "persample/" + spec["run_id"]
        else:
            spec = validate_spec(json.loads(args.spec.read_text()))
            run, identity = OrganizeWorkflow.run, "organize/" + spec["run_id"]
        handle = await client.start_workflow(run, spec,
                      id=identity, task_queue=args.task_queue,
                      id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        print(handle.id)
    elif args.command == "resume-persample":
        from .persample_workflow import PersampleWorkflow
        from .agent_session import verified
        from .warm_pool.state import identifier, read, status
        from .agent_bridge import status as bridge_status
        identity = "persample/" + identifier(args.run_id)
        previous = client.get_workflow_handle(identity)
        if (await previous.describe()).status.name != "FAILED":
            raise ValueError("Resume requires a failed per-sample workflow")
        history = await previous.fetch_history()
        spec, = await client.data_converter.decode(history.events[0].workflow_execution_started_event_attributes.input.payloads)
        if read(Path(spec["output_root"]) / "spec.json") != spec:
            raise ValueError("Saved per-sample specification changed")
        verified(spec["input_manifest"])
        for root, inspect, allowed in ((spec["pool_root"], status, {"queued", "running", "succeeded"}),
                (spec["bridge_root"], bridge_status, {"queued", "running", "reply_saved"})):
            for path in (Path(root) / "requests").glob("*/request.json"):
                if read(path)["spec"].get("trace", {}).get("workflow_id") == identity:
                    state = inspect(root, path.parent.name)["state"]
                    if state not in allowed:
                        raise ValueError(f"Reconcile {path.parent.name} ({state}) before resume")
        handle = await client.start_workflow(PersampleWorkflow.run, spec, id=identity,
            task_queue=args.task_queue, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        print(handle.id)
    else:
        from .warm_pool.state import identifier
        from .persample_workflow import PersampleWorkflow
        kind = {"status": ("organize/", OrganizeWorkflow), "status-agent": ("agent/", AgentWorkflow),
                "status-persample": ("persample/", PersampleWorkflow)}[args.command]
        handle = client.get_workflow_handle(kind[0] + identifier(args.run_id))
        info = await handle.describe()
        stage = await handle.query(kind[1].stage) if info.status.name == "RUNNING" else None
        print(json.dumps({"workflow_id": handle.id, "status": info.status.name,
                          "stage": stage}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
