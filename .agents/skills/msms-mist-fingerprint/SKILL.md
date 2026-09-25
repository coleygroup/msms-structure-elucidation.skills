---
name: msms-mist-fingerprint
description: Predict molecular fingerprints from MS/MS spectra using only FRIGID's MIST encoder, skipping diffusion generation and ICEBERG refinement.
category: denovo
---

# msms-mist-fingerprint

## Goal

Given spectra in ms-pred `.ms` format with precomputed subformulae, run just
FRIGID's MIST encoder to predict a 4096-bit Morgan-like fingerprint per
spectrum (sigmoid probabilities + thresholded binary). Unlike `msms-denovo`,
this stops after fingerprint prediction — no SMILES generation, no ICEBERG
refinement — useful for benchmarking MIST fingerprint quality directly
against ground-truth Morgan fingerprints.

## When to Use

- Evaluating MIST's fingerprint-prediction accuracy in isolation (e.g.
  Tanimoto similarity vs. true Morgan fingerprints for known structures).
- A fingerprint is needed as a feature/embedding, not a generated structure.

## When NOT to Use

- A candidate structure (SMILES) is the actual goal — use `msms-denovo`
  for the full pipeline.

## Prerequisites

Same as `msms-denovo`: FRIGID cloned + `denovo` env set up
(`.agents/skills/msms-denovo/scripts/setup_env.sh`), `mist_ckpt` set in
`configs/default.yaml`.

Spectra must already be in `.ms` format with subformulae computed —
see `msms-atlas-to-ms` (for atlas-derived spectra) and
`msms-subformulae/scripts/run_batch.py` (batch subformula assignment).

## Instructions

### Step 1 — Run MIST fingerprint prediction

```bash
# Env: denovo
python .agents/skills/msms-mist-fingerprint/scripts/run.py \
    --ms-dir results/atlas_lookup_1k/ms_files \
    --formulae-csv .agents/test/uspto_sample1k.csv \
    --subform-dir results/atlas_lookup_1k/subformulae \
    --adduct "[M+H]+" \
    --instrument "Orbitrap (LCMS)" \
    --output results/atlas_lookup_1k/mist_fingerprints.hdf5 \
    --failure-log results/atlas_lookup_1k/mist_failures.log
```

**Outputs:**

| File | Description |
|------|-------------|
| `mist_fingerprints.hdf5` | Datasets: `inchikey14`, `smiles`, `fp_probs` (float32, sigmoid probabilities), `fp_binary` (uint8, thresholded at 0.172) |
| `mist_failures.log` | Tab-separated `inchikey14\treason` for prediction/loading failures |

## Constraints

- **Environment**: `denovo` (`pixi run --environment denovo python ...`)
- **MIST atom support**: only `C, O, P, N, S, Cl, F, H` — molecules with any
  other element (Br, I, Si, metals, etc.) are silently dropped by FRIGID's
  data loader; this skill detects and logs those as failures
- **Encoder config**: hardcoded to the MSG Large Model dims
  (`hidden_size=640, magma_modulo=2048`), matching `mist_msg.ckpt`. If
  switching to a CANOPUS checkpoint, update these to `hidden_size=512,
  magma_modulo=512` (see FRIGID's `configs/spec2mol_benchmark_canopus.yaml`).
- **Fingerprint format**: 4096-bit, radius-2 Morgan-equivalent (matches
  FRIGID's `fingerprint.bits`/`fingerprint.radius` defaults), threshold 0.172
  for binarization (FRIGID default).

## References

- Bohde, M. et al., "FRIGID: Scaling Diffusion-Based Molecular Generation from Mass Spectra at Training and Inference Time", 2025. [github.com/coleygroup/FRIGID](https://github.com/coleygroup/FRIGID)
- Goldman, S. et al., "Prefix-tree decoding for predicting mass spectra from molecules" (MIST), NeurIPS 2023.

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
