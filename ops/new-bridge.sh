#!/bin/bash
# A second bridge with its own model catalog (#54): datasets whose spec names this bridge_root (a gate:
# ops/gate.py start --bridge=<root>) send their model turns to its catalog's models, beside production.
# usage: bash ops/new-bridge.sh <name> <catalog.json>      -> $STATE/<name>, routing and service copied from $BRIDGE
# then:  BRIDGE=$STATE/<name> INFRA=<version> bash ops/control-plane.sh start bridge
#        BRIDGE=$STATE/<name> VERSION=<version> bash ops/control-plane.sh start runners   (optional: else pool turns)
# Use a name that does not extend the main bridge's path (plan-bridge, not bridge-plan): older launchers match by prefix.
set -euo pipefail
NAME=${1:?bridge name}; CATALOG=$(readlink -f "${2:?model catalog}")
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
ROOT=$STATE/$NAME
[ ! -e "$ROOT/config.json" ] || { echo "$ROOT is already a bridge" >&2; exit 2; }
bash "$(dirname "$0")/run.sh" control -m ecarsi.agent init "$ROOT" --catalog "$CATALOG" --pool-root "$POOL" --concurrency 64
python3 - "$BRIDGE/config.json" "$ROOT/config.json" <<'PY'
import json, sys
main, new = (json.load(open(p)) for p in sys.argv[1:])
new.update({k: main[k] for k in ("routing", "service") if k in main})
json.dump(new, open(sys.argv[2], "w"), indent=1)
PY
echo "bridge $ROOT: catalog $CATALOG"
