"""The release gate for a new image pair: run one fixed small dataset end to end and check what a good run has.

usage (control image; ops/run.sh passes BASE and CONTROL):
  bash ops/runpy.sh ops/gate.py start [--stress-policy=keep] [label]
                                                   start the gate dataset from ~/.config/ecarsi/gate-dataset.json,
                                                   optionally under a stress policy (decision 0017; run id ends -keep)
  VERSION=<name> bash ops/runpy.sh ops/gate.py start
                                                   the gate on a published version, beside production (decision 0019)
  bash ops/runpy.sh ops/gate.py start --bridge=<bridge root> [label]
                                                   the gate's model turns go to that bridge and its model catalog (#54)
  bash ops/runpy.sh ops/gate.py wait <run_id>      wait for it (one long Temporal call: run it in the background), then check
  bash ops/runpy.sh ops/gate.py check <run root>   the checks alone, on any finished run
  For a gate on a published version, give wait and check the same VERSION=<name> as start: the checks import that
  version's code, and another version's gate.py may lack what they need (2026-10-06: ImportError on broken_links).

gate-dataset.json is a dataset spec (examples/dataset-v2.json) without run_id, output_root and dataset_id; give it
`storage` roots apart from the production ones so gate runs stay off the main Periscope list. The checks: the
dataset completed with no failed unit; every unit is released; no step degraded (decision 0013); every zoomed
lineage has its report.html (#26); the display zone record meets its contract and the work archive exists (#25);
every link of the zone's pages and reports reaches a file of the zone (#41).
Run it right after ops/switch-images.sh; a failing gate means switching back to the previous pair.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ecarsi import task_queue, version
from ecarsi.contracts import check as contract
from ecarsi.degraded import read as degraded
from ecarsi.files import read
from ecarsi.review import from_json

BASE, CONTROL = Path(os.environ["BASE"]), os.environ["CONTROL"]  # ops/run.sh passes them
RUNS = BASE.parent / "runs" / "gate"


def start(*args):
    options = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
    if options.keys() - {"stress-policy", "bridge"}:
        raise SystemExit("options: --stress-policy=keep, --bridge=<bridge root>")
    policy, bridge = options.get("stress-policy"), options.get("bridge")
    label = next((a for a in args if not a.startswith("--")), None)
    template = json.loads((Path.home() / ".config/ecarsi/gate-dataset.json").read_text())
    published = version()  # VERSION=<name> ops/runpy.sh: the gate runs on that version's queue, named after it
    run_id = ("gate-" + time.strftime("%Y%m%d-%H%M") + (f"-{policy}" if policy else "")
              + (f"-{published['name'][:7]}" if published else "") + (f"-{Path(bridge).name[:10]}" if bridge else ""))
    spec = dict(template, run_id=run_id, output_root=str(RUNS / run_id), dataset_id=f"Gate {label or run_id}")
    if policy:
        spec["stress_policy"] = policy
    if bridge:  # another bridge, another model catalog (#54): the gate tries its models beside production
        spec["bridge_root"] = bridge
    (RUNS / "specs").mkdir(mode=0o700, parents=True, exist_ok=True)
    path = RUNS / "specs" / f"{run_id}.json"
    path.write_text(json.dumps(spec, indent=1) + "\n")
    subprocess.run([sys.executable, "-m", "ecarsi.control", "--service-root", CONTROL, "--task-queue", task_queue(),
                    "start-dataset", str(path)], check=True)
    print(f"gate started: {run_id}\n  then: bash ops/runpy.sh ops/gate.py wait {run_id}")


def problems(root):
    root, found = Path(root), []
    publication = read(root / "publication.json")
    try:
        contract("dataset", publication)
        if publication["state"] != "complete" or publication["failed_units"]:
            found.append(f"dataset {publication['state']}, failed units: {publication['failed_units']}")
    except ValueError as exc:
        found.append(f"dataset publication: {exc}")
    for unit in sorted(p for p in (root / "units").iterdir() if p.is_dir()):
        release = unit / "release"
        if (read(release / "receipt.json") or {}).get("state") != "complete":
            found.append(f"{unit.name}: not released")
        elif any(item.kind == "degraded" for item in from_json(release / "needs_review.json")):
            found.append(f"{unit.name}: needs_review lists degraded steps")
        for lineage in sorted(unit.glob("rounds/*/03-zoom-in/*/annotation_proposal.json")):
            if not (lineage.parent / "report.html").is_file():
                found.append(f"{lineage.parent.relative_to(root)}: no report.html (#26)")
    found += [f"degraded: {r.get('unit') or 'dataset'} {r.get('stage', '')}: {r['what']}: {r['error']}" for r in degraded(root)]
    spec = read(root / "spec.json") or {}
    if spec.get("storage"):
        from ecarsi.display import broken_links, zone
        place = zone(spec)
        try:
            contract("display", json.loads((Path(place["dest"]) / "display.json").read_text()))
        except (OSError, ValueError) as exc:
            found.append(f"display zone {place['dest']}: {exc} (#25)")
        else:
            found += [f"broken link on the display zone: {page} -> {target} (#41)"
                      for target, page in sorted(broken_links(Path(place["dest"]), place["record"]["name"]).items())]
        if not Path(place["record"]["work"]).is_file():
            found.append(f"no work archive {place['record']['work']} (#25)")
    return found


def report(root):
    found = problems(root)
    print(("GATE FAILED" if found else "GATE PASSED") + f": {root}")
    for problem in found:
        print("  -", problem)
    return 1 if found else 0


async def wait(run_id):
    from temporalio.client import Client
    from ecarsi.control.temporal import endpoint
    client = await Client.connect(endpoint(CONTROL)["endpoint"])
    try:
        await client.get_workflow_handle("dataset/" + run_id).result()
    except Exception as exc:  # the run failed: the checks still say where
        print(f"dataset/{run_id} did not complete: {type(exc).__name__}: {exc}")
    return report(RUNS / run_id)


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or ["help"]
    if command == "start":
        start(*rest)
        code = 0
    elif command == "wait":
        code = asyncio.run(wait(rest[0]))
    elif command == "check":
        code = report(rest[0])
    else:
        print(__doc__)
        code = 2
    sys.stdout.flush()
    os._exit(code)  # the temporal client can segfault at interpreter teardown
