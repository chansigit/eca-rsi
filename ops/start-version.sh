#!/bin/bash
# Start a published version's coordinators and runners on this plane node (decision 0019). They serve only that
# version's task queue and its sessions' turns; production keeps running on its own version meanwhile.
# usage: start-version.sh <name> [coordinators, default 1]
# At most MAX_COORDINATORS (8) coordinators across all versions: each holds 6-7 GB and the plane node has 96 GB.
set -euo pipefail
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
NAME=${1:?version name}; N=${2:-1}; MAX=${MAX_COORDINATORS:-8}
[ -f "$CODE_HOME/versions/$NAME/version.json" ] || { echo "not published: $NAME (ops/publish-version.sh)"; exit 2; }
CONTROL=${CONTROL:-$BASE/durable-control}
count() {  # coordinators serving queue <regex>; container wrappers and shells are not coordinators (control-plane.sh)
  { pgrep -u "$USER" -f "ecarsi.control --service-root $CONTROL --task-queue $1 worker" || true; } | while read -r p; do
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -qE 'apptainer|bash -c' || echo "$p"; done | wc -l; }
running=$(count '[^ ]+'); mine=$(count "ecarsi-$NAME")
(( running - mine + N <= MAX )) || { echo "$running coordinators run ($mine of $NAME); $N more for $NAME exceeds $MAX"; exit 2; }
VERSION=$NAME COORDINATORS=$N exec bash "$(dirname "$0")/control-plane.sh" start coordinators runners
