#!/bin/bash
exec bash "$(dirname "$0")/run.sh" compute --dev "$@"   # see run.sh
