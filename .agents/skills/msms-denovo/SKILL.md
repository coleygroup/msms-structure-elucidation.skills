---
name: msms-denovo
description: Generate candidate structures de novo from an experimental MS/MS spectrum without requiring a reference database.
category: denovo
---

# msms-denovo

## Goal
Given an experimental MS/MS spectrum (mzML or MGF), generate candidate molecular structures de novo using the in-house model. Returns a ranked list of SMILES candidates with confidence scores.

## Instructions

1. **Run de novo prediction**:
```bash
# Env: denovo
python .agents/skills/msms-denovo/scripts/run.py \
    --spectrum results/sample.mzML \
    --top_k 10 \
    --output results/denovo_output.json
```

## Output Format
JSON file with keys:
- `query_spectrum`: path to input spectrum
- `candidates`: list of `{smiles, score}` dicts, sorted by score descending

## Constraints
- **Environment**: `denovo` (`pixi run --environment denovo python ...`)
- **Input**: mzML or MGF spectrum file
- **Checkpoint**: set `models.denovo.checkpoint` in `configs/default.yaml`

## References
- TODO: cite de novo model paper/repo once implemented.

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
