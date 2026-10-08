"""Published versions (decision 0019): a version's pool requests run its own code on the image's workers, its
bridge turns go to its own adapter and runners, and code that is not a published version behaves as before."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ecarsi
import ecarsi.agent as bridge
import ecarsi.agent.session as session
from ecarsi.agent.dispatch import model_key, runner_key
from ecarsi.files import digest, read, save
from ecarsi.warm_pool import backend
from ecarsi.warm_pool.state import retry, submit

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_the_version_json_beside_the_packages(tmp_path):
    assert ecarsi.version() is None and ecarsi.task_queue() == "ecarsi-durable-v2"   # a checkout
    (tmp_path / "ecarsi").mkdir()
    shutil.copy(ROOT / "ecarsi" / "__init__.py", tmp_path / "ecarsi")
    probe = [sys.executable, "-c", "import ecarsi, json; print(json.dumps([ecarsi.version(), ecarsi.task_queue()]))"]
    run = lambda: json.loads(subprocess.run(probe, env={"PYTHONPATH": str(tmp_path), **{k: v for k, v in os.environ.items() if k == "LD_LIBRARY_PATH"}}, cwd=tmp_path,
                                            capture_output=True, text=True, check=True).stdout)
    assert run() == [None, "ecarsi-durable-v2"]
    save(tmp_path / "version.json", dict(name="0123456789ab", commit="0123456789ab" + "0" * 28))
    version, queue = run()
    assert version["name"] == "0123456789ab" and version["root"] == str(tmp_path) and queue == "ecarsi-0123456789ab"


def pool(tmp_path, pythonpath):
    root = tmp_path / "pool"
    root.mkdir(mode=0o700)
    (root / "requests").mkdir()
    save(root / "config.json", dict(runtime=dict(command=[sys.executable], files={}, version="test", pythonpath=pythonpath)))
    return root


def request(root, name):
    return read(root / "requests" / name / "request.json")


def spec(name, inputs=()):
    return dict(request_id=name, operation_id="compute", args=["-c", "pass"], cpus=1, memory_mb=64,
                timeout_seconds=10, outputs=["result.json"], inputs=list(inputs))


def test_a_version_runs_its_own_code_on_the_workers_of_its_image(tmp_path):
    image = str(tmp_path / "image-code")
    root = pool(tmp_path, [image])
    image_runtime = read(root / "config.json")["runtime"]
    v1 = dict(name="v1", root=str(tmp_path / "versions" / "v1"))
    submit(root, spec("versioned"), version=v1)
    submit(root, spec("plain"))   # the caller's own version: this checkout is none
    versioned, plain = request(root, "versioned"), request(root, "plain")
    assert versioned["runtime"]["pythonpath"] == [v1["root"], image]
    assert versioned["runtime_digest"] == digest(versioned["runtime"]) and versioned["version"] == "v1"
    assert versioned["placement"] == digest(image_runtime) == plain["runtime_digest"]
    assert "placement" not in plain and "version" not in plain and plain["runtime"] == image_runtime
    # HQ places both on the workers that declare the image runtime
    for r in (versioned, plain):
        assert "runtime/" + digest(image_runtime) + "=1" in backend.hq_resources(r["spec"], r, dict(model_call_resource=False))

    # a stage file pinned from another version would run that version's code
    other = tmp_path / "versions" / "v0" / "stage.py"
    other.parent.mkdir(parents=True)
    other.write_text("")
    with pytest.raises(ValueError, match="another version"):
        submit(root, spec("mixed", [dict(path=str(other), sha256="0" * 64)]), version=v1)

    # a retry keeps the version; on the current runtime it puts the version first on the new image's path
    folder = root / "requests" / "versioned"
    def failed():
        r = request(root, "versioned")
        save(folder / r["attempt_id"] / "receipt.json", dict(state="failed", finished_at=time.time(), outputs=[],
             attempt_id=r["attempt_id"], request_digest=r["digest"], runtime_digest=r["runtime_digest"]))
    failed()
    retry(root, "versioned", reason="test")
    assert request(root, "versioned")["runtime"] == versioned["runtime"]
    assert request(root, "versioned")["placement"] == versioned["placement"]
    new_image = str(tmp_path / "new-image-code")
    save(root / "config.json", dict(runtime=dict(image_runtime, pythonpath=[new_image])))
    failed()
    retry(root, "versioned", reason="test", use_current_runtime=True)
    again = request(root, "versioned")
    assert again["runtime"]["pythonpath"] == [v1["root"], new_image]
    assert again["placement"] == digest(read(root / "config.json")["runtime"]) and again["version"] == "v1"


def test_a_versions_turns_go_to_its_adapter_and_runners_only(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    model = dict(harness="openai@vllm", model="resident", url="http://127.0.0.1:9/v1")
    save(catalog, dict(models=[model]))
    root_pool = pool(tmp_path, [str(tmp_path)])
    root = bridge.init(tmp_path / "bridge", catalog, concurrency=4, pool_root=root_pool)
    save(root / "config.json", dict(read(root / "config.json"), service=dict(models="all", stale_seconds=60)))
    v1 = dict(name="v1", root=str(tmp_path / "versions" / "v1"))
    monkeypatch.setattr(ecarsi, "version", lambda: v1)   # this code is version v1 from here on
    tool = dict(name="compute", description="Compute on a worker", args=["-c", "pass", "{arguments}"],
                parameters=dict(type="object", properties=dict(value=dict(type="integer")), required=["value"],
                                additionalProperties=False),
                cpus=1, memory_mb=64, timeout_seconds=30, inputs=[], outputs=["result.json"], result_file="result.json")
    def turn(name):
        ref = session.create_session(dict(session_id=name, dataset_id="data", prompt="Answer.", max_turns=2,
                                          pool_root=str(root_pool), bridge_root=str(root),
                                          output_root=str(tmp_path / name), tools=[tool]))
        number = session.submit_turn(ref, 0)
        assert read(root / "requests" / number / "request.json")["version"] == v1
        return number
    beat = lambda key: save(root / "runners" / (key + ".json"), dict(pid=1, observed_at=time.time(), draining=False))
    (root / "runners").mkdir()

    # only the image's runner is alive: a v1 turn is a pool task on v1's code, never that runner's
    beat(model_key(model))
    first = turn("first")
    bridge.serve(root, once=True)
    attempt = bridge.status(root, first)["attempts"][0]
    plan = read(attempt["plan"]["path"])
    assert "portable_adapter" not in plan
    assert plan["adapter_sha256"] == read(root / "requests" / first / "request.json")["adapter_sha256"]
    pooled = request(root_pool, attempt["pool_request_id"])
    assert pooled["version"] == "v1" and pooled["runtime"]["pythonpath"][0] == v1["root"]

    # v1's own runner takes v1's turns
    beat(runner_key(model, v1))
    second = turn("second")
    bridge.serve(root, once=True)
    attempt = bridge.status(root, second)["attempts"][0]
    assert attempt["execution"] == "service" and attempt["runner"] == "v1." + model_key(model)
    assert (root / "runner-queue" / ("v1." + model_key(model)) / (attempt["turn_id"] + ".json")).is_file()


# An execution stays on its version because Temporal keeps activities, children and continue-as-new on the queue
# of the workflow that schedules them; workflow code never names a queue (see the next test).
from datetime import timedelta  # noqa: E402

from temporalio import activity, workflow  # noqa: E402


@activity.defn(name="version_where")
def where() -> str:
    return activity.info().task_queue


@workflow.defn(name="VersionChild")
class Child:
    @workflow.run
    async def run(self) -> str:
        return workflow.info().task_queue


@workflow.defn(name="VersionParent")
class Parent:
    @workflow.run
    async def run(self, rounds: int, seen: list) -> list:
        seen = seen + [workflow.info().task_queue,
                       await workflow.execute_activity("version_where", start_to_close_timeout=timedelta(seconds=30)),
                       await workflow.execute_child_workflow("VersionChild", id=f"{workflow.info().workflow_id}-{rounds}")]
        if rounds:
            workflow.continue_as_new(args=[rounds - 1, seen])
        return seen


def test_an_execution_stays_on_the_queue_it_was_started_on():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker
    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            with ThreadPoolExecutor(4) as threads:
                serve = lambda queue: Worker(env.client, task_queue=queue, workflows=[Parent, Child], activities=[where],
                                             activity_executor=threads, workflow_runner=UnsandboxedWorkflowRunner())
                async with serve("ecarsi-a"), serve("ecarsi-b"):
                    return await env.client.execute_workflow(Parent.run, args=[2, []], id="parent", task_queue="ecarsi-a")
    assert asyncio.run(main()) == ["ecarsi-a"] * 9


def test_workflow_code_never_names_a_task_queue():
    """Only the client side picks a queue -- starting a run (coordinator main), resuming one and serving one (the
    worker) -- and `common.start_child`, which passes the queue the `before_child` activity answered (0022). A queue
    named anywhere else inside a workflow would move work without that check."""
    import ast
    allowed = {("coordinator.py", "main"), ("coordinator.py", "run_worker"), ("dataset.py", "resume_dataset"),
               ("common.py", "start_child"),  # 0022: the one place a child is sent to the current version's queue
               ("coordinator.py", "before_child")}  # ... and the activity that names it (a describe_task_queue call)
    found = set()
    for path in sorted((ROOT / "ecarsi" / "control").glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    if isinstance(node, ast.keyword) and node.arg == "task_queue":
                        found.add((path.name, function.name))
    assert found <= allowed, found - allowed


SANDBOX_PROBE = """
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import _Definition
import ecarsi.control.coordinator as c
from ecarsi.control import crosssample, dataset, persample, zoomin
import asyncio, ecarsi
async def main():  # what a Worker does with each workflow class when it starts
    runner = SandboxedWorkflowRunner()
    for cls in (c.OrganizeWorkflow, c.AgentWorkflow, dataset.DatasetWorkflow, dataset.AnalysisUnitWorkflow,
                persample.PersampleWorkflow, crosssample.CrosssampleWorkflow, zoomin.ZoominWorkflow):
        runner.prepare_workflow(_Definition.must_from_class(cls))
