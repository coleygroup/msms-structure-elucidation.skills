---
trigger: always_on
---

# Pixi Environment Rules

All model execution uses isolated pixi environments defined in `pyproject.toml`. Activate them with:

```bash
pixi run --environment <env-name> python ...
```

## Environments

| Environment | Purpose | Key deps |
|-------------|---------|----------|
| `default` | Orchestration, CLI, MCP servers, shared utilities | python >=3.11, click, pyyaml, anthropic, mcp |
| `preprocess` | `.raw` → mzML/MGF conversion | pyteomics or msconvert wrapper (TODO) |
| `simulator-iceberg` | ICEBERG MS/MS simulator (ms-pred, coleygroup) | python 3.10, pytorch 2.4, rdkit, DGL |
| `simulator` | Generic simulator placeholder | pytorch, rdkit (TODO) |
| `retrieval` | Spectral database retrieval | faiss, numpy (TODO) |
| `denovo` | De novo structure prediction | pytorch (TODO) |

## Adding Dependencies

Edit the relevant `[tool.pixi.feature.<name>.dependencies]` block in `pyproject.toml`.

For conda-only packages or old Python versions, add them as conda-forge deps in the same feature block:
```toml
[tool.pixi.feature.simulator.dependencies]
python = "3.10.*"
pytorch = {version = "2.4.0", channel = "pytorch"}
rdkit = {version = "*", channel = "conda-forge"}
```

For pip-only packages, add a `[tool.pixi.feature.<name>.pypi-dependencies]` block:
```toml
[tool.pixi.feature.simulator.pypi-dependencies]
some-pip-package = "*"
```

## Install

```bash
pixi install                          # installs all environments
pixi install --environment simulator  # installs one env only
```
