---
name: msms-cross-model-elucidation
description: Chain the GLACIER simulator, JAM fingerprint predictor and FRIGID decoder into a three-stage cascade that turns a list of SMILES into ranked candidate structures with per-stage accuracy metrics.
---

# MS/MS Cross-Model Elucidation

## Goal

Run a three-model cascade over a list of SMILES, restricted to the `[M+H]+`
adduct at a fixed collision-energy grid, and quantify where structural
information is lost between stages.

Each stage consumes only the previous stage's output:

1. **GLACIER** (`ms-pred`, joint fragment + intensity model) simulates a sparse
   MS/MS spectrum per (molecule, collision energy): fragment m/z, relative
   intensities and per-fragment element-count formulas.
2. **JAM** (`MistNet`) encodes that simulated spectrum into a predicted 4096-bit
   Morgan fingerprint (radius 2), binarized at a fixed threshold.
3. **FRIGID** (diffusion language model decoder) decodes JAM's predicted
   fingerprint into candidate SMILES, filtered to the target molecular formula.

Only stage 3 produces structures, so the cascade's answer for each compound is
FRIGID's top-1 candidate. Stage 1 and 2 metrics are diagnostics, not independent
candidate sources — see `.agents/workflows/msms-cross-model-cascade.md`.

## Prerequisites

The three models live in sibling repositories with their own environment
managers and are **not** declared in this project's `pyproject.toml`. Each stage
script shells out to the matching interpreter by absolute path:

| Model | Repository | Interpreter |
| --- | --- | --- |
| GLACIER | `$MS_PRED_DIR` | `$MS_PRED_PYTHON` |
| JAM | `$JAM_DIR` | `$JAM_PYTHON` |
| FRIGID | `$FRIGID_DIR` | `$FRIGID_PYTHON` |

