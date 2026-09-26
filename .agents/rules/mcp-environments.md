---
trigger: always_on
---
# Portable Python environments

The CLI and report use the Python standard library. Scientific scoring uses an interpreter with `ms_pred`, `msbuddy`, and RDKit. Pass `--ms-pred-python` or set `MS_PRED_PYTHON`; `bash setup_envs.sh` creates a local venv when needed. Model skills may use independent Python interpreters. Never mutate a user-provided clone or require a particular environment manager.
