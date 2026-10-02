---
name: msms-mist-fingerprint
description: Predict molecular fingerprints from MS/MS spectra using only FRIGID's MIST encoder, skipping diffusion generation and ICEBERG refinement.
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

Same as `msms-denovo`: clone [coleygroup/FRIGID](https://github.com/coleygroup/FRIGID) with submodules if needed, read its `README.md` **Installation** section, and follow that checkout's environment instructions. Set `mist_ckpt` in `configs/default.yaml`.

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

- **Environment**: `denovo` (`python ...`)
- **MIST atom support**: only `C, O, P, N, S, Cl, F, H` — molecules with any
  other element (Br, I, Si, metals, etc.) are silently dropped by FRIGID's
  data loader; this skill detects and logs those as failures
- **Encoder config**: `hidden_size` and `magma_modulo` are read from the
  checkpoint's magma head, so MSG (`640`, `2048`) and CANOPUS (`512`, `512`)
  checkpoints both load. `msms-denovo` runs FRIGID's CANOPUS config, so the
  shared `mist_ckpt` should be the CANOPUS checkpoint when both skills are used.
- **One unknown spectrum**: `--spectrum x.ms --formula F` replaces `--ms-dir`
  and `--formulae-csv`; `--frigid-dir`, `--mist-ckpt` and `--cuda-devices`
  override the config.
- **Fingerprint format**: 4096-bit, radius-2 Morgan-equivalent (matches
  FRIGID's `fingerprint.bits`/`fingerprint.radius` defaults), threshold 0.172
  for binarization (FRIGID default).

## References

- Bohde, M. et al., "FRIGID: Scaling Diffusion-Based Molecular Generation from Mass Spectra at Training and Inference Time", 2025. [github.com/coleygroup/FRIGID](https://github.com/coleygroup/FRIGID)
- Goldman, S. et al., "Prefix-tree decoding for predicting mass spectra from molecules" (MIST), NeurIPS 2023.

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
