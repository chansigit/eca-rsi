#!/bin/bash
# One look at the plane: Slurm jobs with end times, HQ server and workers, scheduler state, node productivity,
# pool failures of the last ~70 min, coordinator memory. Run it on the control-plane node. usage: bash ops/node-check.sh
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
OPS=$(cd "$(dirname "$0")" && pwd)
HQ="$(python3 -c "import json;print(json.load(open('$POOL/config.json'))['hq'])") --server-dir $POOL/hq"
date +%T
echo "=== squeue"; squeue --me -o "%.10i %.16j %.8T %.10M %.11l %.20e %R"
echo "=== hq server"; tail -3 "$POOL/hq-server.log" | cut -c1-200
echo "=== hq workers (id host modelcall)"; $HQ worker list 2>/dev/null | awk -F'|' 'NR>3 && $3 ~ /RUNNING/ {gsub(/ /,"",$2); gsub(/ /,"",$4); mc=($5 ~ /modelcall/)?"modelcall":"-"; print $2, $4, mc}'
echo "=== scheduler"; python3 -c "import json,time; s=json.load(open('$POOL/scheduler.json')); print(s.get('state'), round(time.time()-s['observed_at']),'s ago', s.get('error','')[:200], s.get('release'))"
echo "=== productivity"; bash "$OPS/runpy.sh" -m ecarsi.observatory productivity --root "$BASE" --pool-root "$POOL" 2>&1 | tail -14
echo "=== failures"; bash "$OPS/runpy.sh" "$OPS/fail-recent.py" 2>&1 | tail -8
echo "=== coordinators RSS (MB)"; ps -u "$USER" -o pid,rss,args | awk '/ecarsi.control/ && /worker/ && !/awk/ && !/apptainer/ {printf "%s %d MB\n", $1, $2/1024}'
