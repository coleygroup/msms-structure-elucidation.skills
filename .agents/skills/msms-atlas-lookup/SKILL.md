---
name: msms-atlas-lookup
description: Batch-retrieve precomputed ICEBERG MS/MS spectra for a list of SMILES from the NIST23 spectral atlas, no model inference required.
category: retrieval
---

# msms-atlas-lookup

## Goal

Given a CSV of SMILES, look up predicted MS/MS spectra from the precomputed
NIST23 ICEBERG atlas (the same MGF library backing the internal ms-pred
webui's `/api/query_smiles` endpoint) and save results to a single HDF5 file
with SMILES, InChIKey14, formula, collision energy, and per-peak m/z/intensity.
Molecules absent from the atlas are recorded in a failure log — run
`msms-sim-iceberg` for those individually if a prediction is still needed.

## When to Use

- A large batch of SMILES needs predicted spectra and many are likely to
  already be NIST23 structures (e.g. reagents, common impurities, USPTO
  building blocks).
- Speed matters — this is an indexed file lookup (O(1) per molecule via the
  `.mgf.idx` sidecar), not GPU inference.

## When NOT to Use

- The compound is novel / unlikely to be in NIST23 — use `msms-sim-iceberg`
  directly instead of waiting on atlas misses.
- An experimental spectrum already exists — no prediction is needed at all.

## Prerequisites

### 1 — Set the atlas path in `configs/default.yaml`

```yaml
models:
  atlas:
    atlas_dir: "/mnt/iceberg_atlas"   # NFS mount of iceberg-ms.mit.edu:/home/coley-group/atlas
```

This must contain `h_plus_out_mgf/` and/or `h_minus_out_mgf/`, each holding
per-formula `.mgf` files (optionally with `.mgf.idx` sidecars for fast lookup).

### 2 — Environment

Uses the `retrieval` pixi env (rdkit, numpy, h5py — no GPU, no torch).

## Instructions

### Step 1 — Run the batch lookup

```bash
# Env: retrieval
python .agents/skills/msms-atlas-lookup/scripts/run.py \
    --input-csv /home/datashare/impurities/uspto/uspto_products.csv \
    --smiles-col smiles \
    --adduct "[M+H]+" \
    --output results/atlas_lookup/spectra.hdf5 \
    --failure-log results/atlas_lookup/failures.log
```

**Key parameters:**
- `--smiles-col` — CSV column name holding SMILES (default `smiles`)
- `--adduct` — `[M+H]+` or `[M-H]-`
- `--limit` — only process the first N rows (useful for a quick test pass)

**Outputs:**

| File | Description |
|------|-------------|
| `spectra.hdf5` | Datasets: `smiles`, `inchikey14`, `formula`, `collision_energy`, `mz` (vlen float64), `intensity` (vlen float64), one row per matched (structure, CE) block |
| `failures.log` | Tab-separated `row_index\tsmiles\treason` for every SMILES not found in the atlas |

### Step 2 — Handle misses

Anything in `failures.log` is either an invalid SMILES or a structure not in
NIST23 — run `msms-sim-iceberg` on those to get predicted spectra instead.

## Constraints

- **Environment**: `retrieval` (`pixi run --environment retrieval python ...`)
- **Atlas coverage**: only structures present in NIST23 will be found; this
  is a lookup, not a predictor — coverage is bounded by what's in the atlas
- **InChIKey matching**: matches on the 2D connectivity layer (first 14
  chars), so stereo-unspecified input can still match stereo-specific
  atlas entries
- **No `.idx` sidecar**: falls back silently to returning no blocks for that
  formula file rather than a full scan; regenerate sidecars upstream if a
  formula file is missing its `.idx`

## References

- Atlas and lookup logic adapted from `webui/app.py` in [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred)

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
