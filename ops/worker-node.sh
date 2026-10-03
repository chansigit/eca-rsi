#!/bin/bash
# Deployment wrapper: sbatch --job-name=warmpool-normal --time=8:00:00 --partition=normal --cpus-per-task=16 --mem=32G worker-node.sh
# Settings: ~/.config/ecarsi/deployment.env. The worker script and the code it runs come from the images
# (control-plane.sh unpacks them into image-code/).
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
exec bash "$BASE/image-code/container/worker-node.sh"
