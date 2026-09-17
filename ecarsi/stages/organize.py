"""Bounded Organize computation commands for Warm Pool v2."""
import argparse
import json
from pathlib import Path

from .. import layout as L
from ..run_state import digest, file_identity, read_json, write_json, writer_lock
from ..warm_pool.state import read, save


def prepare(input_root: Path, destination: Path):
    from ..organize import find_ecapp_units, profile_unit
    from ..upstream import inspect_unit

    input_root = input_root.resolve(strict=True)
    units, violations = find_ecapp_units(input_root)
    if violations:
        raise ValueError(f"undeclared H5AD: {violations[0]}")
    records = [inspect_unit(unit) for unit in units]
    accepted = [record for record in records if record["state"] == "accepted"]
    if len(accepted) != len(records) or not accepted:
        raise ValueError("every source must have accepted ECA-PP results")
    profiles = [profile_unit(record) for record in accepted]
    prepared = {"schema_version": 1, "input_root": str(input_root), "records": records,
                "profiles": profiles, "source_identity": digest(records)}
    save(destination, prepared)
    return prepared


def planning_spec(spec, prepared_path):
    """The immutable agent policy names worker programs, never a Bridge-side harness."""
    from ..warm_pool.state import reference
    from ..plan import PLAN_SCHEMA
    prepared = read(prepared_path)
    if prepared is None or Path(prepared["input_root"]).resolve() != Path(spec["input_root"]).resolve():
        raise ValueError("prepared input identity does not match this dataset")
    package = Path(__file__).parents[1]
    sources = [p["name"] for p in prepared["profiles"]]
    tools = []
    for name, parameters, description in (
        ("inspect_source", {"source": {"type": "string", "enum": sources},
            "column": {"type": ["string", "null"]}, "offset": {"type": "integer", "minimum": 0}},
         "Read a source metadata profile on a worker. Use column=null, offset=0 for its profile. "
         "For an existing obs column, return up to 100 value counts starting at offset; no expression reads."),
        ("submit_plan", {"plan_json": {"type": "string"}},
         "Validate and submit the complete plan on a worker. Correct any returned error and resubmit. "
         "plan_json is JSON matching this schema: " + json.dumps(PLAN_SCHEMA))):
        tools.append(dict(name=name, description=description,
            parameters={"type": "object", "properties": parameters,
                        "required": list(parameters), "additionalProperties": False},
            args=["-m", "ecarsi.stages.organize", "plan-tool", name, str(prepared_path), "{arguments}", "result.json"],
            cpus=spec["prepare_cpus"], memory_mb=spec["prepare_memory_mb"],
            timeout_seconds=spec["prepare_timeout_seconds"],
            inputs=[reference(prepared_path), *[reference(package / p) for p in (
                "stages/organize.py", "plan.py", "execute.py", "upstream.py", "downstream.py", "design.py")]],
            outputs=["result.json"], result_file="result.json"))
    prompt = (package / "prompts/plan.md").read_text() + "\n\n" + (package / "prompts/organize_v2.md").read_text()
    prompt += ("\n\n## Worker tools\nYou have no local filesystem or code execution. "
               "Call inspect_source for each source before deciding. Request additional column value counts "
               "only if needed; pagination is explicit. Use submit_plan for the structured plan instead of "
               "returning it as prose. A rejected plan includes a concrete error: correct it and resubmit. "
               "An accepted plan ends this planning session automatically. Never bypass the sample mapping "
               "or cell-conservation checks.\nSources: " + json.dumps(sources))
    return dict(session_id="org-" + digest(spec["run_id"])[:24],
        dataset_id=spec.get("dataset_id", spec["run_id"]), prompt=prompt, tools=tools,
        pool_root=spec["pool_root"], bridge_root=spec["bridge_root"],
        output_root=spec["output_root"] + ".planning", max_turns=30, completion_tool="submit_plan",
        trace={"workflow_id": "organize/" + spec["run_id"], "dataset_id": spec.get("dataset_id", spec["run_id"]),
               "unit_id": "organize.plan", "depends_on": [spec["run_id"] + ".prepare"]})


