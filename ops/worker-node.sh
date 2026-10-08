#!/bin/bash
# Deployment wrapper: sbatch --job-name=warmpool-normal --time=8:00:00 --partition=normal --cpus-per-task=16 --mem=32G worker-node.sh
# (a GPU worker: --partition=gpu --gpus=1; container/worker-node.sh re-enters as a GPU step, #58)
# Settings: ~/.config/ecarsi/deployment.env. The worker script and its launcher come from INFRA's published version,
# without INFRA from the image (control-plane.sh unpacks it into image-code/).
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
code=$BASE/image-code; [ -n "${INFRA:-}" ] && code=$CODE_HOME/versions/$INFRA
exec bash "$code/container/worker-node.sh"