asyncio.run(main())
print(ecarsi.task_queue())
"""


def test_the_workflows_load_in_temporals_sandbox_from_a_checkout_and_from_a_version(tmp_path):
    """Production coordinators re-import the workflow modules in Temporal's sandbox, which forbids file access at
    import time (a module-level ecarsi.task_queue() did that); the workflow tests run unsandboxed and miss it."""
    probe = lambda path, cwd: subprocess.run([sys.executable, "-c", SANDBOX_PROBE], cwd=cwd, capture_output=True, text=True,
                                             env={"PYTHONPATH": os.pathsep.join([str(path), *sys.path[1:]]), **{k: v for k, v in os.environ.items() if k == "LD_LIBRARY_PATH"}})
    checkout = probe(ROOT, ROOT)
    assert checkout.returncode == 0 and checkout.stdout.split()[-1] == "ecarsi-durable-v2", checkout.stderr[-2000:]
    shutil.copytree(ROOT / "ecarsi", tmp_path / "ecarsi", ignore=shutil.ignore_patterns("__pycache__"))
    save(tmp_path / "version.json", dict(name="0123456789ab", commit="0" * 40))
    published = probe(tmp_path, tmp_path)
    assert published.returncode == 0 and published.stdout.split()[-1] == "ecarsi-0123456789ab", published.stderr[-2000:]


def test_the_control_page_names_the_version_of_each_running_dataset():
    from ecarsi.ui.control import running
    fleet = {"workflows": {
        "dataset/a": dict(kind="DatasetWorkflow", status="RUNNING", dataset_id="A", task_queue="ecarsi-0123456789ab"),
        "dataset/b": dict(kind="DatasetWorkflow", status="RUNNING", task_queue="ecarsi-durable-v2"),
        "dataset/c": dict(kind="DatasetWorkflow", status="COMPLETED", task_queue="ecarsi-0123456789ab"),
        "unit/a": dict(kind="AnalysisUnitWorkflow", status="RUNNING", task_queue="ecarsi-0123456789ab")}}
    assert running(fleet) == (["A", "dataset/b"], {"A": "0123456789ab"})
    assert running({}) == ([], {})


def test_a_version_writes_only_shared_file_versions_the_shared_components_know():
    from ecarsi.contracts import KINDS, unknown_to
    assert unknown_to(list(KINDS)) == [] and unknown_to([]) == []   # version 1 needs nothing from the reader
    newer = dict(KINDS, **{"pool-request/2": {}, "stage/2": {}})
    assert unknown_to(list(KINDS), newer) == ["pool-request/2"]     # stage files are the version's own
    assert unknown_to([*KINDS, "pool-request/2"], newer) == []



def test_a_version_runs_in_the_image_it_was_published_with(tmp_path, monkeypatch):
    """Several compute images side by side (decision 0019): configure-runtime registers each image's runtime, a
    version's requests go to the workers of the image in its version.json, and the scheduler holds a request back
    while no live worker declares that image's runtime."""
    import math
    from ecarsi.warm_pool import __main__ as cli
    from ecarsi.warm_pool.backend import infeasible
    from ecarsi.warm_pool.provision import worker_command
    from ecarsi.warm_pool.state import image_runtime
    root = pool(tmp_path, ["/opt/eca-rsi"])
    old = dict(read(root / "config.json")["runtime"], image=dict(path="/images/old.sif", sha256="0" * 64))
    new = dict(old, image=dict(path="/images/new.sif", sha256="1" * 64))
    save(root / "config.json", dict(runtime=old))
    monkeypatch.setattr(cli, "check_runtime", lambda *a, **k: None)   # it runs inside the image in production
    cli.configure_runtime(root, new, register_only=True)
    config = read(root / "config.json")
    assert config["runtime"] == old and image_runtime(config, "/images/new.sif") == new
    version = lambda name, image: dict(name=name, root=str(tmp_path / "versions" / name), science_image=image)
    submit(root, spec("on-new"), version=version("v2", "/images/new.sif"))
    submit(root, spec("on-old"), version=version("v1", "/images/old.sif"))
    assert request(root, "on-new")["placement"] == digest(new) and request(root, "on-old")["placement"] == digest(old)
    assert request(root, "on-new")["runtime"]["image"] == new["image"]
    with pytest.raises(ValueError, match="no runtime registered"):
        submit(root, spec("lost"), version=version("v0", "/images/gone.sif"))
    cli.configure_runtime(root, new)    # the new image becomes current; the old one stays registered
    config = read(root / "config.json")
    assert config["runtime"] == new and image_runtime(config, "/images/old.sif") == old

    old_worker, any_worker = (8, 30000.0, 0, math.inf, {digest(old)}), (8, 30000.0, 0, math.inf, set())
    task = dict(cpus=1, memory_mb=64)
    assert infeasible(task, [old_worker], digest(new)) == "no live worker of the image runtime " + digest(new)[:12]
    assert infeasible(task, [old_worker], digest(old)) is None and infeasible(task, [any_worker], digest(new)) is None
    profile = dict(job_id="1", host="node", gpu_ids=[])
    command = worker_command(root, profile, ["python"], tmp_path, [0], 100, False, digest(new))
    assert command[command.index("--runtime") + 1] == digest(new)
    assert "--runtime" not in worker_command(root, profile, ["python"], tmp_path, [0], 100, False)


def test_current_queue_never_moves_backwards(tmp_path, monkeypatch):
    """A gate runs on a candidate newer than current and must stay there (0022)."""
    import json
    import ecarsi

    versions = tmp_path / "versions"
    for name, published in (("aaa", "2026-10-08T10:00:00-0700"), ("bbb", "2026-10-08T12:00:00-0700")):
        (versions / name).mkdir(parents=True)
        (versions / name / "version.json").write_text(json.dumps(dict(name=name, published=published)))
    mine = lambda name: dict(json.loads((versions / name / "version.json").read_text()), root=str(versions / name))
    (versions / "current").symlink_to("aaa")
    monkeypatch.setattr(ecarsi, "version", lambda: mine("bbb"))
    assert ecarsi.current_queue() is None          # current is older than this version
    monkeypatch.setattr(ecarsi, "version", lambda: mine("aaa"))
    assert ecarsi.current_queue() is None          # current is this version
    (versions / "current").unlink()
    (versions / "current").symlink_to("bbb")
    assert ecarsi.current_queue() == "ecarsi-bbb"  # current is newer
    monkeypatch.setattr(ecarsi, "version", lambda: None)
    assert ecarsi.current_queue() is None
