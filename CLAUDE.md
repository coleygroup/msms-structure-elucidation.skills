---
description:
alwaysApply: true
---

# Snowmageddon Agent Instructions

You are an MS/MS structure elucidation agent with access to three in-house models (simulator, retrieval, de novo) and a set of skills and workflows.

## Project Rules

**Read these rules files at the start of every conversation:**
- `.agents/rules/research-standards.md` — elucidation protocol and intent classification
- `.agents/rules/coding-standards.md` — coding rules and environment management
- `.agents/rules/mcp-environments.md` — pixi environment mapping

@.agents/rules/coding-standards.md
@.agents/rules/mcp-environments.md
@.agents/rules/research-standards.md

**Read on demand:**
- `.agents/rules/skill-standards.md` — for creating or editing a skill
- `.agents/rules/workflow-standards.md` — for creating or editing a workflow
- `.agents/rules/plot-standards.md` — for creating or editing a plotting script
- `.agents/rules/release-standards.md` — for preparing a release tag

## Framework Overview

- **Skills** (`.agents/skills/`): focused tasks — preprocessing, simulation, retrieval, de novo prediction. Each has a `SKILL.md` with step-by-step instructions.
- **Workflows** (`.agents/workflows/`): high-level campaigns that chain skills. Start here.

## Skill Discovery

```bash
grep -r "^description:" .agents/skills/*/SKILL.md
```

## Executing Skills

All skill scripts have `# Env: <pixi-env-name>` annotations. Run with:
```bash
pixi run --environment <env-name> python .agents/skills/<skill>/scripts/run.py [args]
```

## Project Layout

```
.agents/skills/
  msms-preprocess/    # .raw/.d → mzML/MGF
  msms-sim-iceberg/   # predict spectrum from SMILES (ICEBERG)
  msms-retrieval/     # match spectrum against DB
  msms-denovo/        # generate structures de novo
.agents/workflows/
  msms-elucidation.md
src/snowmageddon/
  cli.py              # click CLI
configs/default.yaml  # mode, checkpoints, env names
pyproject.toml        # pixi env definitions
```

## Quick Start

```bash
pixi install
snowmageddon run --input sample.raw --output-dir results/
snowmageddon run --input sample.mzML --mode cascade
```
