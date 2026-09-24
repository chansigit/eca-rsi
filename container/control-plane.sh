#!/bin/bash
# Start/stop the v2 control plane on THIS host. The deployed copy lives in the run directory and
# exports the paths below; everything is idempotent, logs under $BASE/control-logs, and state lives
# on shared storage, so run it again on a fresh node after an allocation expires.
#   control-plane.sh start|stop|restart|status [temporal|hq|scheduler|bridge|coordinators|observatory|fleet-status|pruner|keeper ...]
#   hq is the HyperQueue server on its own: restarting the scheduler then leaves every worker connected.
#   Without it running the scheduler starts (and on exit kills) a server of its own, as before.
#   control-plane.sh report [--sessions HOURS] [--json]     # text status of pool, bridge, workers, datasets
#   control-plane.sh tokens [--json]                        # per-dataset model turns and tokens (after a batch)
set -u
: "${BASE:?run directory holding the pool, bridge and control state}"
: "${IMG:?control image (.sif)}"
CODE=${CODE:-$(cd "$(dirname "$0")/.." && pwd)}       # checkout that PYTHONPATH points at
CONTROL=${CONTROL:-$BASE/durable-control}; POOL=${POOL:-$BASE/pool}; BRIDGE=${BRIDGE:-$BASE/bridge}
LOGS=$BASE/control-logs; mkdir -p "$LOGS"
COORDINATORS=${COORDINATORS:-4}; TASK_QUEUE=${TASK_QUEUE:-ecarsi-durable-v2}
STAGE_LIMIT_FLOORS=${STAGE_LIMIT_FLOORS:-}                # e.g. '{"max_in_flight_deg": 12, "max_in_flight_lineages": 6}'
# TEMPORAL_PORT / DATABASE_PORT / UI_PORT / OBSERVATORY_PORT: set them when another control plane shares the host.
# TEMPORAL_DYNAMIC_CONFIG: a Temporal dynamic-config YAML (hot-reloaded), e.g. a longer default workflow task timeout.
BINDS=${BINDS:-/scratch,/oak,/home,/lscratch}
HOST_IP=$(hostname -I | awk '{print $1}')
HOSTPY=${HOSTPY:-python3}                                 # Periscope (with the control-plane monitor at /_control/) runs on a host interpreter
PY=(apptainer exec --cleanenv --bind "$BINDS" --env LC_ALL=C --env LANG=C
    --env "PYTHONPATH=$CODE:/opt/rsi-control" --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1
    --env PYTHONDONTWRITEBYTECODE=1 --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1
    "$IMG" /usr/local/bin/python3)

# Patterns name this run directory's roots, so two control planes on one host never count or kill each other.
pattern() { case $1 in temporal) echo "ecarsi.control.temporal --root $CONTROL";; scheduler) echo "ecarsi.warm_pool --root $POOL scheduler";;
    hq) echo "ecarsi.warm_pool --root $POOL hq-server";;
    bridge) echo "ecarsi.agent serve $BRIDGE";; runners) echo "ecarsi.agent runners $BRIDGE";; coordinators) echo "ecarsi.control --service-root $CONTROL .*worker";;
    observatory) echo "ecarsi.serve --registry $BASE/periscope-registry.json";;   # the registry path, not --control-plane: another Periscope may serve the same run directory
    fleet-status) echo "fleet-status.py --service-root $CONTROL";;
    pruner) echo "request-pruner.py --service-root $CONTROL";;
    keeper) echo "worker-keeper.sh $POOL";; esac; }
# Skip container wrappers, interactive `bash -c` shells and this script's own subshells: a shell whose
# command text merely mentions a component (an editor, a heredoc) must never count as, or be killed as, that component.
pids() { pgrep -u "$USER" -f "$(pattern "$1")" | while read -r p; do tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -qE 'apptainer|bash -c|control-plane\.sh' || echo "$p"; done; }
launch() { local name=$1; shift; (cd "$CODE" && exec setsid nohup "$@" >>"$LOGS/$name.log" 2>&1 < /dev/null) & }

