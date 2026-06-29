---
name: msms-denovo
description: Generate candidate structures de novo from an experimental MS/MS spectrum using FRIGID, a masked diffusion language model with optional ICEBERG-guided refinement.
category: denovo
---

# msms-denovo

## Goal

Given an experimental MS/MS spectrum in ms-pred `.ms` format, generate a ranked list of candidate molecular SMILES using FRIGID. FRIGID first generates candidates via masked diffusion conditioned on a predicted Morgan fingerprint (MIST encoder) and the molecular formula, then optionally refines them iteratively using ICEBERG-simulated spectra (inference-time scaling).

## When to Use

- An unknown compound must be elucidated without a reference database.
- Retrieval (`msms-retrieval`) returned no confident match.
- Cascade mode has exhausted upstream models.

## When NOT to Use

- The compound is likely in a spectral database — try `msms-retrieval` first.
- The molecular formula is completely unknown — FRIGID requires a formula or formula prediction.
- The spectrum is not in `.ms` format — convert with `msms-preprocess` first.

## Prerequisites

### 1 — Clone FRIGID (one-time per machine)

```bash
git clone --recurse-submodules https://github.com/coleygroup/FRIGID /path/to/FRIGID
```

Set the clone path in `configs/default.yaml`:

```yaml
models:
  denovo:
    frigid_src: "/path/to/FRIGID"
```

### 2 — Install the environment (one-time per machine)

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

Verify:

```bash
pixi run --environment denovo python -c "import dlm; import mist; import torch; print('OK, torch', torch.__version__)"
```

### 3 — Set checkpoint paths in `configs/default.yaml`

Download `frigid_pretrained_checkpoints.tar.gz` from [Zenodo record 19685145](https://zenodo.org/records/19685145) and place checkpoints at:

```
checkpoints/
  frigid/DLM.ckpt
  mist/mist_canopus.pt
  iceberg/iceberg_canopus_model1.ckpt
  iceberg/iceberg_canopus_model2.ckpt
```

The config should point to:

```yaml
models:
  denovo:
    dlm_ckpt: "checkpoints/frigid/DLM.ckpt"
    mist_ckpt: "checkpoints/mist/mist_canopus.pt"
    iceberg_gen_ckpt: "checkpoints/iceberg/iceberg_canopus_model1.ckpt"
    iceberg_inten_ckpt: "checkpoints/iceberg/iceberg_canopus_model2.ckpt"
```

Note: The ICEBERG checkpoints bundled with FRIGID are used for inference-time refinement and are separate from the ICEBERG simulator skill's checkpoints.

## Instructions

### Step 1 — Ensure spectrum is in `.ms` format

If starting from mzML or MGF, convert first with `msms-preprocess`. The `.ms` format is ms-pred's native format (see `data/exp_specs/` in ms-pred-dev for examples).

### Step 2 — Assign per-peak subformulae

FRIGID's MIST encoder requires peak-formula assignments. Run the `msms-subformulae` skill first:

```bash
pixi run --environment denovo python .agents/skills/msms-subformulae/scripts/run.py \
    --spectrum results/sample.ms \
    --formula C12H17NO3 \
    --adduct "[M+H]+" \
    --output-dir results/subformulae/
```

### Step 3 — Run de novo prediction

```bash
pixi run --environment denovo python .agents/skills/msms-denovo/scripts/run.py \
    --spectrum results/sample.ms \
    --formula C12H17NO3 \
    --adduct "[M+H]+" \
    --subform-dir results/subformulae/ \
    --top-k 10 \
    --output results/denovo_output.json
```

**Key parameters:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--formula` | Molecular formula of the precursor | required |
| `--adduct` | Ionization adduct | `[M+H]+` |
| `--subform-dir` | Output dir from msms-subformulae | required |
| `--instrument` | Instrument type | `Orbitrap` |
| `--top-k` | Candidates to return | `10` |
| `--num-rounds` | ICEBERG refinement rounds (`0` = FRIGID-base only) | from config (`2`) |
| `--batch-size` | Samples per round | from config (`128`) |

### Step 4 — Inspect output

`results/denovo_output.json`:

```json
{
  "query_spectrum": "results/sample.ms",
  "formula": "C12H17NO3",
  "adduct": "[M+H]+",
  "top_k": 10,
  "num_rounds": 10,
  "candidates": [
    {"smiles": "CC(=O)Oc1ccccc1C(=O)OCCN", "score": 0.82},
    ...
  ]
}
```

Candidates are sorted by ICEBERG-refined fingerprint similarity (Tanimoto) descending.

## How FRIGID Works

1. **MIST encoder** — predicts a 4096-bit Morgan fingerprint from the experimental spectrum and peak-formula pairs.
2. **FRIGID-base** — masked diffusion language model generates candidate SMILES (as SAFE sequences) conditioned on the predicted fingerprint and molecular formula.
3. **ICEBERG refinement** (when `--num_rounds > 0`) — for each round: simulate spectra for top-K candidates with ICEBERG, score hallucinated peaks, mask responsible tokens, regenerate. Iterates R rounds.

## Constraints

- **Environment**: `denovo` (`pixi run --environment denovo python ...`)
- **Python**: 3.10 (required by FRIGID and its ms-pred submodule)
- **Input format**: ms-pred `.ms` format only
- **Formula required**: FRIGID conditions on molecular formula; use BUDDY or formula prediction upstream if unknown
- **Checkpoints required**: script raises `FileNotFoundError` if any checkpoint path is missing
- **GPU recommended**: ICEBERG refinement is slow on CPU; set `cuda_devices` in config

## References

- Bohde, M. et al., "FRIGID: Scaling Diffusion-Based Molecular Generation from Mass Spectra at Training and Inference Time", 2025. [github.com/coleygroup/FRIGID](https://github.com/coleygroup/FRIGID)
- Alberts, M. et al., "Artificial intelligence for context-aware mass spectrometry", *Nature Methods*, 2025. [DOI:10.1038/s41592-025-02658-z](https://doi.org/10.1038/s41592-025-02658-z)

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
