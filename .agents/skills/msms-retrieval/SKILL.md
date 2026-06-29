---
name: msms-retrieval
description: Retrieve candidate structures from a spectral database by matching an experimental MS/MS spectrum.
category: retrieval
---

# msms-retrieval

## Goal
Given an experimental MS/MS spectrum (mzML or MGF), search a spectral database to retrieve the top-N candidate structures ranked by spectral similarity.

## Instructions

1. **Run retrieval**:
```bash
# Env: retrieval
python .agents/skills/msms-retrieval/scripts/run.py \
    --spectrum results/sample.mzML \
    --db configs/default.yaml \
    --top_k 10 \
    --output results/retrieval_output.json
```

## Output Format
JSON file with keys:
- `query_spectrum`: path to input spectrum
- `candidates`: list of `{smiles, score, db_id}` dicts, sorted by score descending

## Constraints
- **Environment**: `retrieval` (`pixi run --environment retrieval python ...`)
- **Input**: mzML or MGF spectrum file
- **Database path**: set `models.retrieval.db_path` in `configs/default.yaml`

## References
- TODO: cite retrieval model/database once implemented.

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
