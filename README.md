# snowmageddon

Combining the Coley group's elucidation suite into an LLM-orchestrated workflow.

Three in-house models — ICEBERG simulator, spectral database retrieval, de novo prediction — orchestrated in parallel or cascade mode by a Claude agent.

## Setup

```bash
# Install all pixi environments
pixi install

# Or install a specific env only
pixi install --environment simulator-iceberg
```

### Dev setup (pre-commit hooks)

```bash
pixi install                          # installs pre-commit into the default env
pixi run pre-commit install           # wires hooks into .git/hooks/
```

After this, ruff (lint + format) and nbstripout run automatically on every commit.
To run all hooks manually: `pixi run pre-commit run --all-files`

### Before pushing (CI checks)

CI runs `ruff check` and `ruff format --check` on `src/`. Pre-commit handles both
automatically on every commit. To run manually:

```bash
pixi run pre-commit run --all-files   # lint + format everything
# or individually:
pixi run ruff check src/
pixi run ruff format src/
```


### ICEBERG checkpoints

Download from [coleygroup/ms-pred releases](https://github.com/coleygroup/ms-pred) and place in `downloads/`:

```
downloads/
├── iceberg_dag_gen_msg_best.ckpt
└── iceberg_dag_inten_msg_best.ckpt
```

DGL and `ms_pred` must be installed manually after `pixi install --environment simulator-iceberg`:

```bash
pixi run --environment simulator-iceberg \
  pip install dgl --find-links https://data.dgl.ai/wheels/torch-2.4/repo.html
pixi run --environment simulator-iceberg \
  pip install torch-scatter torch-sparse \
    --find-links https://data.pyg.org/whl/torch-2.4.0+cpu.html
pixi run --environment simulator-iceberg \
  pip install git+https://github.com/coleygroup/ms-pred
```

## Usage

```bash
# Run elucidation (parallel mode, auto-preprocesses .raw)
snowmageddon run --input sample.raw --output-dir results/

# Cascade mode
snowmageddon run --input sample.mzML --mode cascade
```

## Project Layout

```
.agents/skills/
  msms-preprocess/        # .raw/.d → mzML/MGF
  msms-sim-iceberg/       # ICEBERG simulator (working)
  msms-simulator/         # generic simulator stub (TODO)
  msms-retrieval/         # spectral DB retrieval stub (TODO)
  msms-denovo/            # de novo prediction stub (TODO)
.agents/workflows/
  msms-elucidation.md     # full elucidation workflow
src/snowmageddon/
  cli.py                  # click CLI
configs/default.yaml      # mode, checkpoints, env names
pyproject.toml            # pixi environment definitions
```

## Implementing a New Model

1. Add a `[tool.pixi.feature.<model-name>.dependencies]` block to `pyproject.toml`.
2. Add the env to `[tool.pixi.environments]`.
3. Create a skill under `.agents/skills/msms-<model-name>/` following `.agents/rules/skill-standards.md`.
4. Wire it up in `src/snowmageddon/cli.py`.
