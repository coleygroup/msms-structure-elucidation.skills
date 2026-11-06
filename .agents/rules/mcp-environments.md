---
trigger: always_on
---
# Portable Python environments

The CLI and report use the Python standard library. Scientific scoring uses an interpreter with `ms_pred`, `msbuddy`, and RDKit. Pass `--ms-pred-python` or set `MS_PRED_PYTHON`; `bash setup_envs.sh` creates a local venv when needed. Model skills may use independent Python interpreters. Never mutate a user-provided clone or require a particular environment manager.

The MCP server (`python -m msms_structure_elucidation.mcp`) needs only `mcp` and this package; install them in a small venv such as `.cache/mcp-venv`. It reaches the model environments only through the interpreter paths in `configs/local.yaml` (`models.simulator.python`, `models.denovo.frigid_python`). Host names, scheduler partitions, GPU types and paths belong in that uncommitted file, never in code, skills or `configs/default.yaml`.

With a prediction pool, place whole requests on workers. The coordinator needs setup-verified model hashes, not scientific interpreters or model files. Each SSH worker performs CPU preparation, GPU prediction, CPU cache publication/scoring and viewer structure rendering locally. Slurm/Kubernetes stages stay on their cluster's shared storage. Transfer final results and renderings, retaining shared prediction caches on the worker. Keep native HDF5 on that worker until validated cache publication, then remove successful intermediates under the configured cleanup policy. A viewer with worker-rendered structures needs only the package and Flask; older results without those renderings still require the scientific environment.
