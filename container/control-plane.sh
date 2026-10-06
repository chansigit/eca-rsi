#!/bin/bash
# Start/stop the v2 control plane on THIS host. The deployed copy lives in the run directory and
# exports the paths below (from ~/.config/ecarsi/deployment.env, see examples/deployment.env); everything is idempotent, logs under $BASE/control-logs, and state lives
# on shared storage, so run it again on a fresh node after an allocation expires.
#   control-plane.sh start|stop|restart|status [temporal|hq|scheduler|bridge|runners|coordinators|fleet-status|pruner ...]
#   Periscope is not a component: ops/start-periscope.sh starts it from the science image.
#   hq is the HyperQueue server on its own: restarting the scheduler then leaves every worker connected.
#   Without it running the scheduler starts (and on exit kills) a server of its own, as before.
#   control-plane.sh report [--sessions HOURS] [--json]     # text status of pool, bridge, workers, datasets
#   control-plane.sh tokens [--json]                        # per-dataset model turns and tokens (after a batch)
set -u
: "${BASE:?run directory holding the pool, bridge and control state}"
: "${IMG:?control image (.sif)}"
# Everything runs from the images (2026-10-01): the control image carries Temporal, PostgreSQL, HQ and a snapshot
# of eca-rsi at /opt/eca-rsi; the science image carries HQ and the same snapshot. Set CODE to a checkout only to
# develop: it comes first on PYTHONPATH and shadows the snapshot. SCIENCE_IMG is the science image (Periscope).
CODE=${CODE:-}
if [ -n "$CODE" ]; then CODE_IN=$CODE; HOST_CD=$CODE; else CODE_IN=/opt/eca-rsi; HOST_CD=/tmp; fi
# Published versions (decision 0019, ops/publish-version.sh): VERSION=<name> starts that version's coordinators and
# runners, on its own task queue; INFRA=<name> (deployment.env) is the version the shared components run from.
VERSION=${VERSION:-}; INFRA=${INFRA:-}
[ -n "$VERSION$INFRA" ] && VERSIONS=${CODE_HOME:?}/versions
if [ -n "$VERSION" ]; then  # its coordinators and runners run in the control image it was published with
  v_img=$(sed -n 's/.*"control_image": *"\([^"]*\)".*/\1/p' "$VERSIONS/$VERSION/version.json"); IMG=${v_img:-$IMG}; fi
POSTGRES_BIN=${POSTGRES_BIN:-/opt/rsi-services/postgres/bin}
TEMPORAL_DIR=${TEMPORAL_DIR:-/opt/rsi-services/temporal}
SCHEMA_DIR=${SCHEMA_DIR:-/opt/rsi-services/temporal/schema/postgresql/v12}
CONTROL=${CONTROL:-$BASE/durable-control}; POOL=${POOL:-$BASE/pool}; BRIDGE=${BRIDGE:-$BASE/bridge}
LOGS=$BASE/control-logs; mkdir -p "$LOGS"
COORDINATORS=${COORDINATORS:-4}; TASK_QUEUE=${TASK_QUEUE:-ecarsi-durable-v2}
[ -n "$VERSION" ] && TASK_QUEUE=ecarsi-$VERSION
STAGE_LIMIT_FLOORS=${STAGE_LIMIT_FLOORS:-}                # e.g. '{"max_in_flight_deg": 12, "max_in_flight_lineages": 6}'
# TEMPORAL_PORT / DATABASE_PORT / UI_PORT: set them when another control plane shares the host.
# TEMPORAL_DYNAMIC_CONFIG: a Temporal dynamic-config YAML (hot-reloaded), e.g. a longer default workflow task timeout.
: "${BINDS:?host directories the containers see, comma-separated, e.g. /scratch,/home}"
HOST_IP=$(hostname -I | awk '{print $1}')
python_for() {  # PY = python3 in the control image with <code> first on its path
  PY=(apptainer exec --cleanenv --bind "$BINDS" --env LC_ALL=C --env LANG=C
      --env "PYTHONPATH=$1:/opt/rsi-control" --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1
      --env PYTHONDONTWRITEBYTECODE=1 --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1
      "$IMG" /usr/local/bin/python3); }
