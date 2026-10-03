#!/bin/bash
# eca control plane (deployment wrapper; $BASE/control-plane.sh links here). Settings: ~/.config/ecarsi/deployment.env (images, directories, ports, BINDS).
# The launcher itself comes from the control image, so the image is the single source of what runs.
# CODE=<checkout> before the command runs a checkout instead of the image snapshot (development only).
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$BASE/.control-plane.from-image.sh"
exec bash "$BASE/.control-plane.from-image.sh" "$@"
