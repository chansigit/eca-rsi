#!/bin/bash
# Update the two images in place of a full rebuild: start from the current pair, replace /opt/eca-rsi with a fresh
# snapshot and (optionally) swap Python packages in /opt/rsi-control (and the bridge in /opt/rsi-python), then pack
# by hand. usage: FROM=20261001-2 STAMP=20261001-3 WHEELS=<dir of wheels to install> build-images-update.sh
# Settings: ~/.config/ecarsi/deployment.env (the images' directory, DEV_WORKTREE, HOSTPY, BUILD_DIR).
set -euo pipefail
export LC_ALL=C LANG=C
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
IMAGES=$(dirname "$IMG")
: "${FROM:?stamp of the images to start from}" "${STAMP:?stamp of the new images}"
WHEELS=${WHEELS:-}
REPO=$DEV_WORKTREE
HP=$HOSTPY
WORK=$BUILD_DIR/images-$STAMP
[ -e "$WORK" ] && { echo "work dir exists: $WORK"; exit 2; }
git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || { echo "eca-rsi checkout is dirty"; exit 2; }
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)
mkdir -p "$WORK/replaced"; cd "$WORK"
git -C "$REPO" archive HEAD ecarsi container pyproject.toml README.md > eca-rsi.tar
for name in control science; do
  echo "$(date +%T) extract $name"
  apptainer build --sandbox "sb-$name" "$IMAGES/rsi-$name-$FROM.sif" 2>&1 | tail -n 1
  mv "sb-$name/opt/eca-rsi" "replaced/$name-eca-rsi"; mkdir "sb-$name/opt/eca-rsi"
  tar -C "sb-$name/opt/eca-rsi" -xf eca-rsi.tar
  printf '{"eca_rsi_commit": "%s", "built": "%s", "image_stamp": "%s", "from": "%s"}\n' "$COMMIT" "$(date -Iseconds)" "$STAMP" "$FROM" > "sb-$name/opt/eca-rsi/BUILD.json"
  if [ -n "$WHEELS" ]; then
    # A wheel replaces its distribution wherever this image has it (the kernels live in /opt/rsi-python of the
    # science image only, the bridge in both sites); a distribution the image lacks is not added.
    for whl in "$WHEELS"/*.whl; do
      dist=$(basename "$whl" | cut -d- -f1)   # e.g. openai_agents, msp_sc
      sites=()
      for site in "sb-$name/opt/rsi-control" "sb-$name/opt/rsi-python"; do
        [ -d "$site" ] || continue
        # the distribution's files, by its RECORD: top-level entries it owns, plus the dist-info itself
        for info in "$site"/"$dist"-*.dist-info; do
          [ -d "$info" ] || continue
          sites+=("$site")
          # only the package's own top-level entries: never "..", "bin" (shared console scripts) or hidden names
          for top in $(cut -d, -f1 "$info/RECORD" | cut -d/ -f1 | sort -u | grep -vxE '\.\.?|bin|__pycache__|\..*'); do
            [ -e "$site/$top" ] && mv "$site/$top" "replaced/$name-$(basename "$site")-$top-$RANDOM"
          done
        done
      done
      [ ${#sites[@]} -eq 0 ] && echo "$name: $dist is not in this image; $(basename "$whl") skipped"
      for site in ${sites[@]+"${sites[@]}"}; do "$HP" -m pip install -q --no-deps --no-compile --target "$site" "$whl"; done
    done
  fi
  echo "$(date +%T) pack $name"
  /usr/libexec/apptainer/bin/mksquashfs "sb-$name" "$name.sqfs" -noappend -all-root -processors 4 >/dev/null
  apptainer sif new "$IMAGES/rsi-$name-$STAMP.sif"
  apptainer sif add "$IMAGES/rsi-$name-$STAMP.sif" "$name.sqfs" --datatype 4 --parttype 2 --partfs 1 --partarch 2 --groupid 1
done
echo "$(date +%T) verify"
cd /tmp
for name in control science; do
  apptainer exec --cleanenv --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python \
    "$IMAGES/rsi-$name-$STAMP.sif" /usr/local/bin/python3 -c "
import importlib.metadata as m, ecarsi, json
v = {d: m.version(d) for d in ('openai-agents', 'openai', 'urllib3', 'agent-harness-bridge', 'claude-agent-sdk')}
dup = {}
for d in m.distributions():
    dup.setdefault(d.metadata['Name'].lower().replace('_','-'), []).append(d.version)
dups = {k: x for k, x in dup.items() if len(set(x)) > 1}
print('$name', json.load(open('/opt/eca-rsi/BUILD.json'))['eca_rsi_commit'], v, 'duplicates:', dups or 'none', 'ecarsi:', ecarsi.__file__)"
done
sha256sum "$IMAGES/rsi-control-$STAMP.sif" "$IMAGES/rsi-science-$STAMP.sif"
echo "$(date +%T) BUILD DONE"