def plan_tool(name, prepared_path, arguments_path, destination):
    """Metadata reads and scientific plan validation run only inside a Pool grant."""
    from ..upstream import inspect_unit, normalize
    from ..plan import PLAN_SCHEMA, _validate, validate_sample_mapping
    from ..execute import _conservation_audit, _experiment_audit
    from jsonschema import validate, ValidationError
    prepared, arguments = read(prepared_path), read(arguments_path)
    current = [inspect_unit(record) for record in prepared["records"]]
    if digest(current) != prepared["source_identity"]:
        raise ValueError("ECA-PP inputs changed after Organize preparation")
    try:
        if name == "inspect_source":
            profile = next(p for p in prepared["profiles"] if p["name"] == arguments["source"])
            column = arguments["column"]
            if column is None:
                result = {k: profile[k] for k in ("name", "species", "n_obs", "n_vars", "obs_columns")}
            else:
                if column not in profile["obs_columns"]:
                    raise ValueError("Column is not present in this source")
                from ..design import _obs
                values = normalize(_obs(profile["h5ad"])[column])
                counts = values.value_counts()
                offset = arguments["offset"]
                result = {"source": profile["name"], "column": column, "offset": offset,
                          "total_values": len(counts), "n_na": int(values.isna().sum()),
                          "value_counts": {str(k): int(v) for k, v in counts.iloc[offset:offset + 100].items()}}
        elif name == "submit_plan":
            plan = json.loads(arguments["plan_json"])
            validate(plan, PLAN_SCHEMA)
            _validate(plan, prepared["profiles"])
            validate_sample_mapping(plan, prepared["profiles"])
            units = {r["name"]: r for r in current}
            conservation = _conservation_audit(units, plan)
            experiments = _experiment_audit(units, plan)
            result = {"accepted": True, "response": {"plan": plan},
                      "source_identity": prepared["source_identity"],
                      "conservation": conservation, "experiments": experiments}
        else:
            raise ValueError("Unknown Organize worker tool")
    except (ValueError, ValidationError) as exc:
        result = {"accepted": False, "error": str(exc)[:4000], "instruction": "Correct the arguments and retry."}
    if len(json.dumps(result).encode()) > 262144:
        raise ValueError("Metadata result exceeds the agent handoff limit")
    save(destination, result)
    return result


def accepted_plan(spec, session_result, prepared_path):
    from ..warm_pool.state import verified
    from ..warm_pool.state import status
    result = read(session_result)
    if not result or "output" not in result:
        raise ValueError("Agent ended without an accepted submit_plan")
    session = verified(result["session"])["spec"]
    if session != planning_spec(spec, prepared_path):
        raise ValueError("Plan session does not belong to this preparation")
    receipt = status(spec["pool_root"], result["pool_request_id"])
    output = verified(result["output"])
    if (receipt["state"] != "succeeded" or output.get("accepted") is not True
            or output.get("source_identity") != read(prepared_path)["source_identity"]
            or result["output"] not in [{k: o[k] for k in ("path", "sha256")} for o in receipt["receipt"]["outputs"]]):
        raise ValueError("Plan has no matching accepted worker receipt")
    return {"path": result["output"]["path"], "request_id": result["pool_request_id"]}


def execute(prepared_path: Path, reply_path: Path, output: Path):
    from ..execute import execute_plan
    from ..plan import _validate, validate_sample_mapping
    from ..upstream import inspect_unit

    prepared = read(prepared_path)
    reply = read(reply_path)
    plan = reply.get("response", {}).get("plan") if reply else None
    if prepared is None or plan is None:
        raise ValueError("verified preparation and saved Bridge reply are required")
    _validate(plan, prepared["profiles"])
    validate_sample_mapping(plan, prepared["profiles"])
    # Recheck source identities at the compute boundary; model waits can be long.
    current = [inspect_unit(record) for record in prepared["records"]]
    if digest(current) != prepared["source_identity"]:
        raise ValueError("ECA-PP inputs changed after Organize preparation")
    run = output / "run"
    execute_plan(current, prepared["profiles"], plan, run,
                 records=current, input_identity=prepared["source_identity"],
                 adapter_identity=digest([file_identity(Path(__file__)),
                                          file_identity(Path(__file__).parents[1] / "execute.py")]))
    manifest = read_json(L.organize_manifest(run))
    if manifest["state"] != "complete" or not manifest.get("experiment_audit"):
        raise ValueError("Organize did not confirm experiment mapping")
    completion = {"input_identity": prepared["source_identity"],
                  "plan_digest": digest(plan), "manifest": file_identity(L.organize_manifest(run)),
                  "units": [{"name": unit["name"], "input": unit["identity"],
                             "manifest": unit["manifest_identity"]} for unit in manifest["units_written"]]}
    save(output / "completion.json", completion)
    return completion


