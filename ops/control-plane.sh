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
CUR=$(readlink "$CODE_HOME/versions/current" 2>/dev/null); cmd=${1:-status}
[ -n "$CUR" ] && [[ $cmd =~ ^(start|stop|restart|status)$ ]] || exec bash "$BASE/.control-plane.from-image.sh" "$@"
# With a current version (ops/set-current.sh), its coordinators and runners run beside the image's shared components.
# The image's own coordinators would serve only the queue ecarsi-durable-v2 of runs started before the first version;
# to resume one of those: bash $BASE/.control-plane.from-image.sh start coordinators
set -o pipefail; shift || true
comps=("$@"); [ ${#comps[@]} -eq 0 ] && comps=(temporal hq scheduler bridge runners coordinators fleet-status pruner)
shared=(); own=()
for c in "${comps[@]}"; do case $c in runners|coordinators) own+=("$c") ;; *) shared+=("$c") ;; esac; done
image() { bash "$BASE/.control-plane.from-image.sh" "$@" | grep -vE '^(runners|coordinators) '; }
current() { VERSION=$CUR bash "$0" "$@" | grep -E '^(eca-rsi|runners|coordinators|error|warning)' | sed -E "s/^(runners|coordinators) /\1@$CUR /"; }
case $cmd in
  status) image status; current status ;;
  stop) { [ ${#own[@]} -eq 0 ] || current stop "${own[@]}"; } && { [ ${#shared[@]} -eq 0 ] || image stop "${shared[@]}"; } ;;
  *) { [ ${#shared[@]} -eq 0 ] || image "$cmd" "${shared[@]}"; } && { [ ${#own[@]} -eq 0 ] || current "$cmd" "${own[@]}"; } ;;
esac
