#!/usr/bin/env bash
# Create an isolated ms-pred environment without requiring a particular package manager.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
ASSETS="${MSMS_ASSETS_DIR:-$ROOT/.cache}"
PYTHON="${MSMS_BASE_PYTHON:-python3}"
MS_PRED_DIR="${MS_PRED_DIR:-$ASSETS/ms-pred}"
if [[ ! -d "$MS_PRED_DIR/.git" ]]; then
  mkdir -p "$ASSETS"
  git clone https://github.com/coleygroup/ms-pred.git "$MS_PRED_DIR"
fi
if [[ ! -x "$ASSETS/ms-pred-venv/bin/python" ]]; then
  "$PYTHON" -m venv "$ASSETS/ms-pred-venv"
fi
"$ASSETS/ms-pred-venv/bin/python" -m pip install --upgrade pip
"$ASSETS/ms-pred-venv/bin/python" -m pip install -e "$ROOT"
"$ASSETS/ms-pred-venv/bin/python" -m pip install -e "$MS_PRED_DIR[cpu]"
"$ASSETS/ms-pred-venv/bin/python" -c 'import ms_pred, msbuddy, rdkit; print("ms-pred environment ready")'
printf 'Set MS_PRED_PYTHON=%s\n' "$ASSETS/ms-pred-venv/bin/python"
