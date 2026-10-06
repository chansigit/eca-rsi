#!/bin/bash
# Publish a commit as a version (decision 0019): a read-only copy of its code in $CODE_HOME/versions/<commit12>/
# with version.json, byte-compiled and import-checked in both images. Publishing starts nothing:
# ops/start-version.sh starts its coordinators and runners, ops/set-current.sh makes new datasets use it.
# usage: publish-version.sh [commit]      (default: main of $CODE_HOME/src/eca-rsi)
#        VERSION_IMG=<control .sif> VERSION_SCIENCE_IMG=<compute .sif> publish-version.sh ...   a version with new
#        images (INSTALL.md A.11); default the images of deployment.env
# Settings: ~/.config/ecarsi/deployment.env (CODE_HOME, IMG, SCIENCE_IMG, BINDS).
set -euo pipefail
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
IMG=${VERSION_IMG:-$IMG}; SCIENCE_IMG=${VERSION_SCIENCE_IMG:-$SCIENCE_IMG}
REPO=${REPO:-$CODE_HOME/src/eca-rsi}
COMMIT=$(git -C "$REPO" rev-parse --verify "${1:-main}^{commit}")
NAME=${COMMIT:0:12}
VERSIONS=$CODE_HOME/versions; DEST=$VERSIONS/$NAME
[ -e "$DEST" ] && { echo "already published: $DEST"; exit 0; }
mkdir -p "$VERSIONS"; chmod 700 "$VERSIONS"
TMP=$(mktemp -d "$VERSIONS/.$NAME.XXXX")
git -C "$REPO" archive "$COMMIT" ecarsi osp msp zmip standissect_lite harness_bridge genesets container | tar -x -C "$TMP"
printf '{"name": "%s", "commit": "%s", "published": "%s", "control_image": "%s", "science_image": "%s"}\n' \
  "$NAME" "$COMMIT" "$(date -Iseconds)" "$IMG" "$SCIENCE_IMG" > "$TMP/version.json"
# Both images run Python 3.12: one byte-compile serves both, and a read-only tree cannot write one later.
in_image() { apptainer exec --cleanenv --bind "$BINDS" --env PYTHONSAFEPATH=1 \
  --env "PYTHONPATH=$TMP:/opt/rsi-control:/opt/rsi-python" "$1" /usr/local/bin/python3 "${@:2}"; }
in_image "$SCIENCE_IMG" -m compileall -q -j 0 "$TMP" >/dev/null
in_image "$IMG" -c "
import ecarsi, ecarsi.control.coordinator, ecarsi.agent.dispatch, ecarsi.agent.runner, ecarsi.warm_pool.backend, harness_bridge
assert ecarsi.version()['name'] == '$NAME' and ecarsi.task_queue() == 'ecarsi-$NAME', (ecarsi.version(), ecarsi.task_queue())
print('control: queue', ecarsi.task_queue())"
in_image "$SCIENCE_IMG" -c "
import importlib, ecarsi
assert ecarsi.version()['name'] == '$NAME'
for k in ('ecarsi.stages.persample', 'ecarsi.stages.crosssample', 'ecarsi.stages.zoomin', 'ecarsi.stages.release'):
    importlib.import_module(k)
for k in ('osp', 'msp', 'zmip'):
    api = importlib.import_module(k + '.api'); [getattr(api, n) for n in api.__all__]
print('science: stages and kernel api modules import')"
# The files it shares with the shared components (decision 0019): no version of them the shared side does not know.
SHARED_CODE=${INFRA:+$CODE_HOME/versions/$INFRA}
known=$(apptainer exec --cleanenv --bind "$BINDS" --env PYTHONSAFEPATH=1 --env "PYTHONPATH=${SHARED_CODE:-/opt/eca-rsi}:/opt/rsi-control" \
  "$IMG" /usr/local/bin/python3 -c "from ecarsi.contracts import KINDS; print(' '.join(KINDS))")
in_image "$IMG" -c "
from ecarsi.contracts import unknown_to
unknown = unknown_to('$known'.split())
assert not unknown, 'the shared components (${SHARED_CODE:-image}) do not know %s: update INFRA first' % unknown
print('contracts: the shared components know every shared file version this one writes')"
chmod -R a-w "$TMP"
mv -T "$TMP" "$DEST"
echo "published $NAME ($COMMIT) in $DEST"
