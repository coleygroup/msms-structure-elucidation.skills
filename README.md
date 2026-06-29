# snowmageddon

Combining the Coley group's elucidation suite into an LLM-orchestrated workflow.

Three in-house models — ICEBERG simulator, spectral database retrieval, de novo prediction — orchestrated in parallel or cascade mode by a Claude agent.

## Setup

### Environments

```bash
bash setup_envs.sh   # pixi install + all per-skill post-install steps
```

### Checkpoints

Download model weights and update paths in `configs/default.yaml`.

| Model | Source |
|-------|--------|
| ICEBERG (simulator) | [coleygroup/ms-pred releases](https://github.com/coleygroup/ms-pred) — `gen_ckpt` and `inten_ckpt` |
| GLACIER (simulator) | [coleygroup/ms-pred releases](https://github.com/coleygroup/ms-pred) — `gen_ckpt` and `inten_ckpt` |
| Retrieval | TODO |
| FRIGID (de novo) | [coleygroup/FRIGID releases](https://github.com/coleygroup/FRIGID) |

curl -L "https://zenodo.org/records/19685145/files/frigid_pretrained_checkpoints.tar.gz?download=1" -o checkpoints/frigid_pretrained_checkpoints.tar.gz

### Dev setup (pre-commit hooks)

```bash
pixi run pre-commit install
```

Ruff (lint + format) and nbstripout run automatically on every commit.
To run manually: `pixi run pre-commit run --all-files`

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