def publish(output: Path, destination: Path):
    """Coordinator-side acceptance after a successful Pool receipt."""
    from ..upstream import verify_snapshots

    output, destination = output.resolve(), destination.resolve()
    completion = read(output / "completion.json")
    if completion is None:
        raise ValueError("missing Organize completion receipt")
    with writer_lock(destination.parent / ("." + destination.name + ".publish.lock")):
        run = output / "run"
        target = destination if destination.exists() else run
        manifest = read_json(L.organize_manifest(target))
        snapshot = L.organize_manifest(target).with_name("worker-manifest.json")
        if not snapshot.exists():
            if file_identity(L.organize_manifest(target)) != completion["manifest"]:
                raise ValueError("Organize worker manifest changed before publication")
            write_json(snapshot, manifest)
        if file_identity(snapshot) != completion["manifest"]:
            raise ValueError("Organize worker manifest snapshot changed")
        original = read_json(snapshot)
        relocated = {**original, "units_written": [
            {**u, "dir": str(L.unit_dir(destination, u["name"]))} for u in original["units_written"]]}
        if manifest not in (original, relocated):
            raise ValueError("Organize published manifest changed")
        if (manifest["state"] != "complete" or manifest["input_identity"] != completion["input_identity"]
                or digest(manifest["plan"]) != completion["plan_digest"]
                or len(manifest["units_written"]) != len(completion["units"])):
            raise ValueError("Organize result does not match its completion receipt")
        for item in completion["units"]:
            unit = L.unit_dir(target, item["name"])
            if (file_identity(L.input_h5ad(unit)) != item["input"]
                    or file_identity(L.input_manifest(unit)) != item["manifest"]):
                raise ValueError(f"Organize output changed: {item['name']}")
            meta = read_json(L.input_manifest(unit))
            verify_snapshots(L.input_manifest(unit).parent, meta)
            if "sample_mapping" not in meta:
                raise ValueError("confirmed experiment mapping is missing")
            mapping = L.input_manifest(unit).parent / meta["sample_mapping"]["path"]
            if file_identity(mapping) != meta["sample_mapping"]["identity"]:
                raise ValueError("confirmed experiment mapping changed")
        if target == run:
            if destination.exists():
                raise ValueError("Organize output destination already exists")
            run.rename(destination)
        if manifest != relocated:
            write_json(L.organize_manifest(destination), relocated)
        # The Pool receipt still names this small worker output after the unit
        # directory moves. Preserve it for later receipt verification and replay.
        if not L.organize_manifest(run).exists():
            write_json(L.organize_manifest(run), original)
        if file_identity(L.organize_manifest(run)) != completion["manifest"]:
            raise ValueError("Organize Pool manifest output changed")
        publication = {**completion, "worker_manifest": completion["manifest"],
                       "manifest": file_identity(L.organize_manifest(destination))}
        previous = read(destination / "publication.json")
        if previous is not None and previous != publication:
            raise ValueError("Organize publication changed")
        save(destination / "publication.json", publication)
    return str(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("input_root", type=Path)
    p.add_argument("output", type=Path)
    p = commands.add_parser("execute")
    p.add_argument("prepared", type=Path)
    p.add_argument("reply", type=Path)
    p.add_argument("output", type=Path)
    p = commands.add_parser("plan-tool")
    p.add_argument("name", choices=["inspect_source", "submit_plan"])
    p.add_argument("prepared", type=Path)
    p.add_argument("arguments", type=Path)
    p.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.input_root, args.output)
    elif args.operation == "plan-tool":
        plan_tool(args.name, args.prepared, args.arguments, args.output)
    else:
        execute(args.prepared, args.reply, args.output)


if __name__ == "__main__":
    main()
