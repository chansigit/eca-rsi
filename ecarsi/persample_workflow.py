"""Temporal per-sample fan-out with bounded preparation and durable agent children."""
import asyncio
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


def validate_spec(spec, *, resume=False):
    from .agent_session import reference
    from .warm_pool.state import identifier, pool_root, read
    from .agent_bridge import root_path
    required = {"run_id", "dataset_id", "unit", "output_root", "pool_root", "bridge_root",
                "partition_budget", "compute_budget", "tool_budget", "finalize_budget", "config",
                "batch_size", "max_in_flight_samples", "max_batch_bytes"}
    if not isinstance(spec, dict) or set(spec) - {'depends_on'} != required:
        raise ValueError("Per-sample needs explicit input, services, compute and backlog budgets")
    identifier(spec["run_id"])
    if 'depends_on' in spec:
        from .warm_pool.state import validate_trace
        validate_trace(dict(workflow_id='persample/' + spec['run_id'], dataset_id=spec['dataset_id'],
                            unit_id='persample.partition', depends_on=spec['depends_on']))
    if len(spec["run_id"]) > 60 or not isinstance(spec["dataset_id"], str) or not spec["dataset_id"].strip():
        raise ValueError("Use a run ID up to 60 characters and a dataset label")
    unit, output = Path(spec["unit"]), Path(spec["output_root"])
    if not unit.is_absolute() or not output.is_absolute() or (output.exists() and not resume) or output.is_relative_to(unit):
        raise ValueError("Use an existing absolute input unit and fresh output outside it")
    metadata = read(unit / "input/manifest.json")
    if not metadata or not metadata.get("sample_mapping"):
        raise ValueError("Organize must confirm sample mapping before per-sample")
    from .run_state import file_identity
    publication = read(unit.parent.parent / "publication.json", {})
    published = next((u for u in publication.get("units", []) if u["name"] == unit.name), None)
    if (unit.parent.name != "units" or not published or
            published["manifest"] != file_identity(unit / "input/manifest.json")):
        raise ValueError("Per-sample input must be an accepted Organize publication")
    pool_root(spec["pool_root"])
    root_path(spec["bridge_root"])
    for key in ("partition_budget", "compute_budget", "tool_budget", "finalize_budget"):
        budget = spec[key]
        if set(budget) != {"cpus", "memory_mb", "timeout_seconds"} or any(type(v) is not int or v < 1 for v in budget.values()):
            raise ValueError("Resource budgets must explicitly specify positive CPU, MiB and timeout")
    for key in ("batch_size", "max_in_flight_samples", "max_batch_bytes"):
        if type(spec[key]) is not int or spec[key] < 1:
            raise ValueError(key + " must be positive")
    if spec["batch_size"] > spec["max_in_flight_samples"]:
        raise ValueError("Batch size exceeds the in-flight sample limit")
    cfg = spec["config"]
    required_config = {"scrublet", "decontx", "resolution", "tissue"}
    if (not required_config <= set(cfg) or set(cfg) - required_config - {"compute_backend", "gpu_min_cells", "gpu_memory_mb"}
            or any(type(cfg[k]) is not bool for k in ("scrublet", "decontx"))):
        raise ValueError("Explicit Scrublet, DecontX, resolution and tissue settings are required")
    if cfg.get("compute_backend", "cpu") not in {"cpu", "rapids", "auto"}:
        raise ValueError("compute_backend must be cpu, rapids or auto")
    if cfg.get("compute_backend", "cpu") != "cpu":
        if any(type(cfg.get(k)) is not int or cfg[k] <= 0 for k in ("gpu_min_cells", "gpu_memory_mb")):
            raise ValueError("GPU execution needs explicit positive gpu_min_cells and gpu_memory_mb")
    import math
    if isinstance(cfg["resolution"], bool) or not isinstance(cfg["resolution"], (int, float)) or not math.isfinite(cfg["resolution"]) or cfg["resolution"] <= 0:
        raise ValueError("Resolution must be finite and positive")
    if not isinstance(cfg["tissue"], str) or not cfg["tissue"].strip():
        raise ValueError("Tissue context must be a nonempty string")
    return {**spec, "input_manifest": reference(unit / "input/manifest.json"),
            "config": {**cfg, "annotate": True, "species": metadata["species"], "language": "English", "model": None, "effort": None}}


