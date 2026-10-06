#!/bin/bash
# eca control plane (deployment wrapper; $BASE/control-plane.sh links here). Settings: ~/.config/ecarsi/deployment.env (images, directories, ports, BINDS).
# The launcher itself comes from the control image, so the image is the single source of what runs; with VERSION or
# INFRA set (decision 0019) it comes from that published version, $CODE_HOME/versions/<name>.
# CODE=<checkout> before the command runs a checkout instead of the image snapshot (development only).
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
V=${VERSION:-${INFRA:-}}
if [ -n "$V" ]; then cat "$CODE_HOME/versions/$V/container/control-plane.sh" > "$BASE/.control-plane.$V.sh" || exit 2
  exec bash "$BASE/.control-plane.$V.sh" "$@"; fi
apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$BASE/.control-plane.from-image.sh"
exec bash "$BASE/.control-plane.from-image.sh" "$@"