code_for() { case $1 in coordinators|runners) [ -n "$VERSION" ] && { echo "$VERSIONS/$VERSION"; return; } ;;
             *) [ -n "$VERSION" ] && { echo "error: VERSION starts coordinators and runners only, not $1" >&2; return 1; }
                [ -n "$INFRA" ] && { echo "$VERSIONS/$INFRA"; return; } ;; esac
             echo "$CODE_IN"; }
python_for "$CODE_IN"

# Patterns name this run directory's roots, so two control planes on one host never count or kill each other.
pattern() { case $1 in temporal) echo "ecarsi.control.temporal --root $CONTROL";; scheduler) echo "ecarsi.warm_pool --root $POOL scheduler";;
    hq) echo "ecarsi.warm_pool --root $POOL hq-server";;
    bridge) echo "ecarsi.agent serve $BRIDGE";; coordinators) echo "ecarsi.control --service-root $CONTROL --task-queue $TASK_QUEUE worker";;
    runners) echo "ecarsi.agent runners $BRIDGE${VERSION:+ --version $VERSION}\$";;   # anchored: one version's runners only
    fleet-status) echo "fleet-status.py --service-root $CONTROL";;
    pruner) echo "request-pruner.py --service-root $CONTROL";; esac; }
# Skip container wrappers, interactive `bash -c` shells and this script's own subshells: a shell whose
# command text merely mentions a component (an editor, a heredoc) must never count as, or be killed as, that component.
pids() { pgrep -u "$USER" -f "$(pattern "$1")" | while read -r p; do tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -qE 'apptainer|bash -c|control-plane\.sh' || echo "$p"; done; }
launch() { local name=$1; shift; (cd "$HOST_CD" && exec setsid nohup "$@" >>"$LOGS/$name.log" 2>&1 < /dev/null) & }
# Which eca-rsi runs: a checkout (path + commit) or the image snapshot (its BUILD.json). Logged at every start,
# because a checkout and a snapshot can carry the same version number with different source.
identity() {
  if [ -n "$VERSION$INFRA" ]; then echo "eca-rsi: version ${VERSION:-$INFRA} ($VERSIONS/${VERSION:-$INFRA})"
  elif [ -n "$CODE" ]; then echo "eca-rsi: checkout $CODE @ $(git -C "$CODE" log -1 --format='%h %cs' 2>/dev/null || echo '?')"
  else echo "eca-rsi: image snapshot $("${PY[@]}" -c 'print(open("/opt/eca-rsi/BUILD.json").read().strip())' 2>/dev/null)"; fi
}
# Host-side helpers (worker launch, add-worker) need eca-rsi on the host: they call scontrol, nvidia-smi and ssh,
# which the images do not have. Unpack the image snapshot next to the control state once per start.
host_code() {
  [ -n "$CODE" ] && { echo "$CODE"; return; }
  [ -n "$VERSION$INFRA" ] && { echo "$VERSIONS/${VERSION:-$INFRA}"; return; }
  local dest=$BASE/image-code
  rm -rf "$dest.new"; mkdir -p "$dest.new"
  apptainer exec "$IMG" tar -C /opt/eca-rsi -cf - . | tar -C "$dest.new" -xf -
  rm -rf "$dest.old"; [ -d "$dest" ] && mv "$dest" "$dest.old"; mv "$dest.new" "$dest"; rm -rf "$dest.old"
  echo "$dest"
}