For a missing ms-pred or FRIGID checkout, clone the corresponding official repository ([ms-pred](https://github.com/coleygroup/ms-pred), [FRIGID](https://github.com/coleygroup/FRIGID) with submodules). Read each cloned checkout's `README.md` installation section and follow its environment instructions before setting the interpreter paths above. For existing checkouts, read their own README and verify the environment rather than assuming a named venv is ready.

Download the two remote checkpoints into the gitignored resources directory
(FRIGID's checkpoint is already local at `$FRIGID_CHECKPOINT`):

```bash
# Env: default
python -c "
from huggingface_hub import hf_hub_download
import shutil, pathlib
base = pathlib.Path('.agents/skills/msms-cross-model-elucidation/resources/checkpoints')
for repo, sub, dest in [
    ('CRG-MIT/GLACIER', 'glacier_all_random_unmatched', 'glacier'),
    ('CRG-MIT/MIST-JAM', 'mist_jam_all_maxtrain_random_split', 'jam'),
]:
    (base / dest).mkdir(parents=True, exist_ok=True)
    for name in ('best.ckpt', 'args.yaml'):
        shutil.copyfile(hf_hub_download(repo_id=repo, filename=f'{sub}/{name}'), base / dest / name)
"
```

If `huggingface_hub` is not importable in the `default` env, run the same
snippet with the JAM project's interpreter
(`$JAM_PYTHON`), which ships
it.

## Instructions

### 1. Create a timestamped results directory

```bash
# Env: default
export CASCADE_RESULTS="results/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$CASCADE_RESULTS"
```

### 2. Simulate spectra with GLACIER

Supply collision energies as rounded integer eV. If they come from experimental NCE, confirm the unit with the provider, convert using precursor m/z, and round before setting `--collision-energies`.

Builds `labels.tsv` (one row per molecule, `ionization` fixed to `[M+H]+`,
`collision_energies` as a Python-literal list) and `split.tsv`, runs
`ms_pred.glacier.predict_smis_joint` with `--sparse-out --frag-form-vecs`, then
reads the resulting PredSpecDB HDF5 into one JSON per (molecule, collision
energy).

```bash
# Env: default
python \
    .agents/skills/msms-cross-model-elucidation/scripts/01_simulate_glacier.py \
    --smiles-file data/oprd_experiments_260904/rxn_7/rxn_7.txt \
    --output-dir "$CASCADE_RESULTS/01_glacier" \
    --name-prefix rxn7 \
    --collision-energies 20,40,60 \
    --gpu   # omit to run GLACIER inference on CPU
```

Outputs: `labels.tsv`, `split.tsv`, `glacier_out/preds.hdf5`,
`spectra/<name>_ce<ce>.json`, `manifest.json`, `input_configs.yaml`.

### 3. Predict fingerprints with JAM

Converts each stage-1 spectrum into the subformula-assignment tree
`PeakFormulaFeaturizer` expects (`output_tbl` with `formula`/`ms2_inten`/`ions`,
plus `cand_form` and `cand_ion`), featurizes it, runs `MistNet.encode_spectra`,
binarizes, and scores against an RDKit Morgan fingerprint of the input SMILES.

```bash
# Env: JAM environment (not a msms-structure-elucidation Python env)
$JAM_PYTHON \
    .agents/skills/msms-cross-model-elucidation/scripts/02_predict_jam_fingerprint.py \
    --spectra-dir "$CASCADE_RESULTS/01_glacier/spectra" \
    --manifest "$CASCADE_RESULTS/01_glacier/manifest.json" \
    --output-dir "$CASCADE_RESULTS/02_jam" \
    --threshold 0.15 \
    --device cpu
```

Outputs: `subformulae/<spec>.json`, `predicted_fingerprints.npz`
(`pred_fp_probs`, `pred_fp_bits`), `metrics.json`, `input_configs.yaml`.

### 4. Decode structures with FRIGID

Conditions FRIGID's sampler on JAM's **predicted** fingerprint (not the ground
truth) and filters candidates to the target molecular formula, reusing
`dlm.utils.benchmark_utils.generate_with_formula_filter`,
`build_prediction_entry` and `evaluate_predictions` so metrics match FRIGID's
own `scripts/eval_dlm_pred_fp.py`.

```bash
# Env: external FRIGID conda env (not a msms-structure-elucidation Python env)
$FRIGID_PYTHON \
    .agents/skills/msms-cross-model-elucidation/scripts/03_decode_frigid.py \
    --jam-metrics "$CASCADE_RESULTS/02_jam/metrics.json" \
    --jam-fingerprints "$CASCADE_RESULTS/02_jam/predicted_fingerprints.npz" \
    --output-dir "$CASCADE_RESULTS/03_frigid" \
    --n-required 10   # add --cpu to force CPU generation
```

Outputs: `results.json` (rewritten after every record, so an interrupted run
resumes), `summary.json`, `input_configs.yaml`.

### 5. Build the notebook and report

```bash
# Env: preprocess
python \
    .agents/skills/msms-cross-model-elucidation/scripts/04_build_report.py \
    --results-dir "$CASCADE_RESULTS" \
    --notebook notebooks/msms_cross_model_cascade.ipynb
```

Outputs: an executed `notebooks/msms_cross_model_cascade.ipynb` and
`$CASCADE_RESULTS/report.md`.

### 6. Verify the run

Checks the invariants that span stages — that the spectrum set is complete and
well-formed, that the fingerprint matrix lines up with the scored records, and
that each candidate's ground-truth Tanimoto agrees with the aggregate metric
computed by FRIGID's own code path.

```bash
# Env: preprocess
python \
    .agents/skills/msms-cross-model-elucidation/test_cascade_outputs.py \
    "$CASCADE_RESULTS"
```

## Constraints

- **Environment**: stages 1 and 5 use this project's Python envs (`default` for
  orchestration, `preprocess` for plotting/notebook rendering). Stages 2 and 3
  run under the sibling repositories' own environments and cannot be run from a
  msms-structure-elucidation env.
- **Input format**: a plain text file of SMILES, one per line, no header and no
  index column. Per-molecule ids are generated as `<prefix>_<0-based index>`.
- **Adduct**: `[M+H]+` only. Stage 2's featurizer requires an adduct present in
  `jam.chem.ION_LIST`, and every stage writes that adduct verbatim.
- **Element ordering**: `ms_pred.common.chem_utils.VALID_ELEMENTS` and
  `jam.chem.VALID_ELEMENTS` order elements **differently**. Stage 2 re-emits every
  stage-1 formula through `jam.chem.formula_to_vec`/`vec_to_formula` rather than
  passing the raw string through.
- **GLACIER flags**: `--sparse-out` is required (`run_shard` asserts it) and
  `--frag-form-vecs` must be set to get per-fragment formulas. Never pass
  `--binned-out`, which discards the per-fragment formulas stage 2 needs.
- **Formula filter**: FRIGID is given the ground-truth molecular formula, treated
  as known from the precursor mass. This is an assumption of the protocol, not a
  measurement.
- **Threshold**: JAM's binarization threshold defaults to 0.15 and is not tuned
  on the evaluated molecules. `jam.evaluate` normally selects it by sweeping on a
  validation split; provide `--threshold` if one is available.
- **Two distinct similarities**: FRIGID's sampler ranks candidates by similarity
  to the *conditioning* fingerprint, whereas the reported metrics score them
  against the *ground truth*. `evaluate_predictions` defines top-1 as the
  first-generated candidate, not the best of the ten. Stage 3 therefore stores
  both numbers per candidate (`tanimoto_vs_ground_truth` and
  `similarity_to_conditioning_fp`); never present the latter as accuracy.
- **Determinism**: FRIGID sampling is stochastic, so candidate sets and stage-3
  metrics vary between runs.

## References

- **GLACIER / ms-pred** — joint fragment and intensity MS/MS simulation,
  `coleygroup/ms-pred`. Checkpoint: `CRG-MIT/GLACIER`
  (`glacier_all_random_unmatched`).
- **JAM / MIST** — spectrum-to-fingerprint encoder. Goldman et al., "Annotating
  metabolite mass spectra with domain-inspired chemical formula transformers",
  *Nature Machine Intelligence* 5 (2023) 965–979.
  doi:10.1038/s42256-023-00708-3. Checkpoint: `CRG-MIT/MIST-JAM`
  (`mist_jam_all_maxtrain_random_split`).
- **FRIGID** — masked diffusion language model decoding fingerprints to SAFE
  molecular strings. Checkpoint: `$FRIGID_CHECKPOINT`.
- **SAFE** — Noutahi et al., "Gotta be SAFE: a new framework for molecular
  design", *Digital Discovery* 3 (2024) 796–804. doi:10.1039/D4DD00019F.
- **Morgan fingerprints** — Rogers & Hahn, "Extended-Connectivity Fingerprints",
  *J. Chem. Inf. Model.* 50 (2010) 742–754. doi:10.1021/ci100050t.

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
