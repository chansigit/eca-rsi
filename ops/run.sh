#!/bin/bash
# Python inside an image. Settings: ~/.config/ecarsi/deployment.env.
# usage: run.sh control|compute [--dev] <python args>
#   control = the control image (Temporal client, agent SDKs); compute = the compute image (kernels, numerics).
#   Default: the image's own eca-rsi snapshot, /opt/eca-rsi -- the production code.
#   --dev: the checkout in $WT (default $DEV_WORKTREE) shadows the snapshot; runs from that checkout.
#   VERSION=<name>: a published version, $CODE_HOME/versions/<name> (decision 0019), shadows the snapshot;
#   unset, the current version (versions/current, ops/set-current.sh) if there is one.
# The state directories (BASE CONTROL POOL BRIDGE) are passed in for the ops/*.py helpers.
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
case $1 in control) RUN_IMG=$IMG; LIBS=/opt/rsi-control ;; compute) RUN_IMG=$SCIENCE_IMG; LIBS=/opt/rsi-control:/opt/rsi-python ;;
  *) echo "usage: run.sh control|compute [--dev] <python args>" >&2; exit 2 ;; esac; shift
VERSION=${VERSION:-$(readlink "$CODE_HOME/versions/current" 2>/dev/null)}
if [ "${1:-}" = --dev ]; then shift; W=${WT:-$DEV_WORKTREE}; cd "$W"; CODE=$W; SAFE=0
elif [ -n "${VERSION:-}" ]; then CODE=$CODE_HOME/versions/$VERSION; SAFE=1
  [ -f "$CODE/version.json" ] || { echo "no published version $VERSION in $CODE_HOME/versions" >&2; exit 2; }
else CODE=/opt/eca-rsi; SAFE=1; fi
exec apptainer exec --cleanenv --bind "$BINDS" --env LC_ALL=C --env LANG=C \
  --env "PYTHONPATH=$CODE:$LIBS:$PYTEST_LIBS" --env PYTHONDONTWRITEBYTECODE=1 \
  $([ $SAFE = 1 ] && echo --env PYTHONSAFEPATH=1) --env OMP_NUM_THREADS=4 \
  --env BASE="$BASE" --env CONTROL="$CONTROL" --env POOL="$POOL" --env BRIDGE="$BRIDGE" "$RUN_IMG" /usr/local/bin/python3 "$@"
