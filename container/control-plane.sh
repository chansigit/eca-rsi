#!/bin/bash
# Start/stop the v2 control plane on THIS host. The deployed copy lives in the run directory and
# exports the paths below; everything is idempotent, logs under $BASE/control-logs, and state lives
# on shared storage, so run it again on a fresh node after an allocation expires.
#   control-plane.sh start|stop|restart|status [temporal|scheduler|bridge|coordinators|observatory|keeper ...]
#   control-plane.sh report [--sessions HOURS] [--json]     # text status of pool, bridge, workers, datasets
set -u
: "${BASE:?run directory holding the pool, bridge and control state}"
: "${IMG:?control image (.sif)}"
CODE=${CODE:-$(cd "$(dirname "$0")/.." && pwd)}       # checkout that PYTHONPATH points at
CONTROL=${CONTROL:-$BASE/durable-control}; POOL=${POOL:-$BASE/pool}; BRIDGE=${BRIDGE:-$BASE/bridge}
LOGS=$BASE/control-logs; mkdir -p "$LOGS"
COORDINATORS=${COORDINATORS:-4}; TASK_QUEUE=${TASK_QUEUE:-ecarsi-durable-v2}
STAGE_LIMIT_FLOORS=${STAGE_LIMIT_FLOORS:-}                # e.g. '{"max_in_flight_deg": 12, "max_in_flight_lineages": 6}'
BINDS=${BINDS:-/scratch,/oak,/home,/lscratch}
HOST_IP=$(hostname -I | awk '{print $1}')
HOSTPY=${HOSTPY:-python3}                                 # the observatory runs on a host interpreter
PY=(apptainer exec --cleanenv --bind "$BINDS" --env LC_ALL=C --env LANG=C
    --env "PYTHONPATH=$CODE:/opt/rsi-control" --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1
    --env PYTHONDONTWRITEBYTECODE=1 --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1
    "$IMG" /usr/local/bin/python3)

pattern() { case $1 in temporal) echo "ecarsi.control.temporal";; scheduler) echo "ecarsi.warm_pool .*scheduler";;
    bridge) echo "ecarsi.agent serve";; coordinators) echo "ecarsi.control .*worker";;
    observatory) echo "ecarsi.observatory serve";; keeper) echo "worker-keeper.sh";; esac; }
# Skip container wrappers, interactive `bash -c` shells and this script's own subshells: a shell whose
# command text merely mentions a component (an editor, a heredoc) must never count as, or be killed as, that component.
pids() { pgrep -u "$USER" -f "$(pattern "$1")" | while read -r p; do tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -qE 'apptainer|bash -c|control-plane\.sh' || echo "$p"; done; }
launch() { local name=$1; shift; (cd "$CODE" && exec setsid nohup "$@" >>"$LOGS/$name.log" 2>&1 < /dev/null) & }

start() {
  pids "$1" | grep -q . && return 0
  case $1 in
    temporal) launch temporal "${PY[@]}" -m ecarsi.control.temporal --root "$CONTROL" --postgres-bin "${POSTGRES_BIN:?}" \
        --temporal-dir "${TEMPORAL_DIR:?}" --schema-dir "${SCHEMA_DIR:?}" --bind "$HOST_IP" ;;
    scheduler) launch scheduler "${PY[@]}" -m ecarsi.warm_pool --root "$POOL" scheduler --host "$(hostname -s)" ;;
    bridge) launch bridge "${PY[@]}" -m ecarsi.agent serve "$BRIDGE" ;;
    coordinators) local n; n=$(pids coordinators | wc -l)
        for ((i=n; i<COORDINATORS; i++)); do launch "coordinator-$i" env "APPTAINERENV_ECA_RSI_STAGE_LIMIT_FLOORS=$STAGE_LIMIT_FLOORS" \
            "${PY[@]}" -m ecarsi.control --service-root "$CONTROL" --task-queue "$TASK_QUEUE" worker --workflow-slots 2; done ;;
    observatory) launch observatory env PYTHONPATH="$CODE" "$HOSTPY" -m ecarsi.observatory serve --root "$BASE" --bind "$HOST_IP" --port "${OBSERVATORY_PORT:-8765}" --temporal-service-root "$CONTROL" ;;
    keeper) launch worker-keeper "$BASE/worker-keeper.sh" ;;
  esac
}
stop() {
  pids "$1" | while read -r p; do kill -TERM "$p" 2>/dev/null; done
  for _ in $(seq 20); do pids "$1" | grep -q . || return 0; sleep 1; done; echo "warning: $1 still running"
}
status() { for c in temporal scheduler bridge coordinators observatory keeper; do printf '%-13s %s\n' "$c" "$(n=$(pids "$c" | wc -l); [ "$n" -gt 0 ] && echo "running ($n proc)" || echo stopped)"; done; }

cmd=${1:-status}; shift || true
comps=("$@"); [ ${#comps[@]} -eq 0 ] && comps=(temporal scheduler bridge coordinators observatory keeper)
case $cmd in
  start) for c in "${comps[@]}"; do start "$c"; done; sleep 2; status ;;
  stop) for c in "${comps[@]}"; do stop "$c"; done; status ;;
  restart) for c in "${comps[@]}"; do stop "$c"; done; for c in "${comps[@]}"; do start "$c"; done; sleep 3; status ;;
  status) status ;;
  report) (cd /tmp && "${PY[@]}" -m ecarsi.observatory status --root "$BASE" --temporal-service-root "$CONTROL" "$@") ;;
  *) echo "usage: $0 start|stop|restart|status [component...] | report [--sessions HOURS] [--json]"; exit 2 ;;
esac
