#!/usr/bin/env bash
# run-eca-rsi.sh — thin shell entry for the eca-rsi main line.
#
#   ./run-eca-rsi.sh <eca-pp-dir> <root> [--rounds N] [--serve [PORT] [--ngrok --domain D]]
#   ./run-eca-rsi.sh <subcommand> ...        # organize | persample | loop | serve | ... (see eca-rsi --help)
#
# Picks the interpreter that has ecarsi + the kernels installed (ECA_RSI_PYTHON,
# else the dl2025 venv, else whatever `python` is) and hands everything to
# `python -m ecarsi`. A bare "<input> <root>" invocation means `run`.
set -euo pipefail

PY="${ECA_RSI_PYTHON:-/scratch/users/chensj16/venvs/dl2025/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python)"

case "${1:-}" in
  ""|-h|--help) exec "$PY" -m ecarsi --help ;;
  run|organize|persample|crosssample|zoomin|loop|ledger|index|serve|umapdata) exec "$PY" -m ecarsi "$@" ;;
  *) exec "$PY" -m ecarsi run "$@" ;;
esac
