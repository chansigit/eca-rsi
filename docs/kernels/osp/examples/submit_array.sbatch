#!/bin/bash
# Template: run OSP over every sample of an h5ad as a Slurm job array.
# Fill in SAMPLES, H5AD, and PYTHON for your project. Then submit an explicit
# zero-based range matching SAMPLES, for example:
#   mkdir -p osp_out/logs
#   sbatch --array=0-2 submit_array.sbatch
#SBATCH -J osp_run
#SBATCH -p normal
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32GB
#SBATCH -o osp_out/logs/%x_%A_%a.out

set -euo pipefail

H5AD=data/your_dataset.h5ad
OUTROOT=osp_out
PYTHON=/path/to/venv/bin/python
SAMPLES=(SAMPLE_A SAMPLE_B SAMPLE_C)
S=${SAMPLES[$SLURM_ARRAY_TASK_ID]}

srun "$PYTHON" examples/run_one_sample.py "$H5AD" "$S" "$OUTROOT"
# Add --annotate to use the configured backend/model, or pass --model explicitly.
