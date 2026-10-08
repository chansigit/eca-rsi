#!/bin/bash
# The tests of one layer, in parallel, inside the compute image (the dev worktree): the lane of the code you changed.
# usage: ops/test-lane.sh <lane> [pytest args]    lanes: agent control pool stages ui shared (ecarsi.<package>: the test
#        modules that import it), osp msp zmip bridge standissect (their own test directories), all (the whole suite).
# Every lane runs with pytest-xdist (-n auto; pytest-only holds it) unless -n is given. The whole suite stays the
# release check (INSTALL.md, tests/README.md); a lane is a quick check, not proof: shared modules reach everything.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); LANE=${1:?lane}; shift
cd "${WT:-$(. "$HOME/.config/ecarsi/deployment.env"; echo "$DEV_WORKTREE")}"
case $LANE in
  all) paths=(tests) ;;
  osp|msp|zmip|standissect) paths=(tests/${LANE/standissect/standissect_lite}) ;;
  bridge) paths=(tests/harness_bridge) ;;
  agent|control|pool|stages|ui) pkg=${LANE/pool/warm_pool}
    mapfile -t paths < <(grep -lE "ecarsi\.$pkg\b|from \.\.?$pkg\b" tests/test_*.py tests/*.py 2>/dev/null | grep -v conftest | sort -u) ;;
  shared) mapfile -t paths < <(grep -LE "ecarsi\.(agent|control|warm_pool|stages|ui)\b" tests/test_*.py | sort -u) ;;
  *) echo "unknown lane $LANE (agent control pool stages ui shared osp msp zmip bridge standissect all)" >&2; exit 2 ;;
esac
[ ${#paths[@]} -gt 0 ] || { echo "lane $LANE: no test module imports it" >&2; exit 2; }
n=(-n auto); case " $* " in *" -n "*|*" -n"[0-9]*) n=() ;; esac
echo "lane $LANE: ${#paths[@]} test paths" >&2
exec bash "$HERE/runsci-dev.sh" -m pytest -q -p no:cacheprovider "${n[@]}" "$@" "${paths[@]}"
