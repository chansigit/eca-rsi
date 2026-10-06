#!/bin/bash
# Restart Periscope: stop only the server on PERISCOPE_PORT, then start it again.
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
for p in $(ps -eo pid,args | awk -v port="$PERISCOPE_PORT" '$2 ~ /python/ && $0 ~ ("ecarsi serve --port " port) {print $1}'); do kill $p; done
sleep 2
bash "$(dirname "$0")/start-periscope.sh"
