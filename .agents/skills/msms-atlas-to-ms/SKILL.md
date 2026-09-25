---
name: msms-atlas-to-ms
description: Convert atlas-lookup spectra (HDF5 from msms-atlas-lookup) into ms-pred's native .ms format, one file per molecule.
category: preprocessing
---

# msms-atlas-to-ms

## Goal

Given the HDF5 output of `msms-atlas-lookup` (per-peak spectra keyed by
SMILES/InChIKey14/formula/collision energy), write one ms-pred `.ms` file per
molecule with all its collision-energy blocks. This makes atlas hits usable
by any skill that expects `.ms` input, e.g. `msms-subformulae` and
`msms-denovo`.

## When to Use

- After running `msms-atlas-lookup`, before running `msms-subformulae` or
  MIST fingerprint prediction on the resulting spectra.

## When NOT to Use

- The spectrum did not come from the atlas (e.g. real experimental data,
  or ICEBERG-simulated) — those have their own conversion paths.

## Prerequisites

Uses the `retrieval` pixi env (rdkit, h5py — no GPU).

## Instructions

### Step 1 — Convert

```bash
# Env: retrieval
python .agents/skills/msms-atlas-to-ms/scripts/run.py \
    --input results/atlas_lookup_1k/spectra.hdf5 \
    --adduct "[M+H]+" \
    --instrument "Orbitrap (LCMS)" \
    --output-dir results/atlas_lookup_1k/ms_files \
    --failure-log results/atlas_lookup_1k/ms_conversion_failures.log
```

**Outputs:**

| File | Description |
|------|-------------|
| `ms_files/<inchikey14>.ms` | One ms-pred `.ms` file per molecule, all CE blocks included |
| `ms_conversion_failures.log` | Tab-separated `inchikey14\tsmiles\treason` for conversion failures |

## Constraints

- **Environment**: `retrieval` (`pixi run --environment retrieval python ...`)
- **Input**: HDF5 must have `smiles`, `inchikey14`, `formula`,
  `collision_energy`, `mz`, `intensity` datasets (as produced by
  `msms-atlas-lookup`)
- **Parent mass**: computed as neutral exact mass ± proton mass for the
  given adduct (`[M+H]+` / `[M-H]-` only)

## References

- `.ms` format: [github.com/coleygroup/ms-pred](https://github.com/coleygroup/ms-pred), `data/exp_specs/` examples

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