@activity.defn
def sample_step(action, args):
    from .agent_session import immutable, reference, verified
    from .warm_pool.state import submit, digest, read, status, save, lock
    from .persample_v2 import annotation_spec
    if action == "read":
        return read(args[0])
    if action == "agent":
        spec, path, parent = args
        return annotation_spec(spec, reference(path), parent)
    if action == "accepted_annotation":
        result = read(args[0])
        if "output" not in result or verified(result["output"]).get("accepted") is not True:
            raise ValueError("Agent did not submit an accepted annotation")
        session = verified(result["session"])["spec"]
        if status(session["pool_root"], result["pool_request_id"])["state"] != "succeeded":
            raise ValueError("Annotation validation worker is no longer accepted")
        return {"path": result["output"]["path"], "parent": result["pool_request_id"]}
    if action == "recoverable":
        from .agent_bridge import status as bridge_status
        spec, sample = args
        found = False
        for root, inspect, allowed in (
            (spec["pool_root"], status, {"succeeded"}),
            (spec["bridge_root"], bridge_status, {"reply_saved", "queued", "running"}),
        ):
            for path in (Path(root) / "requests").glob("*/request.json"):
                trace = read(path)["spec"].get("trace", {})
                if trace.get("workflow_id") == "persample/" + spec["run_id"] and trace.get("sample_id") == sample:
                    found = True
                    if inspect(root, path.parent.name)["state"] not in allowed:
                        return False
        return found
    if action == "publish":
        spec, results, failed, totals = args
        results = sorted(results, key=lambda p: read(p)["sample"])
        records = [verified(reference(path)) for path in results]
        if len({r["sample"] for r in records}) != len(records):
            raise ValueError("Sample completed twice")
        n_input = sum(r["validation"]["n_input"] for r in records)
        n_kept = sum(r["validation"]["n_survived"] for r in records)
        n_removed = sum(r["validation"]["n_removed"] for r in records)
        if not failed and (len(records) != totals["total_samples"] or
                           n_input + totals["n_excluded"] != totals["n_input"] or n_kept + n_removed != n_input):
            raise ValueError("Per-sample sample/cell conservation failed")
        publication = {
            "state": "incomplete" if failed else "complete", "input": spec["input_manifest"],
            "samples": [reference(p) for p in results], "failed_samples": failed,
            "n_input": totals["n_input"], "n_survived": n_kept,
            "n_removed": n_removed + totals["n_excluded"], "partition_exclusions": totals["exclusions"]}
        root = Path(spec["output_root"])
        with lock(root / "publication.lock"):
            previous = read(root / "publication.json")
            if previous and previous != publication:
                if previous["state"] == "complete":
                    raise ValueError("Cannot replace a completed publication with different results")
                immutable(root / ("publication-" + digest(previous) + ".json"), previous)
            immutable(root / ("publication-" + digest(publication) + ".json"), publication)
            save(root / "publication.json", publication)
        return str(root / "publication.json")
    spec = args[0]
    root = Path(spec["output_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    trace = {"workflow_id": "persample/" + spec["run_id"], "dataset_id": spec["dataset_id"]}
    accelerator = {}
    if action == "partition":
        offset, parent = args[1:]
        reference_spec = immutable(root / "spec.json", spec)
        request_id = spec["run_id"] + ".partition-" + str(offset)
        command = ["partition", reference_spec["path"], str(offset)]
        inputs, budget, output, unit = [reference_spec, spec["input_manifest"]], spec["partition_budget"], "partition.json", "persample.partition"
        parents = [parent] if parent else spec.get('depends_on', [])
    elif action == "compute":
        entry, parent = args[1:]
        trace["sample_id"] = entry["sample_id"]
        request_id = spec["run_id"] + ".compute-" + digest(entry["sample_id"])[:20]
        command = ["compute", entry["bundle"]["path"]]
        inputs, budget, output, unit = [entry["bundle"]], spec["compute_budget"], "computed.json", "osp.compute"
        cfg = spec["config"]
        backend = cfg.get("compute_backend", "cpu")
        if backend == "rapids" or backend == "auto" and entry["n_cells"] >= cfg["gpu_min_cells"]:
            accelerator = {"gpu": {"mode": "required" if backend == "rapids" else "preferred",
                                   "memory_mb": cfg["gpu_memory_mb"]}}
        parents = [parent]
    elif action == "finalize":
        computed, annotation, parent = args[1:]
        data = read(computed)
        trace["sample_id"] = data["sample"]
        request_id = spec["run_id"] + ".finalize-" + digest(data["sample"])[:20]
        command = ["finalize", computed, annotation or "none"]
        inputs = [reference(computed)] + ([reference(annotation)] if annotation else [])
        budget, output, unit = spec["finalize_budget"], "final.json", "osp.finalize"
        parents = [parent]
    else:
        raise ValueError("Unknown per-sample activity")
    submit(spec["pool_root"], {"request_id": request_id, "operation_id": unit,
        "trace": {**trace, "unit_id": unit, "depends_on": parents},
        "args": ["-m", "ecarsi.persample_v2", *command], **budget, **accelerator,
        "inputs": inputs + [reference(Path(__file__).with_name("persample_v2.py"))], "outputs": [output]})
    return {"id": request_id, "output": output}


async def call(fn, *args):
    return await workflow.execute_activity(fn, args=args, start_to_close_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_attempts=3))


async def await_pool(spec, request):
    from .work_coordinator import check_pool
    while True:
        result = await call(check_pool, spec["pool_root"], request["id"], request["output"])
        if result["state"] == "ready":
            return result["path"]
        if result["state"] != "waiting":
            raise ApplicationError(f"{request['id']}: {result['state']}: {result.get('detail')}", non_retryable=True)
        await workflow.sleep(2)


@workflow.defn
class SampleWorkflow:
    @workflow.run
    async def run(self, spec, entry, parent):
        from .work_coordinator import AgentWorkflow
        request = await call(sample_step, "compute", [spec, entry, parent])
        computed = await await_pool(spec, request)
        bundle = await call(sample_step, "read", [computed])
        annotation, parent = None, request["id"]
        if not bundle["empty"]:
            session = await call(sample_step, "agent", [spec, computed, request["id"]])
            result = await workflow.execute_child_workflow(AgentWorkflow.run, session,
                id=workflow.info().workflow_id + "/annotate")
            accepted = await call(sample_step, "accepted_annotation", [result])
            annotation, parent = accepted["path"], accepted["parent"]
        request = await call(sample_step, "finalize", [spec, computed, annotation, parent])
        return await await_pool(spec, request)


@workflow.defn
class PersampleWorkflow:
    @workflow.query
    def stage(self):
        return getattr(self, "_stage", "created")

    @workflow.run
    async def run(self, spec):
        offset, total, pending, completed, failed, parent = 0, None, {}, [], [], None
        totals, inputs, replayed = None, {}, set()
        while True:
            recover = bool(failed) and workflow.patched("persample-recovered-receipts-v1")
            if recover:
                for failure in list(failed):
                    if len(pending) >= spec["max_in_flight_samples"]:
                        break
                    sample = failure["sample"]
                    if sample in replayed or not await call(sample_step, "recoverable", [spec, sample]):
                        continue
                    entry, predecessor, identity = inputs[sample]
                    handle = await workflow.start_child_workflow(SampleWorkflow.run,
                        args=[spec, entry, predecessor], id=identity + "/recovered")
                    pending[handle] = sample
                    replayed.add(sample)
                    failed.remove(failure)
            if total is not None and offset >= total and not pending:
                break
            if (total is None or offset < total) and len(pending) <= spec["max_in_flight_samples"] - spec["batch_size"]:
                self._stage = "partitioning"
                request = await call(sample_step, "partition", [spec, offset, parent])
                path = await await_pool(spec, request)
                batch = await call(sample_step, "read", [path])
                totals = totals or batch
                total, offset, parent = batch["total_samples"], batch["next_offset"], request["id"]
                for index, entry in enumerate(batch["entries"]):
                    # IDs depend on immutable sample order, not worker placement or completion order.
                    identity = workflow.info().workflow_id + "/sample-" + str(offset - len(batch["entries"]) + index)
                    handle = await workflow.start_child_workflow(SampleWorkflow.run, args=[spec, entry, parent], id=identity)
                    pending[handle] = entry["sample_id"]
                    inputs[entry["sample_id"]] = (entry, parent, identity)
                if not pending and offset < total:
                    raise ApplicationError("Partition made no progress", non_retryable=True)
                continue
            self._stage = "processing samples"
            done, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED,
                timeout=30 if recover and any(f["sample"] not in replayed for f in failed) else None)
            for handle in sorted(done, key=lambda h: pending[h]):
                sample = pending.pop(handle)
                try:
                    completed.append(await handle)
                except Exception as exc:
                    failed.append({"sample": sample, "error": str(exc)})
        self._stage = "publishing"
        output = await call(sample_step, "publish", [spec, completed, failed, totals])
        if failed:
            raise ApplicationError(f"{len(failed)} samples failed; completed siblings retained at {output}", non_retryable=True)
        self._stage = "complete"
        return output