start() {
  [ "$1" != coordinators ] && pids "$1" | grep -q . && return 0   # coordinators top up to COORDINATORS below
  local code; code=$(code_for "$1") || return 1; python_for "$code"
  case $1 in
    temporal) launch temporal "${PY[@]}" -m ecarsi.control.temporal --root "$CONTROL" --postgres-bin "${POSTGRES_BIN:?}" \
        --temporal-dir "${TEMPORAL_DIR:?}" --schema-dir "${SCHEMA_DIR:?}" --bind "$HOST_IP" \
        ${TEMPORAL_PORT:+--port $TEMPORAL_PORT} ${DATABASE_PORT:+--database-port $DATABASE_PORT} ${UI_PORT:+--ui-port $UI_PORT} \
        ${TEMPORAL_DYNAMIC_CONFIG:+--dynamic-config $TEMPORAL_DYNAMIC_CONFIG} ;;
    hq) launch hq-server "${PY[@]}" -m ecarsi.warm_pool --root "$POOL" hq-server --host "$(hostname -s)"
        # the scheduler looks for this lock once, at start: hold it before a scheduler can look
        # without it the scheduler would start an embedded server of its own beside this one: stop here
        for _ in $(seq 60); do flock -n "$POOL/hq-server.lock" true || return 0; sleep 1; done
        echo "error: hq-server did not take $POOL/hq-server.lock within 60 s; see $LOGS/hq-server.log; nothing after hq started" >&2; return 1 ;;
    scheduler) launch scheduler "${PY[@]}" -m ecarsi.warm_pool --root "$POOL" scheduler --host "$(hostname -s)" ;;
    bridge) launch bridge "${PY[@]}" -m ecarsi.agent serve "$BRIDGE" ;;
    coordinators) local n; n=$(pids coordinators | wc -l)
        for ((i=n; i<COORDINATORS; i++)); do launch "coordinator${VERSION:+-$VERSION}-$i" env "APPTAINERENV_ECA_RSI_STAGE_LIMIT_FLOORS=$STAGE_LIMIT_FLOORS" \
            "${PY[@]}" -m ecarsi.control --service-root "$CONTROL" --task-queue "$TASK_QUEUE" worker --workflow-slots 2; done ;;
    fleet-status) launch fleet-status "${PY[@]}" "$code/container/fleet-status.py" --service-root "$CONTROL" --out "$BASE/fleet-status.json" ;;
    runners) launch "runners${VERSION:+-$VERSION}" "${PY[@]}" -m ecarsi.agent runners "$BRIDGE" ${VERSION:+--version "$VERSION"} ;;
    pruner) launch request-pruner "${PY[@]}" "$code/container/request-pruner.py" --service-root "$CONTROL" --pool-root "$POOL" \
        --fleet-status "$BASE/fleet-status.json" --interval "${PRUNE_INTERVAL:-3600}" ;;
  esac
}
stop() {
  pids "$1" | while read -r p; do kill -TERM "$p" 2>/dev/null; done
  for _ in $(seq 20); do pids "$1" | grep -q . || return 0; sleep 1; done; echo "warning: $1 still running"
}
status() { for c in temporal hq scheduler bridge runners coordinators fleet-status pruner; do printf '%-13s %s\n' "$c" "$(n=$(pids "$c" | wc -l); [ "$n" -gt 0 ] && echo "running ($n proc)" || echo stopped)"; done; }

cmd=${1:-status}; shift || true
comps=("$@"); [ ${#comps[@]} -eq 0 ] && comps=(temporal hq scheduler bridge runners coordinators fleet-status pruner)
case $cmd in
  start) identity | tee -a "$LOGS/identity.log"; host_code >/dev/null; for c in "${comps[@]}"; do start "$c" || exit 1; done; sleep 2; status ;;
  stop) for c in "${comps[@]}"; do stop "$c"; done; status ;;
  restart) identity | tee -a "$LOGS/identity.log"; host_code >/dev/null; for c in "${comps[@]}"; do stop "$c"; done; for c in "${comps[@]}"; do start "$c" || exit 1; done; sleep 3; status ;;
  identity) identity ;;
  host-code) host_code ;;   # the eca-rsi that host-side helpers (worker-node.sh, add-worker) should put on PYTHONPATH
  status) status ;;
  report) (cd /tmp && "${PY[@]}" -m ecarsi.observatory status --root "$BASE" --pool-root "$POOL" --bridge-root "$BRIDGE" --temporal-service-root "$CONTROL" "$@") ;;
  tokens) (cd /tmp && "${PY[@]}" -m ecarsi.observatory tokens --bridge-root "$BRIDGE" "$@") ;;   # per-dataset token totals; one paced walk of the bridge
  productivity|timeline) (cd /tmp && "${PY[@]}" -m ecarsi.observatory "$cmd" --root "$BASE" --pool-root "$POOL" "$@") ;;   # the Operations page's tables, on the command line
  *) echo "usage: $0 start|stop|restart|status [component...] | identity | host-code | report [--sessions HOURS] [--json] | tokens [--json] | productivity [--json] | timeline [--hours H] [--host NODE] [--dataset SUBSTR] [--running] [--json]"; exit 2 ;;
esac
