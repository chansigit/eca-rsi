#!/bin/bash
# The control plane as a Slurm job (#62): the job starts every component and Periscope and stops them before its
# walltime, as ops/worker-node.sh makes the job a worker. A plane move is: submit the next plane job.
#   sbatch --job-name=eca-plane --time=2-00:00:00 --partition=normal --cpus-per-task=8 --mem=96G $BASE/ops/plane-node.sh
# 96 GB: the coordinators hold 6-7 GB each under load. Settings: ~/.config/ecarsi/deployment.env.
# Takeover: $CONTROL/plane-job names the job that runs the plane. A new job sends that job TERM (scancel --signal=TERM
# --batch), checks every 60 s that it has left the queue (at most 20 min), then starts. A plane that runs outside a
# plane job (a running scheduler's heartbeat under 2 min old, no running plane job named) is refused: stop it first.
# On TERM (a takeover, or 10 min before the walltime) the job records which versions' coordinators run here and how
# many ($CONTROL/plane-versions), stops Periscope, every version's coordinators and runners and the shared components,
# and ends. The next job starts the shared components and the current version, then the other recorded versions.
# Periscope listens on this node's 127.0.0.1: point your attended ssh -L forward at the host named in plane-job.
#SBATCH --signal=B:TERM@600
set -u
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
OPS=$BASE/ops; CONTROL=${CONTROL:-$BASE/durable-control}; POOL=${POOL:-$BASE/pool}; STATE=$CONTROL/plane-job
log() { echo "$(date '+%F %T') plane job $SLURM_JOB_ID on $(hostname -s): $*"; }
current() { readlink "$CODE_HOME/versions/current"; }
versions() {  # "<version> <coordinators>" for every version whose coordinators run on this node
  pgrep -u "$USER" -af "ecarsi.control --service-root $CONTROL --task-queue ecarsi-[0-9a-f]+ worker" \
    | grep -vE 'apptainer|bash -c' | grep -oE 'ecarsi-[0-9a-f]{12} ' | sort | uniq -c | awk '{sub("ecarsi-", "", $2); print $2, $1}'; }
stop() {
  trap '' TERM
  log "stopping"
  [ "$(cut -d' ' -f1 "$STATE" 2>/dev/null)" = "$SLURM_JOB_ID" ] && versions > "$CONTROL/plane-versions"
  pkill -u "$USER" -f "ecarsi serve --port $PERISCOPE_PORT"
  for v in $(versions | cut -d' ' -f1); do VERSION=$v bash "$OPS/control-plane.sh" stop coordinators runners; done
  bash "$OPS/control-plane.sh" stop
  log "stopped"; exit 0
}

read -r old host < "$STATE" 2>/dev/null
if [ -n "${old:-}" ] && [ "$old" != "$SLURM_JOB_ID" ] && squeue -h -j "$old" 2>/dev/null | grep -q .; then
  log "taking over from job $old on $host"
  scancel --signal=TERM --batch "$old"
  for _ in $(seq 20); do sleep 60; squeue -h -j "$old" 2>/dev/null | grep -q . || break; done
  squeue -h -j "$old" 2>/dev/null | grep -q . && { log "job $old still runs after 20 min: not starting"; exit 1; }
elif heartbeat=$(python3 -c "import json, time; d = json.load(open('$POOL/scheduler.json'))
print(d['host'] if d.get('state') != 'stopped' and time.time() - d['observed_at'] < 120 else '')" 2>/dev/null) && [ -n "$heartbeat" ]; then
  log "a plane runs outside a plane job (scheduler heartbeat from $heartbeat): stop it there first (control-plane.sh stop)"; exit 1
fi
echo "$SLURM_JOB_ID $(hostname -s)" > "$STATE"
trap stop TERM
bash "$OPS/control-plane.sh" start || { log "start failed"; stop; }
if [ -s "$CONTROL/plane-versions" ]; then
  while read -r v n; do
    [ "$v" = "$(current)" ] || [ ! -f "$CODE_HOME/versions/$v/version.json" ] || bash "$OPS/start-version.sh" "$v" "$n"
  done < "$CONTROL/plane-versions"
fi
bash "$OPS/start-periscope.sh"
log "plane up"
sleep infinity & wait $!
