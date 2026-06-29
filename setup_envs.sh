#!/usr/bin/env bash
# Run all per-skill environment setup scripts after `pixi install`.
# Add a new entry here whenever a skill gains a setup_env.sh.

set -euo pipefail

SKILL_DIR=".agents/skills"

run_if_exists() {
    local script="$SKILL_DIR/$1/scripts/setup_env.sh"
    if [[ -f "$script" ]]; then
        echo ">>> $1"
        bash "$script"
    fi
}

pixi install

run_if_exists msms-sim-iceberg
run_if_exists msms-retrieval
run_if_exists msms-denovo
run_if_exists msms-preprocess

echo ""
echo "All environments set up."
