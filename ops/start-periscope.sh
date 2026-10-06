#!/bin/bash
# Periscope from the compute image, on the code of INFRA's version when deployment.env names one (decision 0019).
# Local only: reach it through your own attended ssh -L forward. Password from ~/.config/ecarsi/periscope-password
# (user:pass, mode 600; #21). Settings: ~/.config/ecarsi/deployment.env.
# Datasets: display_root and more_display_roots of ~/.config/ecarsi/results.json, plus periscope-datasets.json.
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
cd /tmp && setsid nohup apptainer exec --cleanenv --bind "$BINDS" \
  --env PYTHONSAFEPATH=1 --env PYTHONPATH=${INFRA:+$CODE_HOME/versions/$INFRA:}/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python \
  "$SCIENCE_IMG" /usr/local/bin/python3.12 -m ecarsi serve --port "$PERISCOPE_PORT" \
  --auth-file "$HOME/.config/ecarsi/periscope-password" \
  --control-plane "$BASE" --control-pool-root "$POOL" --control-bridge-root "$BRIDGE" --control-temporal-root "$CONTROL" \
  > "$BASE/control-logs/periscope.log" 2>&1 < /dev/null &
