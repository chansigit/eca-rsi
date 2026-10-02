#!/bin/bash
# Keep the warm pool's workers registered without a human. Launched by control-plane.sh as
#   worker-keeper.sh <pool root>      (BASE, CODE, HOSTPY come from the environment)
#   * every running ${KEEPER_PREFIX}-* Slurm job of this user without a registered worker gets `add-worker`
#   * when LEAD > 0 and a job has < LEAD seconds left with no replacement pending, sbatch one
# LEAD=0 (default): never sbatch; the user requests nodes, this only add-workers them. ONCE=1 runs one tick.
set -u
POOL=${1:?pool root}
CODE=${CODE:?checkout that PYTHONPATH points at}
HOSTPY=${HOSTPY:-python3}
LEAD=${LEAD:-0}; INTERVAL=${INTERVAL:-300}; KEEPER_PREFIX=${KEEPER_PREFIX:-warmpool}
JOB_SCRIPT=${JOB_SCRIPT:-$HOME/hpc_compute.sh}          # idle job that holds the allocation; add-worker joins it
declare -A SUBMIT=(
  [$KEEPER_PREFIX-bigmem]="--time=24:00:00 --partition=bigmem --cpus-per-task=64 --mem=256G"
  [$KEEPER_PREFIX-gpu]="--time=24:00:00 --partition=gpu --gpus=1 -C GPU_GEN:AMP|GPU_GEN:LOV|GPU_GEN:HPR --cpus-per-task=32 --mem=64G"
  [$KEEPER_PREFIX-normal]="--time=48:00:00 --partition=normal --cpus-per-task=8 --mem=32G"
)
log() { echo "$(date '+%F %T') $*"; }

tick() {
  local name id node end left pending latest
  for name in "${!SUBMIT[@]}"; do
    latest=0
    while read -r id node end; do
      [ -n "${id:-}" ] || continue
      left=$(( $(date -d "$end" +%s) - $(date +%s) ))
      [ "$left" -gt "$latest" ] && latest=$left
      if ! grep -q '"running"' "$POOL/worker-state/$node-$id/launcher.json" 2>/dev/null; then
        log "add-worker $node (job $id, ${left}s left)"
        PYTHONPATH=$CODE "$HOSTPY" -m ecarsi.warm_pool --root "$POOL" add-worker "$node" --job-id "$id" 2>&1 | tail -n 3
      fi
    done < <(squeue --me -h -n "$name" -t R -o "%i %N %e")
    pending=$(squeue --me -h -n "$name" -t PD -o "%i" | wc -l)
    if [ "$LEAD" -gt 0 ] && [ "$latest" -lt "$LEAD" ] && [ "$pending" -eq 0 ]; then
      log "submit replacement $name (longest remaining ${latest}s)"
      # shellcheck disable=SC2086  # option string is ours; word-splitting is intended
      sbatch --job-name="$name" ${SUBMIT[$name]} "$JOB_SCRIPT" 2>&1 | tail -n 1
    fi
  done
}

if [ "${ONCE:-0}" = 1 ]; then tick; exit 0; fi
log "worker-keeper started on $(hostname -s) for $POOL, LEAD=$LEAD INTERVAL=$INTERVAL"
while true; do tick; sleep "$INTERVAL"; done
