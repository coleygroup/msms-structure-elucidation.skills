#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
bash "$ROOT/setup_envs.sh"
printf 'Configure GLACIER --checkpoint or ICEBERG --gen-checkpoint and --inten-checkpoint in configs/local.yaml or the prediction launcher. Public MassSpecGym weights: https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym\n'