start() {
  [ "$1" != coordinators ] && pids "$1" | grep -q . && return 0   # coordinators top up to COORDINATORS below
  case $1 in
    temporal) launch temporal "${PY[@]}" -m ecarsi.control.temporal --root "$CONTROL" --postgres-bin "${POSTGRES_BIN:?}" \
        --temporal-dir "${TEMPORAL_DIR:?}" --schema-dir "${SCHEMA_DIR:?}" --bind "$HOST_IP" \
        ${TEMPORAL_PORT:+--port $TEMPORAL_PORT} ${DATABASE_PORT:+--database-port $DATABASE_PORT} ${UI_PORT:+--ui-port $UI_PORT} \
        ${TEMPORAL_DYNAMIC_CONFIG:+--dynamic-config $TEMPORAL_DYNAMIC_CONFIG} ;;
    hq) launch hq-server "${PY[@]}" -m ecarsi.warm_pool --root "$POOL" hq-server --host "$(hostname -s)"
        # the scheduler looks for this lock once, at start: hold it before a scheduler can look
        for _ in $(seq 60); do flock -n "$POOL/hq-server.lock" true || return 0; sleep 1; done; echo "warning: hq-server did not start" ;;
    scheduler) launch scheduler "${PY[@]}" -m ecarsi.warm_pool --root "$POOL" scheduler --host "$(hostname -s)" ;;
    bridge) launch bridge "${PY[@]}" -m ecarsi.agent serve "$BRIDGE" ;;
    coordinators) local n; n=$(pids coordinators | wc -l)
        for ((i=n; i<COORDINATORS; i++)); do launch "coordinator-$i" env "APPTAINERENV_ECA_RSI_STAGE_LIMIT_FLOORS=$STAGE_LIMIT_FLOORS" \
            "${PY[@]}" -m ecarsi.control --service-root "$CONTROL" --task-queue "$TASK_QUEUE" worker --workflow-slots 2; done ;;
    observatory) launch observatory env PYTHONPATH="$CODE" "$HOSTPY" -m ecarsi.serve --registry "$BASE/periscope-registry.json" --control-plane "$BASE" \
        --control-pool-root "$POOL" --control-bridge-root "$BRIDGE" --control-temporal-root "$CONTROL" --bind "$HOST_IP" --port "${OBSERVATORY_PORT:-8765}" ;;
    fleet-status) launch fleet-status "${PY[@]}" "$CODE/container/fleet-status.py" --service-root "$CONTROL" --out "$BASE/fleet-status.json" ;;
    runners) launch runners "${PY[@]}" -m ecarsi.agent runners "$BRIDGE" ;;
    pruner) launch request-pruner "${PY[@]}" "$CODE/container/request-pruner.py" --service-root "$CONTROL" --pool-root "$POOL" \
        --fleet-status "$BASE/fleet-status.json" --interval "${PRUNE_INTERVAL:-3600}" ;;
    keeper) launch worker-keeper env CODE="$CODE" HOSTPY="$HOSTPY" "$CODE/container/worker-keeper.sh" "$POOL" ;;
  esac
}
stop() {
  pids "$1" | while read -r p; do kill -TERM "$p" 2>/dev/null; done
  for _ in $(seq 20); do pids "$1" | grep -q . || return 0; sleep 1; done; echo "warning: $1 still running"
}
status() { for c in temporal hq scheduler bridge runners coordinators observatory fleet-status pruner keeper; do printf '%-13s %s\n' "$c" "$(n=$(pids "$c" | wc -l); [ "$n" -gt 0 ] && echo "running ($n proc)" || echo stopped)"; done; }

cmd=${1:-status}; shift || true
comps=("$@"); [ ${#comps[@]} -eq 0 ] && comps=(temporal hq scheduler bridge runners coordinators observatory fleet-status pruner keeper)
case $cmd in
  start) for c in "${comps[@]}"; do start "$c"; done; sleep 2; status ;;
  stop) for c in "${comps[@]}"; do stop "$c"; done; status ;;
  restart) for c in "${comps[@]}"; do stop "$c"; done; for c in "${comps[@]}"; do start "$c"; done; sleep 3; status ;;
  status) status ;;
  report) (cd /tmp && "${PY[@]}" -m ecarsi.observatory status --root "$BASE" --pool-root "$POOL" --bridge-root "$BRIDGE" --temporal-service-root "$CONTROL" "$@") ;;
  tokens) (cd /tmp && "${PY[@]}" -m ecarsi.observatory tokens --bridge-root "$BRIDGE" "$@") ;;   # per-dataset token totals; one paced walk of the bridge
  *) echo "usage: $0 start|stop|restart|status [component...] | report [--sessions HOURS] [--json] | tokens [--json]"; exit 2 ;;
esac
