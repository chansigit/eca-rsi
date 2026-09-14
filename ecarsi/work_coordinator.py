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
def submit_execute(spec: dict, prepared_path: str, reply_path: str) -> str:
    from .warm_pool.state import file_digest, submit
    request_id = spec["run_id"] + ".execute"
    submit(spec["pool_root"], dict(request_id=request_id, operation_id="organize.execute",
        trace=task_trace(spec, "organize.execute"),
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
        self._stage = "executing"
        execute_id = await call(submit_execute, spec, prepared_path, reply_path)
        completion = await await_pool(execute_id, "completion.json")
        self._stage = "publishing"
        return await call(accept_organize, completion, spec["output_root"])


ACTIVITIES = [submit_prepare, submit_plan, submit_execute, check_pool, check_bridge, accept_organize]


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
    p = commands.add_parser("status")
    p.add_argument("run_id")
    args = parser.parse_args()
    client = await Client.connect(args.temporal)
    if args.command == "worker":
        with ThreadPoolExecutor(max_workers=16) as executor:
            async with Worker(client, task_queue=args.task_queue, workflows=[OrganizeWorkflow],
                              activities=ACTIVITIES, activity_executor=executor,
                              max_concurrent_activities=16):
                await asyncio.Future()
    elif args.command == "start":
        spec = validate_spec(json.loads(args.spec.read_text()))
        handle = await client.start_workflow(OrganizeWorkflow.run, spec,
                      id="organize/" + spec["run_id"], task_queue=args.task_queue,
                      id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        print(handle.id)
    else:
        from .warm_pool.state import identifier
        handle = client.get_workflow_handle("organize/" + identifier(args.run_id))
        info = await handle.describe()
        stage = await handle.query(OrganizeWorkflow.stage) if info.status.name == "RUNNING" else None
        print(json.dumps({"workflow_id": handle.id, "status": info.status.name,
                          "stage": stage}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
