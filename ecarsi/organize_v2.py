"""Bounded Organize computation commands for Warm Pool v2."""
import argparse
from pathlib import Path

from . import layout as L
from .run_state import digest, file_identity, read_json, write_json, writer_lock
from .warm_pool.state import read, save


def prepare(input_root: Path, destination: Path):
    from .organize import find_ecapp_units, profile_unit
    from .upstream import inspect_unit

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


def execute(prepared_path: Path, reply_path: Path, output: Path):
    from .execute import execute_plan
    from .plan import _validate, validate_sample_mapping
    from .upstream import inspect_unit

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
                                          file_identity(Path(__file__).with_name("execute.py"))]))
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
    from .upstream import verify_snapshots

    output, destination = output.resolve(), destination.resolve()
    completion = read(output / "completion.json")
    if completion is None:
        raise ValueError("missing Organize completion receipt")
    with writer_lock(destination.parent / ("." + destination.name + ".publish.lock")):
        run = output / "run"
        target = destination if destination.exists() else run
        manifest = read_json(L.organize_manifest(target))
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
            manifest["units_written"] = [{**u, "dir": str(L.unit_dir(destination, u["name"]))}
                                         for u in manifest["units_written"]]
            write_json(L.organize_manifest(destination), manifest)
        save(destination / "publication.json", completion)
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
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.input_root, args.output)
    else:
        execute(args.prepared, args.reply, args.output)


if __name__ == "__main__":
    main()
