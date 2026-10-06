#!/bin/bash
# Build the two self-contained images from base images (the Python environments: docs/history/CONTAINER_LEGACY.md
# builds them from images/base/python312-slim.sif and container/control-requirements.lock).
#   control: + PostgreSQL, Temporal server / sql-tool / ui-server + schema, HQ, eca-rsi snapshot
#   science: + HQ, eca-rsi snapshot
# The service binaries come out of an existing control image (default: the current one, $IMG), where they sit
# relocated under /opt/rsi-services and /opt/rsi-bin: every control image carries them, nothing on scratch does.
# Unprivileged sandbox extract on $BUILD_DIR -> add files -> pack. Most updates need only ops/build-images-update.sh.
# usage: STAMP=<date>-<n> BASE_CONTROL=<control base .sif> BASE_SCIENCE=<science base .sif> [SERVICES_FROM=<control .sif>] \
#          bash ops/build-images.sh > $BASE/control-logs/build-images-$STAMP.log 2>&1
# Settings: ~/.config/ecarsi/deployment.env (the images' directory, DEV_WORKTREE, BUILD_DIR).
set -euo pipefail
export LC_ALL=C LANG=C
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
: "${STAMP:?stamp of the new images}" "${BASE_CONTROL:?control base image}" "${BASE_SCIENCE:?science base image}"
SERVICES_FROM=${SERVICES_FROM:-$IMG}
OUT=$(dirname "$IMG")
REPO=$DEV_WORKTREE
WORK=$BUILD_DIR/images-$STAMP
[ -e "$WORK" ] && { echo "work dir exists: $WORK"; exit 2; }
git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || { echo "eca-rsi checkout is dirty"; exit 2; }
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)
mkdir -p "$WORK"; cd "$WORK"

echo "$(date +%T) stage shared payload (eca-rsi @ $COMMIT, hq from $SERVICES_FROM)"
mkdir -p payload/opt/eca-rsi payload/opt/rsi-bin services/opt
git -C "$REPO" archive HEAD ecarsi osp msp zmip standissect_lite harness_bridge container pyproject.toml README.md | tar -C payload/opt/eca-rsi -xf -   # decision 0018
printf '{"eca_rsi_commit": "%s", "built": "%s", "image_stamp": "%s"}\n' "$COMMIT" "$(date -Iseconds)" "$STAMP" > payload/opt/eca-rsi/BUILD.json
apptainer exec "$SERVICES_FROM" cat /opt/rsi-bin/hq > payload/opt/rsi-bin/hq; chmod 755 payload/opt/rsi-bin/hq
"$WORK/payload/opt/rsi-bin/hq" --version

echo "$(date +%T) stage control-only payload (postgres, temporal)"
apptainer exec "$SERVICES_FROM" tar -C /opt -cf - rsi-services | tar -C services/opt -xf -

build() {  # name source extra-payload...
  local name=$1 src=$2; shift 2
  echo "$(date +%T) extract $src"
  apptainer build --sandbox "sb-$name" "$src" 2>&1 | tail -n 1
  for p in payload "$@"; do cp -a "$p/opt/." "sb-$name/opt/"; done
  echo "$(date +%T) pack $name"
  # apptainer build's own mksquashfs call dies with SIGSEGV (exit 139) on the 6.6 GB science sandbox here, every
  # time, while the same mksquashfs run by hand succeeds (also seen 2026-09-21): pack by hand, then wrap as a SIF
  # with the same single primary-system partition apptainer writes.
  /usr/libexec/apptainer/bin/mksquashfs "sb-$name" "$name.sqfs" -noappend -all-root -processors 4 >/dev/null
  apptainer sif new "$OUT/rsi-$name-$STAMP.sif"
  apptainer sif add "$OUT/rsi-$name-$STAMP.sif" "$name.sqfs" --datatype 4 --parttype 2 --partfs 1 --partarch 2 --groupid 1
}
build control "$BASE_CONTROL" services
build science "$BASE_SCIENCE"

echo "$(date +%T) verify"
apptainer exec --cleanenv "$OUT/rsi-control-$STAMP.sif" sh -c '
  /opt/rsi-bin/hq --version && /opt/rsi-services/postgres/bin/postgres --version && /opt/rsi-services/temporal/temporal-server --version 2>&1 | head -1
  ldd /opt/rsi-services/postgres/bin/psql | grep -E "not found" && exit 1 || true
  test -d /opt/rsi-services/temporal/schema/postgresql/v12/temporal/versioned && cat /opt/eca-rsi/BUILD.json'
cd /tmp; apptainer exec --cleanenv --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python "$OUT/rsi-science-$STAMP.sif" /usr/local/bin/python3.12 -c "
import ecarsi, msp, zmip, osp, standissect_lite, harness_bridge
assert all(m.__file__.startswith('/opt/eca-rsi/') for m in (msp, zmip, osp, standissect_lite, harness_bridge)), 'a package not from the snapshot'
print('science: ecarsi and the kernels from', ecarsi.__file__)"
apptainer exec --cleanenv "$OUT/rsi-science-$STAMP.sif" /opt/rsi-bin/hq --version
sha256sum "$OUT/rsi-control-$STAMP.sif" "$OUT/rsi-science-$STAMP.sif"
echo "$(date +%T) BUILD DONE"
