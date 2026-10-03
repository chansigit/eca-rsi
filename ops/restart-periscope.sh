#!/bin/bash
# kill only the public Periscope (its port) and its ngrok; never the user's pizza tunnel
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
for p in $(ps -eo pid,args | awk -v port="$PERISCOPE_PORT" '$2 ~ /python/ && $0 ~ ("ecarsi serve --port " port) {print $1}'); do kill $p; done
for p in $(ps -eo pid,args | awk -v port="$PERISCOPE_PORT" '$2 ~ /ngrok$/ && $3=="http" && $4==port {print $1}'); do kill $p; done
sleep 60   # ngrok releases the old agent session (ERR_NGROK_108 otherwise)
bash "$(dirname "$0")/start-periscope.sh"
