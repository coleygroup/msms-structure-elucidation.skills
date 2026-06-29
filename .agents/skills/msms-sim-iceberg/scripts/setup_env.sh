#!/usr/bin/env bash
# One-time setup for the simulator-iceberg pixi environment.
# Run from the project root:
#   bash .agents/skills/msms-sim-iceberg/scripts/setup_env.sh
#
# Safe to re-run — pip skips packages that are already installed.

set -euo pipefail

ENV="simulator-iceberg"
PIXI="pixi run --environment $ENV"

echo "=== Installing DGL (torch-2.4 wheel index) ==="
$PIXI python -m pip install dgl \
    --find-links https://data.dgl.ai/wheels/torch-2.4/repo.html

echo "=== Installing torch-scatter and torch-sparse (torch-2.4 CPU wheel index) ==="
$PIXI python -m pip install torch-scatter torch-sparse \
    --find-links https://data.pyg.org/whl/torch-2.4.0+cpu.html
# ponytail: GPU users should replace +cpu with +cu121 or +cu124 to match their CUDA version

echo "=== Installing ms-pred (editable install from local clone) ==="
CONFIG="$(dirname "$0")/../../../../configs/default.yaml"
MS_PRED_DIR=$(python3 -c "
import yaml, os, sys
cfg = yaml.safe_load(open('$CONFIG'))
p = cfg['models']['simulator']['ms_pred_src']
print(os.path.expanduser(p))
")

if [[ ! -d "$MS_PRED_DIR" ]]; then
    echo "ERROR: ms_pred_src not found: $MS_PRED_DIR"
    echo "Clone ms-pred and set models.simulator.ms_pred_src in configs/default.yaml"
    exit 1
fi

echo "    source: $MS_PRED_DIR"

# algos2.pyx uses `long` which was removed in Cython 3; replace all occurrences with int
sed -i 's/\.astype(long,/.astype(int,/g' \
    "$MS_PRED_DIR/src/ms_pred/massformer_pred/massformer_code/algos2.pyx"

# iceberg_elucidation.py calls predict_smis.py with a relative path and no cwd,
# so it resolves against whatever the caller's cwd is. Fix: run from ms-pred root.
sed -i 's/run_result = subprocess.run(cmd, shell=True)$/ms_pred_root = Path(__file__).parent.parent.parent.parent\n        run_result = subprocess.run(cmd, shell=True, cwd=ms_pred_root)/' \
    "$MS_PRED_DIR/src/ms_pred/dag_pred/iceberg_elucidation.py"

# dag_pred and other subpackages are missing __init__.py so find_packages skips them
find "$MS_PRED_DIR/src/ms_pred" -type d | while read -r d; do
    touch "$d/__init__.py"
done

# Editable install so iceberg_elucidation.py can find its sibling scripts at runtime
$PIXI python -m pip install --no-build-isolation -e "$MS_PRED_DIR"

echo "=== Done. Verify with: ==="
echo "    pixi run --environment $ENV python -c 'import ms_pred; import dgl; print(\"OK\")'"
