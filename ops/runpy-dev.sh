#!/bin/bash
exec bash "$(dirname "$0")/run.sh" control --dev "$@"   # see run.sh
