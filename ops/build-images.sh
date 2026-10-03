#!/bin/bash
# Build the two self-contained images from base images: everything a deployment needs is inside them.
#   control: + PostgreSQL (RUNPATH made relative), Temporal server / sql-tool / ui-server + schema, HQ, eca-rsi snapshot
#   science: + HQ, eca-rsi snapshot
# Unprivileged sandbox extract on $BUILD_DIR -> add files -> pack. Most updates need only ops/build-images-update.sh.
# usage: STAMP=<date>-<n> BASE_CONTROL=<control base .sif> BASE_SCIENCE=<science base .sif> \
#          TOOLS=<dir with hyperqueue-local/hq, postgresql-16.15/runtime, temporal-server-1.32.0> PATCHELF=<patchelf> \
#          bash ops/build-images.sh > $BASE/control-logs/build-images-$STAMP.log 2>&1
# Settings: ~/.config/ecarsi/deployment.env (the images' directory, DEV_WORKTREE, BUILD_DIR).
set -euo pipefail
export LC_ALL=C LANG=C
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
: "${STAMP:?stamp of the new images}" "${BASE_CONTROL:?control base image}" "${BASE_SCIENCE:?science base image}"
: "${TOOLS:?directory of the service binaries}" "${PATCHELF:?patchelf binary}"
OUT=$(dirname "$IMG")
SRC_CONTROL=$BASE_CONTROL
SRC_SCIENCE=$BASE_SCIENCE
REPO=$DEV_WORKTREE
WORK=$BUILD_DIR/images-$STAMP
[ -e "$WORK" ] && { echo "work dir exists: $WORK"; exit 2; }
git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || { echo "eca-rsi checkout is dirty"; exit 2; }
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)
mkdir -p "$WORK"; cd "$WORK"

echo "$(date +%T) stage shared payload (eca-rsi @ $COMMIT, hq)"
mkdir -p payload/opt/eca-rsi payload/opt/rsi-bin
git -C "$REPO" archive HEAD ecarsi container pyproject.toml README.md | tar -C payload/opt/eca-rsi -xf -
printf '{"eca_rsi_commit": "%s", "built": "%s", "image_stamp": "%s"}\n' "$COMMIT" "$(date -Iseconds)" "$STAMP" > payload/opt/eca-rsi/BUILD.json
cp "$TOOLS/hyperqueue-local/hq" payload/opt/rsi-bin/hq
"$WORK/payload/opt/rsi-bin/hq" --version

echo "$(date +%T) stage control-only payload (postgres, temporal)"
mkdir -p services/opt/rsi-services/postgres services/opt/rsi-services/temporal/schema/postgresql
cp -a "$TOOLS/postgresql-16.15/runtime/"{bin,lib,share} services/opt/rsi-services/postgres/
for f in services/opt/rsi-services/postgres/bin/* services/opt/rsi-services/postgres/lib/*.so* services/opt/rsi-services/postgres/lib/postgresql/*.so; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  if "$PATCHELF" --print-rpath "$f" >/dev/null 2>&1 && [ -n "$("$PATCHELF" --print-rpath "$f")" ]; then
    case "$f" in */bin/*) "$PATCHELF" --set-rpath '$ORIGIN/../lib' "$f";; */lib/postgresql/*) "$PATCHELF" --set-rpath '$ORIGIN/..' "$f";; *) "$PATCHELF" --set-rpath '$ORIGIN' "$f";; esac
  fi
done
if grep -rl "$TOOLS/postgresql" services/opt/rsi-services/postgres/bin >/dev/null; then echo "note: absolute tool path still inside some postgres binaries (compiled-in defaults; relocation is by relative layout)"; fi
cp "$TOOLS/temporal-server-1.32.0/"{temporal-server,temporal-sql-tool,ui-server} services/opt/rsi-services/temporal/
cp -a "$TOOLS/temporal-server-1.32.0/temporal-1.32.0/schema/postgresql/v12" services/opt/rsi-services/temporal/schema/postgresql/

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
build control "$SRC_CONTROL" services
build science "$SRC_SCIENCE"

echo "$(date +%T) verify"
apptainer exec --cleanenv "$OUT/rsi-control-$STAMP.sif" sh -c '
  /opt/rsi-bin/hq --version && /opt/rsi-services/postgres/bin/postgres --version && /opt/rsi-services/temporal/temporal-server --version 2>&1 | head -1
  ldd /opt/rsi-services/postgres/bin/psql | grep -E "not found|'"$TOOLS"'" && exit 1 || true
  test -d /opt/rsi-services/temporal/schema/postgresql/v12/temporal/versioned && cat /opt/eca-rsi/BUILD.json'
cd /tmp; apptainer exec --cleanenv --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python "$OUT/rsi-science-$STAMP.sif" /usr/local/bin/python3.12 -c "
import ecarsi, msp, zmip, osp; print('science: ecarsi from', ecarsi.__file__)"
apptainer exec --cleanenv "$OUT/rsi-science-$STAMP.sif" /opt/rsi-bin/hq --version
sha256sum "$OUT/rsi-control-$STAMP.sif" "$OUT/rsi-science-$STAMP.sif"
echo "$(date +%T) BUILD DONE"
