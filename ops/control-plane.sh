#!/bin/bash
# eca control plane (deployment wrapper; $BASE/control-plane.sh links here). Settings: ~/.config/ecarsi/deployment.env (images, directories, ports, BINDS).
# The launcher comes from the control image or from a published version, $CODE_HOME/versions/<name> (decision 0019):
# VERSION=<name> runs that version's coordinators and runners; INFRA=<name> (deployment.env) the shared components.
# CODE=<checkout> before the command runs a checkout instead of the image snapshot (development only).
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
if [ -n "${VERSION:-}" ]; then   # one version's coordinators and runners
  cat "$CODE_HOME/versions/$VERSION/container/control-plane.sh" > "$BASE/.control-plane.$VERSION.sh" || exit 2
  exec bash "$BASE/.control-plane.$VERSION.sh" "$@"; fi
if [ -n "${INFRA:-}" ]; then     # the shared components run from the version INFRA names (deployment.env)
  SHARED=$BASE/.control-plane.$INFRA.sh; cat "$CODE_HOME/versions/$INFRA/container/control-plane.sh" > "$SHARED" || exit 2
else SHARED=$BASE/.control-plane.from-image.sh; apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$SHARED"; fi
CUR=$(readlink "$CODE_HOME/versions/current" 2>/dev/null); cmd=${1:-status}
[ -n "$CUR" ] && [[ $cmd =~ ^(start|stop|restart|status)$ ]] || exec bash "$SHARED" "$@"
# With a current version (ops/set-current.sh), its coordinators and runners run beside the shared components.
# The image's own coordinators would serve only the queue ecarsi-durable-v2 of runs started before the first version;
# to resume one of those: bash $BASE/.control-plane.from-image.sh start coordinators
set -o pipefail; shift || true
comps=("$@"); [ ${#comps[@]} -eq 0 ] && comps=(temporal hq scheduler bridge runners coordinators fleet-status pruner)
shared=(); own=()
for c in "${comps[@]}"; do case $c in runners|coordinators) own+=("$c") ;; *) shared+=("$c") ;; esac; done
image() { bash "$SHARED" "$@" | grep -vE '^(runners|coordinators) '; }
current() { VERSION=$CUR bash "$0" "$@" | grep -E '^(eca-rsi|runners|coordinators|error|warning)' | sed -E "s/^(runners|coordinators) /\1@$CUR /"; }
case $cmd in
  status) image status; current status ;;
  stop) { [ ${#own[@]} -eq 0 ] || current stop "${own[@]}"; } && { [ ${#shared[@]} -eq 0 ] || image stop "${shared[@]}"; } ;;
  *) { [ ${#shared[@]} -eq 0 ] || image "$cmd" "${shared[@]}"; } && { [ ${#own[@]} -eq 0 ] || current "$cmd" "${own[@]}"; } ;;
esac
