#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
ASSETS="${MSMS_ASSETS_DIR:-$ROOT/.cache}"
FRIGID_DIR="${FRIGID_DIR:-$ASSETS/FRIGID}"
PYTHON="${MSMS_BASE_PYTHON:-python3}"
if [[ ! -d "$FRIGID_DIR/.git" ]]; then
  mkdir -p "$ASSETS"
  git clone --recurse-submodules https://github.com/coleygroup/FRIGID.git "$FRIGID_DIR"
  (cd "$FRIGID_DIR" && bash env/patch_ms_pred.sh)
fi
if [[ ! -x "$ASSETS/frigid-venv/bin/python" ]]; then
  "$PYTHON" -m venv "$ASSETS/frigid-venv"
fi
"$ASSETS/frigid-venv/bin/python" -m pip install --upgrade pip
"$ASSETS/frigid-venv/bin/python" -m pip install -r "$FRIGID_DIR/ms-pred/requirements.txt"
"$ASSETS/frigid-venv/bin/python" -m pip install -e "$FRIGID_DIR/ms-pred"
"$ASSETS/frigid-venv/bin/python" -m pip install -e "$FRIGID_DIR"
if [[ "${MSMS_DOWNLOAD_FRIGID_WEIGHTS:-0}" == 1 ]]; then
  mkdir -p "$ASSETS/checkpoints/frigid"
  curl -fL 'https://zenodo.org/records/19685145/files/frigid_pretrained_checkpoints.tar.gz?download=1' -o "$ASSETS/checkpoints/frigid/frigid_pretrained_checkpoints.tar.gz"
  echo '1059c193b5f3dd7034079076e943389b  '"$ASSETS/checkpoints/frigid/frigid_pretrained_checkpoints.tar.gz" | md5sum -c -
  tar -xzf "$ASSETS/checkpoints/frigid/frigid_pretrained_checkpoints.tar.gz" -C "$ASSETS/checkpoints/frigid"
fi
printf 'FRIGID_DIR=%s\nFRIGID_PYTHON=%s\n' "$FRIGID_DIR" "$ASSETS/frigid-venv/bin/python"
printf 'Set checkpoint paths in config or pass your licensed NIST assets directly. Public weights: https://zenodo.org/records/19685145\n'
