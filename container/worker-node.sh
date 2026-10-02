#!/bin/bash
# A pool worker in its own Slurm job: the worker IS the job (Sherlock rejects sleeper jobs). It takes every granted
# core and 90 % of the memory, joins the pool's HQ server, and leaves when the job ends.
#   POOL=<pool dir> SCIENCE_IMG=<science .sif> sbatch --time=8:00:00 --cpus-per-task=16 --mem=32G worker-node.sh
# Get this script from the image: `control-plane.sh host-code` unpacks the image's eca-rsi, container/ included.
# CODE=<checkout> (development) runs that checkout inside the container instead of the image snapshot.
set -u
: "${POOL:?pool directory}" "${SCIENCE_IMG:?science image (.sif)}"
HOSTPY=${HOSTPY:-python3}          # the launcher probes Slurm (scontrol) and GPUs (nvidia-smi) on the host
BINDS=${BINDS:-/scratch,/oak,/home,/lscratch}
HERE=$(cd "$(dirname "$0")/.." && pwd)
INSIDE=${CODE:-/opt/eca-rsi}
cpus=$("$HOSTPY" -c "import os; print(','.join(map(str, sorted(os.sched_getaffinity(0)))))")
mem=$(( SLURM_MEM_PER_NODE * 9 / 10 ))
work=$POOL/worker-state/$(hostname -s)-$SLURM_JOB_ID
echo "worker node $(hostname -s) job $SLURM_JOB_ID cpus $cpus mem ${mem}MB eca-rsi ${CODE:-image snapshot}"
export PYTHONPATH=${CODE:-$HERE}
exec "$HOSTPY" -m ecarsi.warm_pool --root "$POOL" slurm-worker --cpus "$cpus" --memory-mb "$mem" --work-dir "$work" --job-id "$SLURM_JOB_ID" -- \
  apptainer exec --cleanenv --bind "$BINDS" \
  --env "PYTHONPATH=$INSIDE:/opt/rsi-control:/opt/rsi-python" --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1 --env PYTHONDONTWRITEBYTECODE=1 \
  --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 --env LC_ALL=C --env LANG=C \
  "$SCIENCE_IMG" /usr/local/bin/python3.12
