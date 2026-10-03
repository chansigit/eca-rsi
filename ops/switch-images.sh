#!/bin/bash
# Switch the plane to another image pair: guard (no running executions), stop worker supervisors and the plane,
# re-register the pool runtime from inside the new science image, repoint deployment.env, start, re-add workers.
# Settings: ~/.config/ecarsi/deployment.env; this script rewrites its IMG and SCIENCE_IMG lines.
# usage: switch-images.sh <stamp> <science sha256> <host:job> ...
# A worker with a CPU slice (the plane node) is re-added by hand: warm_pool add-worker <host> --job-id <id> --cpus ... --memory-mb ...
set -e -o pipefail
OPS=$(cd "$(dirname "$0")" && pwd)
STAMP=$1; SHA=$2; shift 2
ENVFILE=$HOME/.config/ecarsi/deployment.env
set -a; . "$ENVFILE"; set +a
CTL=$(dirname "$IMG")/rsi-control-$STAMP.sif; SCI=$(dirname "$SCIENCE_IMG")/rsi-science-$STAMP.sif
[ -f "$CTL" ] && [ -f "$SCI" ] || { echo "missing images for $STAMP"; exit 2; }
n=$(bash $OPS/run.sh control $OPS/count-wf.py 2>&1 | grep -oE 'running executions: [0-9]+'); echo "$n"; [ "$n" = "running executions: 0" ]
echo "== stop worker supervisors"
for d in $POOL/worker-state/*/; do
  host=$(basename $d); host=${host%-*}; pid=$(python3 -c "import json;print(json.load(open('$d/worker.json')).get('pid',''))" 2>/dev/null || true)
  [ -n "$pid" ] || continue
  ssh -o BatchMode=yes -o ConnectTimeout=8 $host "ps -p $pid -o args= | grep -q 'ecarsi.warm_pool' && kill -TERM $pid && echo '$host stopped' || true" 2>/dev/null || true
done
sleep 20
echo "== stop plane"; cd /tmp; bash $OPS/control-plane.sh stop >/dev/null 2>&1 || true
pgrep -u $USER -f "rsi-services/temporal/temporal-server" >/dev/null && { echo "temporal still running"; exit 1; } || true
echo "== runtime"
python3 - "$POOL" "$BASE" "$SCI" "$SHA" <<'PY'
import json, sys
pool, base, sci, sha = sys.argv[1:5]
r = json.load(open(pool + "/config.json"))["runtime"]
r["image"] = {"path": sci, "sha256": sha}
json.dump(r, open(base + "/pool-runtime-current.json", "w"), indent=1)
PY
apptainer exec --cleanenv --bind "$BINDS" --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python \
  "$SCI" /usr/local/bin/python3.12 -m ecarsi.warm_pool --root $POOL configure-runtime $BASE/pool-runtime-current.json 2>&1 | grep runtime_digest
echo "== repoint deployment.env"
sed -i -E "s#rsi-(control|science)-[0-9]{8}-[0-9]+\.sif#rsi-\1-$STAMP.sif#g" "$ENVFILE"
grep -ho "rsi-[a-z]*-[0-9-]*\.sif" "$ENVFILE" | sort -u | tr '\n' ' '; echo
echo "== start"; bash $OPS/control-plane.sh start temporal hq scheduler bridge runners coordinators fleet-status pruner 2>&1 | grep -E "eca-rsi:|running|stopped"
bash $OPS/restart-periscope.sh
echo "== workers"
HC=$BASE/image-code
for hj in "$@"; do h=${hj%%:*}; j=${hj##*:}
  ( PYTHONPATH=$HC timeout 420 "$HOSTPY" -m ecarsi.warm_pool --root $POOL add-worker $h --job-id $j --wait-seconds 300 2>&1 | grep -E '"cpus"' | tr -d '\n' | sed "s/^/$h /"; echo ) &
done; wait   # timeout: the ssh in add-worker can hang on a worker that keeps the channel open (#20)
apptainer exec "$CTL" /opt/rsi-bin/hq --server-dir $POOL/hq worker list 2>/dev/null | grep RUNNING | cut -c1-110 || echo "no worker running yet"
echo "SWITCH DONE $STAMP"
