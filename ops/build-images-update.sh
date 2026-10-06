#!/bin/bash
# Update the two images in place of a full rebuild: start from the current pair, replace /opt/eca-rsi with a fresh
# snapshot of the repository (ecarsi and, since decision 0018, the kernels and harness_bridge), take out the wheels
# those packages were installed from, (optionally) swap third-party wheels, then pack by hand.
# usage: FROM=20261001-2 STAMP=20261001-3 WHEELS=<dir of wheels to install> build-images-update.sh
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
PACKAGES="ecarsi osp msp zmip standissect_lite harness_bridge genesets"   # decision 0018: one repository
git -C "$REPO" archive HEAD $PACKAGES container pyproject.toml README.md > eca-rsi.tar
# A distribution's own files by its RECORD (a hand-installed one has only top_level.txt) and its dist-info, moved
# aside; never "..", "bin" (shared console scripts) or hidden names. Prints each site it was in.
take_out() {  # <image name> <site> <distribution>
  local info tops top
  for info in "$2"/"$3"-*.dist-info; do
    [ -d "$info" ] || continue
    if [ -f "$info/RECORD" ]; then tops=$(cut -d, -f1 "$info/RECORD" | cut -d/ -f1); else tops=$(cat "$info/top_level.txt"); fi
    for top in $(printf '%s\n' $tops "$(basename "$info")" | sort -u | grep -vxE '\.\.?|bin|__pycache__|\..*'); do
      [ -e "$2/$top" ] && mv "$2/$top" "replaced/$1-$(basename "$2")-$top-$RANDOM"
    done
    echo "$2"
  done
}
for name in control science; do
  echo "$(date +%T) extract $name"
  apptainer build --sandbox "sb-$name" "$IMAGES/rsi-$name-$FROM.sif" 2>&1 | tail -n 1
  mv "sb-$name/opt/eca-rsi" "replaced/$name-eca-rsi"; mkdir "sb-$name/opt/eca-rsi"
  tar -C "sb-$name/opt/eca-rsi" -xf eca-rsi.tar
  printf '{"eca_rsi_commit": "%s", "built": "%s", "image_stamp": "%s", "from": "%s"}\n' "$COMMIT" "$(date -Iseconds)" "$STAMP" "$FROM" > "sb-$name/opt/eca-rsi/BUILD.json"
  # decision 0018: the snapshot carries these packages; their old wheels go, so nothing else answers their imports
  for dist in osp_sc msp_sc zmip standissect_lite agent_harness_bridge; do
    for site in "sb-$name/opt/rsi-control" "sb-$name/opt/rsi-python"; do [ -d "$site" ] && take_out "$name" "$site" "$dist" >/dev/null; done
  done
  if [ -n "$WHEELS" ]; then
    # A wheel replaces its distribution wherever this image has it; a distribution the image lacks is not added.
    # Third-party only: the repository's own packages come with the snapshot (decision 0018).
    for whl in "$WHEELS"/*.whl; do
      dist=$(basename "$whl" | cut -d- -f1)   # e.g. openai_agents, msp_sc
      sites=()
      for site in "sb-$name/opt/rsi-control" "sb-$name/opt/rsi-python"; do
        [ -d "$site" ] || continue
        [ -n "$(take_out "$name" "$site" "$dist")" ] && sites+=("$site")
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
v = {d: m.version(d) for d in ('openai-agents', 'openai', 'urllib3', 'claude-agent-sdk')}
dup = {}
for d in m.distributions():
    dup.setdefault(d.metadata['Name'].lower().replace('_','-'), []).append(d.version)
dups = {k: x for k, x in dup.items() if len(set(x)) > 1}
print('$name', json.load(open('/opt/eca-rsi/BUILD.json'))['eca_rsi_commit'], v, 'duplicates:', dups or 'none', 'ecarsi:', ecarsi.__file__)
if dups: raise SystemExit('two versions of one distribution: a wheel did not replace the old one')
import importlib
# decision 0018: the repository's packages come from the snapshot, never from an installed copy
for k in ('harness_bridge',) + (('osp', 'msp', 'zmip', 'standissect_lite') if '$name' == 'science' else ()):
    if not importlib.import_module(k).__file__.startswith('/opt/eca-rsi/'): raise SystemExit(k + ' does not come from /opt/eca-rsi')
if '$name' == 'science':  # every kernel name eca-rsi uses resolves (decision 0014)
    for k in ('osp', 'msp', 'zmip'):
        api = importlib.import_module(k + '.api'); [getattr(api, n) for n in api.__all__]
    print('science: kernel api modules complete')"
done
sha256sum "$IMAGES/rsi-control-$STAMP.sif" "$IMAGES/rsi-science-$STAMP.sif"
echo "$(date +%T) BUILD DONE"
