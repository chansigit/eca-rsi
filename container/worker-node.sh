#!/bin/bash
# A pool worker in its own Slurm job: the worker IS the job (Sherlock rejects sleeper jobs). It takes every granted
# core and 90 % of the memory, joins the pool's HQ server, and leaves when the job ends.
#   POOL=<pool dir> SCIENCE_IMG=<science .sif> sbatch --time=8:00:00 --cpus-per-task=16 --mem=32G worker-node.sh
#   a GPU worker: add --partition=gpu --gpus=1 (the job re-enters as a GPU step and joins with --gpu)
# Get this script from the image: `control-plane.sh host-code` unpacks the image's eca-rsi, container/ included.
# CODE=<checkout> (development) runs that checkout inside the container instead of the image snapshot.
set -u
: "${POOL:?pool directory}" "${SCIENCE_IMG:?science image (.sif)}"
HOSTPY=${HOSTPY:-python3}          # the launcher probes Slurm (scontrol) and GPUs (nvidia-smi) on the host
: "${BINDS:?host directories the containers see, comma-separated, e.g. /scratch,/home}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
INSIDE=${CODE:-/opt/eca-rsi}
cpus=$("$HOSTPY" -c "import os; print(','.join(map(str, sorted(os.sched_getaffinity(0)))))")
# a --mem-per-cpu job sets only SLURM_MEM_PER_CPU (MB per granted core)
mem=$(( ${SLURM_MEM_PER_NODE:-$(( SLURM_MEM_PER_CPU * $(tr ',' '\n' <<< "$cpus" | wc -l) ))} * 9 / 10 ))
# A GPU job (sbatch --gpus=N) re-enters as a job step that holds its GPUs, where slurm-worker --gpu takes the step's
# grant, as add-worker does. Started in the batch step, the worker joined CPU-only and the card idled (#58).
if [ "${SLURM_GPUS_ON_NODE:-0}" -gt 0 ] && [ -z "${ECA_GPU_STEP:-}" ]; then
  exec srun --ntasks=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:-$(nproc)}" --gpus="$SLURM_GPUS_ON_NODE" --mem=0 \
    --export=ALL,ECA_GPU_STEP=1 bash "$0"
fi
work=$POOL/worker-state/$(hostname -s)-$SLURM_JOB_ID
echo "worker node $(hostname -s) job $SLURM_JOB_ID cpus $cpus mem ${mem}MB gpus ${SLURM_STEP_GPUS:-none} eca-rsi ${CODE:-image snapshot}"
export PYTHONPATH=${CODE:-$HERE}
exec "$HOSTPY" -m ecarsi.warm_pool --root "$POOL" slurm-worker ${ECA_GPU_STEP:+--gpu} --cpus "$cpus" --memory-mb "$mem" --work-dir "$work" --job-id "$SLURM_JOB_ID" -- \
  apptainer exec --cleanenv --bind "$BINDS" \
  --env "PYTHONPATH=$INSIDE:/opt/rsi-control:/opt/rsi-python" --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1 --env PYTHONDONTWRITEBYTECODE=1 \
  --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 --env LC_ALL=C --env LANG=C \
  "$SCIENCE_IMG" /usr/local/bin/python3.12
