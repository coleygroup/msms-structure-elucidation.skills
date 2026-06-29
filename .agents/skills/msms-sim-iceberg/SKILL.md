---
name: msms-sim-iceberg
description: Predict LC-MS/MS spectra from SMILES using ICEBERG, a two-stage DAG + intensity GNN. Outputs predicted m/z vs intensity spectrum, fragment ion SMILES per peak, and a spectrum plot.
category: simulation
---

# msms-sim-iceberg

## Goal

Given a SMILES string and instrument parameters, predict the LC-MS/MS (MS2) spectrum using ICEBERG — a two-stage GNN that first generates a fragmentation DAG (fragment ions) then predicts their intensities.

## When to Use

- A SMILES is known and a predicted spectrum is needed for candidate confirmation or comparison.
- Fragment ion assignments (SMILES per peak) are required for structural interpretation.
- No reference spectrum exists in the database (complement with `msms-retrieval`).

## When NOT to Use

- **Experimental spectrum already available** — use it directly.
- **GC-MS or other MS types** — ICEBERG is trained on LC-MS/MS only.
- **MW > 1000 Da or organometallics** — predictions unreliable or may fail.

## Prerequisites

### 1 — Install the environment (one-time per machine)

`ms_pred`, DGL, and torch-scatter/sparse are not on standard package indexes and must be installed manually after `pixi install`:

```bash
bash .agents/skills/msms-sim-iceberg/scripts/setup_env.sh
```

Verify:
```bash
pixi run --environment simulator-iceberg python -c "import ms_pred; import dgl; print('OK')"
```

### 2 — Set checkpoint paths in `configs/default.yaml`

```yaml
models:
  simulator:
    gen_ckpt: "/absolute/path/to/gen_best.ckpt"
    inten_ckpt: "/absolute/path/to/inten_best.ckpt"
```

Checkpoints are available from colleagues or from [coleygroup/ms-pred releases](https://github.com/coleygroup/ms-pred).

## Instructions

### Step 1 — Simulate a spectrum (CLI)

```bash
pixi run snowmageddon simulate --smiles "c1ccccc1C(=O)OCCN"
```

Checkpoint paths, collision energies, adduct, and instrument are read from `configs/default.yaml`. Override per-run with `--gen-ckpt` / `--inten-ckpt`.

### Step 1b — Run the script directly (advanced)

```bash
# Env: simulator-iceberg
python .agents/skills/msms-sim-iceberg/scripts/predict_msms.py \
    --smiles "c1ccccc1C(=O)OCCN" \
    --gen_ckpt /path/to/gen_best.ckpt \
    --inten_ckpt /path/to/inten_best.ckpt \
    --collision_energies 20 40 \
    --adduct "[M+H]+" \
    --instrument "Orbitrap" \
    --output_dir results/iceberg_prediction
```

**Key parameters:**
- `--collision_energies` — one or more CE values in eV (model trained on absolute eV)
- `--adduct` — supported: `[M+H]+`, `[M-H]-`, `[M+Na]+`, `[M+NH4]+`
- `--instrument` — `"Orbitrap"` or `"QTOF"`
- `--threshold` — DAG generator confidence cutoff (default `0.1`; lower = more fragments)
- `--sparse_k` — max peaks returned (default `100`)
- `--cuda_devices` — GPU IDs e.g. `"0"`; omit for CPU

**Outputs written to `--output_dir`:**

| File | Description |
|------|-------------|
| `spectrum.png` | Stem plot, one panel per collision energy |
| `fragments.json` | `{ce: [{mz, intensity, fragment_smiles}]}` sorted by intensity |
| `input_configs.yaml` | All run parameters |

### Step 2 — Inspect fragment assignments (optional)

`fragments.json` maps each peak to the fragment ion SMILES responsible for it, useful for structural rationalization.

## Examples

### 2-Aminoethyl benzoate

```bash
# Env: simulator-iceberg
python .agents/skills/msms-sim-iceberg/examples/example-2-aminoethyl-benzoate/run_example.py \
    --gen_ckpt downloads/iceberg_dag_gen_msg_best.ckpt \
    --inten_ckpt downloads/iceberg_dag_inten_msg_best.ckpt
```

Expected: precursor `[M+H]+` ≈ 166.087 Da; two-panel spectrum at 20 and 40 eV.

## Constraints

- **Environment**: `simulator-iceberg` (`pixi run --environment simulator-iceberg python ...`)
- **Python**: 3.10 (required by ms_pred and DGL)
- **Checkpoints required**: script raises `FileNotFoundError` if missing
- **Single-compound inference**: one SMILES per call; loop externally for batches
- **Unsupported elements**: metals, lanthanides, rare main-group elements may fail

## Known ms-pred quirks (patched by `setup_env.sh`)

These issues exist in the upstream repo and are patched automatically during install:

| Issue | File | Fix |
|-------|------|-----|
| Cython 3 removed `long` builtin | `massformer_pred/massformer_code/algos2.pyx` | Replace `.astype(long,` with `.astype(int,` |
| Subpackages missing `__init__.py` | All subdirs under `src/ms_pred/` | `touch` each missing `__init__.py` before install |
| `predict_smis.py` called with relative path, no `cwd` | `dag_pred/iceberg_elucidation.py` line ~332 | Add `cwd=Path(__file__).parent.parent.parent.parent` to the `subprocess.run` call |

**Fragment SMILES are not stored in HDF5.** They must be reconstructed at load time:
build a `FragmentEngine(smiles)` from the parent molecule, then call `engine.get_present_atoms(int_frag)` to get atom indices, then extract the substructure with RDKit atom removal. This is done in `predict_msms.py:_int_frag_to_smiles`.

**`preds.hdf5` is written to `~/.cache/ms-pred/iceberg-elucidation/<hash>/`**, not to `--output_dir`. The hash is deterministic — same inputs skip recomputation. The `save_dir` returned by `iceberg_prediction()` points to this cache dir and must be passed to `load_pred_spec`.

**Always use `python -m pip`** inside pixi envs — bare `pip` resolves to whatever is first on PATH (often a system or conda pip) and installs into the wrong Python.

## References

- Alberts, M. et al., "Artificial intelligence for context-aware mass spectrometry", *Nature Methods*, 2025. [DOI:10.1038/s41592-025-02658-z](https://doi.org/10.1038/s41592-025-02658-z)
- Source code: [github.com/coleygroup/ms-pred](https://github.com/coleygroup/ms-pred)

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
