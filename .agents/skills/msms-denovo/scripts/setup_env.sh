#!/usr/bin/env bash
# One-time setup for the denovo pixi environment.
# Run from the project root:
#   bash .agents/skills/msms-denovo/scripts/setup_env.sh
#
# Safe to re-run — pip skips packages that are already installed.

set -euo pipefail

ENV="denovo"
PIXI="pixi run --environment $ENV"

CONFIG="$(dirname "$0")/../../../../configs/default.yaml"
FRIGID_DIR=$(python3 -c "
import yaml, os
cfg = yaml.safe_load(open('$CONFIG'))
p = cfg['models']['denovo']['frigid_src']
print(os.path.expanduser(p))
")

if [[ ! -d "$FRIGID_DIR" ]]; then
    echo "ERROR: frigid_src not found: $FRIGID_DIR"
    echo "Clone FRIGID and set models.denovo.frigid_src in configs/default.yaml:"
    echo "  git clone --recurse-submodules https://github.com/coleygroup/FRIGID $FRIGID_DIR"
    exit 1
fi

echo "    source: $FRIGID_DIR"

if [[ ! -f "$FRIGID_DIR/ms-pred/setup.py" && ! -f "$FRIGID_DIR/ms-pred/pyproject.toml" ]]; then
    echo "ERROR: ms-pred submodule not initialized inside FRIGID."
    echo "Run: git -C $FRIGID_DIR submodule update --init --recursive"
    exit 1
fi

echo "=== Verifying PyTorch 2.6 and detecting installed CUDA build ==="
$PIXI python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available — check pyproject.toml system requirements'; print('  torch', torch.__version__, '| CUDA', torch.version.cuda, '| GPUs:', torch.cuda.device_count())"

# torch-scatter/torch-sparse wheels are tagged by CUDA toolkit version and must match
# the *installed* torch build exactly (e.g. cu124 vs cu126) or the compiled extension
# fails at import with an ABI mismatch (undefined symbol from libtorch).
CUDA_TAG=$($PIXI python -c "import torch; print('cu' + torch.version.cuda.replace('.', ''))")
echo "=== Installing torch-scatter and torch-sparse (torch-2.6+${CUDA_TAG} wheel index) ==="
$PIXI python -m pip install torch-scatter torch-sparse \
    --find-links "https://data.pyg.org/whl/torch-2.6.0+${CUDA_TAG}.html"

echo "=== Installing DGL (torch-2.6 wheel index) ==="
$PIXI python -m pip install dgl \
    --find-links https://data.dgl.ai/wheels/torch-2.6/repo.html

echo "=== Installing Cython (required by ms-pred build) ==="
$PIXI python -m pip install "cython<3"

echo "=== Installing ms-pred-dev (from configs/default.yaml models.simulator.ms_pred_src) ==="
MS_PRED_DIR=$(python3 -c "
import yaml, os
cfg = yaml.safe_load(open('configs/default.yaml'))
p = cfg['models']['simulator']['ms_pred_src']
print(os.path.expanduser(p))
")

if [[ ! -d "$MS_PRED_DIR" ]]; then
    echo "ERROR: ms_pred_src not found: $MS_PRED_DIR"
    echo "Set models.simulator.ms_pred_src in configs/default.yaml"
    exit 1
fi

echo "    ms-pred-dev source: $MS_PRED_DIR"

# ponytail: same patches as simulator-iceberg — algos2.pyx long→int, missing __init__.py
sed -i 's/\.astype(long,/.astype(int,/g' \
    "$MS_PRED_DIR/src/ms_pred/massformer_pred/massformer_code/algos2.pyx" 2>/dev/null || true

find "$MS_PRED_DIR/src/ms_pred" -type d | while read -r d; do
    touch "$d/__init__.py"
done

$PIXI python -m pip install -r "$MS_PRED_DIR/requirements.txt"
$PIXI python -m pip install ray==2.7.2
$PIXI python -m pip install --no-build-isolation -e "$MS_PRED_DIR"

echo "=== Patching FRIGID's ms-pred submodule to use ms-pred-dev ==="
# FRIGID's bundled ms-pred is older (no CompositeMassSpec); replace with ms-pred-dev.
# spec2mol_scaling.py inserts ms-pred/src at sys.path[0] at import time, so PYTHONPATH
# alone cannot override it — redirect the package folder instead.
FRIGID_MS_PRED="$FRIGID_DIR/ms-pred/src/ms_pred"
if [[ -d "$FRIGID_MS_PRED" && ! -L "$FRIGID_MS_PRED" ]]; then
    mv "$FRIGID_MS_PRED" "${FRIGID_MS_PRED}_orig_backup"
    ln -s "$MS_PRED_DIR/src/ms_pred" "$FRIGID_MS_PRED"
    echo "    Symlinked $FRIGID_MS_PRED -> $MS_PRED_DIR/src/ms_pred"
elif [[ -L "$FRIGID_MS_PRED" ]]; then
    echo "    Already symlinked, skipping"
fi

echo "=== Installing FRIGID ==="
$PIXI python -m pip install -e "$FRIGID_DIR"

echo "=== Installing ngboost (required by FRIGID's token length model) ==="
$PIXI python -m pip install ngboost

echo "=== Patching safe-mol (0.1.13 incompatible with transformers>=4.40) ==="
$PIXI python -m pip install "safe-mol==0.1.14"

echo "=== Done. Verify with: ==="
echo "    pixi run --environment $ENV python -c 'import dlm; import mist; import torch; print(\"OK, torch\", torch.__version__)'"
