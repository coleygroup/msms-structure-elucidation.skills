#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
bash "$ROOT/setup_envs.sh"
printf 'Provide ICEBERG weights via --gen-ckpt and --inten-ckpt. Public MassSpecGym weights: https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym\n'
