#!/bin/bash
# One step of the eca-rsi#13 pluggability test: is the control plane replaceable at both ends?
#
# Point B at a DISPOSABLE plane -- its own run directory, Temporal port, PostgreSQL and pool.
# The test SIGKILLs the database, so it must never be aimed at a plane holding real history.
#   B=/path/to/test-run PY=/path/to/python bash container/pluggability-test.sh snapshot before
#
# Every step is: observe, do the violence, observe
# again -- and write both observations down, because the claim under test ("a running pool task is
# not disturbed, and a new coordinator re-attaches rather than resubmits") is only falsifiable
# against what the pool held before.
#
#   pluggability-test.sh snapshot <label>            record pool/workflow state
#   pluggability-test.sh kill-coordinators           SIGKILL the control workers only (leader dies, workers live)
#   pluggability-test.sh kill-plane                  SIGKILL Temporal, PostgreSQL and the coordinators
#   pluggability-test.sh start-coordinators | start-plane
#   pluggability-test.sh compare <before> <after>    what changed: resubmissions, publication contents, delay
set -u
: "${B:?run directory of the plane under test -- never a production run}"
: "${PY:=python3}"                      # any interpreter that can read JSON; no kernels needed
OBS=$B/observations; mkdir -p "$OBS"

snapshot() {
    local label=$1 out=$OBS/$1.json
    "$PY" - "$B" "$out" <<'PYEOF'
import hashlib, json, os, sys, time
from pathlib import Path
base, out = Path(sys.argv[1]), Path(sys.argv[2])
pool = base / "pool" / "requests"
requests = {}
for entry in sorted(os.scandir(pool), key=lambda e: e.name) if pool.is_dir() else []:
    folder = Path(entry.path)
    spec = json.loads((folder / "request.json").read_text()) if (folder / "request.json").is_file() else None
    if spec is None:
        continue
    attempt = folder / spec["attempt_id"]
    receipt = attempt / "receipt.json"
    requests[entry.name] = {
        "attempt_id": spec["attempt_id"],
        # attempt count is the resubmission evidence: replay must adopt, not recompute
        "attempts": sorted(p.name for p in folder.iterdir() if p.is_dir()),
        "state": json.loads(receipt.read_text())["state"] if receipt.is_file() else "unfinished",
        "submitted_at": spec["submitted_at"],
    }
publications = {}
for path in sorted((base / "datasets").rglob("publication.json")) if (base / "datasets").is_dir() else []:
    publications[str(path.relative_to(base))] = hashlib.sha256(path.read_bytes()).hexdigest()
alive = []   # /proc, not ps: the pool interpreter runs inside the image, which has no ps
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        continue
    if str(base) in line and "step.sh" not in line:
        alive.append(f"{entry.name} {line[:110]}")
json.dump({"at": time.time(), "requests": requests, "publications": publications,
           "processes": sorted(alive)}, open(out, "w"), indent=1, sort_keys=True)
print(f"{out.name}: {len(requests)} requests, {len(publications)} publications, {len(alive)} processes")
PYEOF
}

pids() { ps -eo pid,args | grep "$1" | grep "$B" | grep -v grep | awk '{print $1}'; }

case ${1:?usage: step.sh <command> ...} in
snapshot) snapshot "${2:?label}" ;;
kill-coordinators)
    for p in $(pids '[e]carsi.control '); do echo "SIGKILL coordinator $p"; kill -9 "$p"; done ;;
kill-plane)
    for p in $(pids '[e]carsi.control '); do echo "SIGKILL coordinator $p"; kill -9 "$p"; done
    for p in $(pids '[e]carsi.control.temporal'); do echo "SIGKILL temporal $p"; kill -9 "$p"; done
    for p in $(pids '[t]emporal-server'); do echo "SIGKILL temporal-server $p"; kill -9 "$p"; done
    for p in $(pids '[p]ostgres'); do echo "SIGKILL postgres $p"; kill -9 "$p"; done ;;
start-coordinators) "${PLANE:-$B/control-plane.sh}" start coordinators ;;
start-plane) P=${PLANE:-$B/control-plane.sh}; "$P" start temporal; sleep 20; "$P" start coordinators ;;
compare)
    "$PY" - "$OBS/${2:?before}.json" "$OBS/${3:?after}.json" <<'PYEOF'
import json, sys, time
before, after = (json.load(open(p)) for p in sys.argv[1:3])
print(f"elapsed {after['at'] - before['at']:.0f} s")
resubmitted = [k for k, v in after["requests"].items()
               if k in before["requests"] and v["attempts"] != before["requests"][k]["attempts"]]
print("resubmitted:", resubmitted or "none")
regressed = [k for k, v in before["requests"].items()
             if v["state"] != "unfinished" and after["requests"].get(k, {}).get("state") != v["state"]]
print("finished requests that changed state:", regressed or "none")
changed = [k for k, v in before["publications"].items() if after["publications"].get(k) != v]
print("publications whose content changed:", changed or "none")
print("new requests:", len(set(after["requests"]) - set(before["requests"])))
print("new publications:", sorted(set(after["publications"]) - set(before["publications"])))
PYEOF
    ;;
*) echo "unknown command: $1"; exit 2 ;;
esac
