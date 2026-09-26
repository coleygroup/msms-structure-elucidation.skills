---
name: msms-fingerprint-compare
description: Compare MIST-predicted fingerprints against deterministic Morgan fingerprints for the same molecules via Tanimoto similarity.
---

# msms-fingerprint-compare

## Goal

Given a CSV of ground-truth SMILES and an HDF5 of MIST-predicted fingerprints
(from `msms-mist-fingerprint`), compute the deterministic Morgan fingerprint
for each molecule and report per-molecule Tanimoto similarity plus summary
statistics, to evaluate MIST's fingerprint-prediction accuracy.

## When to Use

- After running `msms-mist-fingerprint`, to quantify how close its predicted
  fingerprints are to true structural fingerprints.

## When NOT to Use

- No MIST predictions exist yet — run `msms-mist-fingerprint` first.

## Prerequisites

Uses the `retrieval` Python env (rdkit, h5py, numpy — no GPU).

## Instructions

### Step 1 — Compare

```bash
# Env: retrieval
python .agents/skills/msms-fingerprint-compare/scripts/run.py \
    --smiles-csv .agents/test/uspto_sample1k.csv \
    --mist-fingerprints results/atlas_lookup_1k/mist_fingerprints.hdf5 \
    --output results/atlas_lookup_1k/fingerprint_comparison.csv \
    --failure-log results/atlas_lookup_1k/fingerprint_compare_failures.log
```

**Outputs:**

| File | Description |
|------|-------------|
| `fingerprint_comparison.csv` | `inchikey14, smiles, tanimoto_mist_vs_morgan`, one row per compared molecule |
| `fingerprint_compare_failures.log` | Tab-separated failures (missing SMILES lookup, invalid SMILES) |

Summary statistics (mean/median/min/max Tanimoto) are printed to stdout.

## Constraints

- **Environment**: `retrieval` (`python ...`)
- **Fingerprint parameters**: 4096-bit, radius-2 Morgan — matches FRIGID's
  `fingerprint.bits`/`fingerprint.radius` defaults so both sides are
  comparable
- **Matching key**: InChIKey14 (2D connectivity layer), same as
  `msms-atlas-lookup`

## References

- Rogers, D. & Hahn, M., "Extended-Connectivity Fingerprints", J. Chem. Inf. Model., 2010.

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
