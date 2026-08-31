#!/bin/bash
# Template: run OSP over every sample of an h5ad as a Slurm job array.
# Fill in SAMPLES, H5AD, and the venv path for your project, then:
#   sbatch --array=0-$((${#SAMPLES[@]}-1)) submit_array.sbatch
#SBATCH -J osp_run
#SBATCH -p normal
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32GB
#SBATCH -o osp_out/logs/%x_%A_%a.out

H5AD=data/your_dataset.h5ad
OUTROOT=osp_out
SAMPLES=(SAMPLE_A SAMPLE_B SAMPLE_C)
S=${SAMPLES[$SLURM_ARRAY_TASK_ID]}

python examples/run_one_sample.py "$H5AD" "$S" "$OUTROOT"
# add --annotate --model claude-sonnet-5 to also run the annotation agent
