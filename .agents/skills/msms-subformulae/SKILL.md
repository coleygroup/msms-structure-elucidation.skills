---
name: msms-subformulae
description: Assign per-peak molecular subformulae to an MS/MS spectrum, producing the peakformula features required by FRIGID's MIST encoder.
category: preprocessing
---

# msms-subformulae

## Goal

Given an MS/MS spectrum in ms-pred `.ms` format and the precursor molecular formula, assign candidate fragment formulae to each MS2 peak. Outputs a JSON file consumed by the `msms-denovo` skill (FRIGID).

In de novo mode (structure unknown), pass no `--smiles` — the script uses `--use-all` to enumerate all valid formula subsets from the precursor formula without requiring a known structure.

## When to Use

- Before running `msms-denovo`: FRIGID's MIST encoder requires these peak-formula assignments as input features.
- After `msms-preprocess` has converted the raw spectrum to `.ms` format.

## Prerequisites

Requires the `denovo` environment with FRIGID installed. Run once:
```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

## Instructions

### Step 1 — Assign subformulae (de novo mode, no SMILES needed)

```bash
pixi run --environment denovo python .agents/skills/msms-subformulae/scripts/run.py \
    --spectrum results/sample.ms \
    --formula C12H17NO3 \
    --adduct "[M+H]+" \
    --output-dir results/subformulae/
```

### Step 1b — Assign subformulae (known structure, more precise)

```bash
pixi run --environment denovo python .agents/skills/msms-subformulae/scripts/run.py \
    --spectrum results/sample.ms \
    --formula C12H17NO3 \
    --adduct "[M+H]+" \
    --smiles "CC(=O)Oc1ccccc1C(=O)OCCN" \
    --output-dir results/subformulae/
```

**Key parameters:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--formula` | Precursor molecular formula | required |
| `--adduct` | Ionization adduct | `[M+H]+` |
| `--smiles` | Known SMILES (omit for de novo) | empty |
| `--mass-diff-thresh` | Peak-formula tolerance in ppm | `10.0` |
| `--max-formulae` | Max candidate formulae per peak | `50` |

### Step 2 — Use output in msms-denovo

Pass the output directory to `msms-denovo` via `--subform-dir results/subformulae/`.

## Output

```
results/subformulae/
  default_subformulae/
    <spec_name>.json    ← peak-formula assignments (one JSON per spectrum)
  input_configs.yaml    ← run parameters
```

The JSON has the structure:
```json
{
  "cand_form": "C12H17NO3",
  "cand_ion": "[M+H]+",
  "output_tbl": {
    "mz": [...],
    "ms2_inten": [...],
    "formula": ["", "", ...],
    "ions": ["[M+H]+", ...]
  }
}
```

In de novo mode, `formula` entries are empty strings — this is correct and expected by FRIGID.

## How It Works

Peaks from all collision energies in the `.ms` file are merged into one JSON.

For each peak, the algorithm:
1. Enumerates all formula subsets of the precursor formula (every combination of reducing element counts, e.g. `C8H10N4O2` → `C7H8N4O`, `C6H6N2`, ...)
2. Computes the exact monoisotopic mass of each subset, adjusted for the adduct (e.g. subtract H for `[M+H]+`)
3. Keeps all subsets whose mass falls within `--mass-diff-thresh` ppm of the observed peak m/z

This is **not** a single-formula assignment — each peak receives up to `--max-formulae` candidate formulae. FRIGID uses the full candidate list as input features and learns to reason over the ambiguity internally.

In `--use-all` mode (de novo), all formula subsets are considered. With a known SMILES, MAGMA-based fragmentation in ms-pred can restrict candidates to chemically valid fragments — however, `use_magma` is currently hardcoded to `False`, so both modes use the same combinatorial enumeration.

In de novo mode the `formula` field in the output JSON is left as empty strings — the candidate enumeration happens inside ms-pred but is not propagated to the output. FRIGID only sees the m/z values and intensities.

## Constraints

- **Environment**: `denovo`
- **Input format**: ms-pred `.ms` format only
- **Formula required**: precursor molecular formula must be known or predicted upstream

## References

- Alberts, M. et al., "Artificial intelligence for context-aware mass spectrometry", *Nature Methods*, 2025. [DOI:10.1038/s41592-025-02658-z](https://doi.org/10.1038/s41592-025-02658-z)

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
