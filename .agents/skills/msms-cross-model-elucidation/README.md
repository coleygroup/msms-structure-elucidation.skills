# msms-cross-model-elucidation

A three-stage MS/MS cascade: **GLACIER** simulates spectra, **JAM** encodes them
into fingerprints, **FRIGID** decodes those fingerprints into structures. See
`SKILL.md` for the runnable commands and `.agents/workflows/msms-cross-model-cascade.md`
for the scientific framing.

## Why this exists

Each of the three models has its own benchmark, and each of those benchmarks
feeds the model clean ground-truth input. That hides the question this skill
answers: when the spectrum is only simulated and the fingerprint is only
predicted, how often does the decoder still recover the true structure, and which
stage is the bottleneck?

## Data flow

```
SMILES list (one per line, no header)
  │
  ├─ 01_simulate_glacier.py ──────────── ms-pred venv, --sparse-out --frag-form-vecs
  │    labels.tsv + split.tsv → predict_smis_joint → preds.hdf5 (PredSpecDB)
  │    → spectra/<name>_ce<ce>.json   {masses, intensities, frag_formulas, adduct}
  │
  ├─ 02_predict_jam_fingerprint.py ───── jam pixi project
  │    → subformulae/<spec>.json       {output_tbl, cand_form, cand_ion}
  │    → MistNet.encode_spectra → predicted_fingerprints.npz (probs + bits)
  │    → metrics.json                  fingerprint Tanimoto vs RDKit Morgan
  │
  ├─ 03_decode_frigid.py ─────────────── FRIGID conda env
  │    JAM predicted bits + target formula → generate_with_formula_filter
  │    → results.json, summary.json     exact match / Tanimoto top-1 and top-10
  │
  └─ 04_build_report.py ──────────────── preprocess pixi env
       → notebooks/msms_cross_model_cascade.ipynb (executed)
       → results/<timestamp>/report.md
```

Stage 3's output is the answer; stages 1 and 2 are diagnostics. The models are
chained, not fused — a fingerprint is not a candidate structure, so it cannot be
ranked against one.

## Three details that are easy to get wrong

1. **Element ordering differs between the repos.**
   `ms_pred.common.chem_utils.VALID_ELEMENTS` starts `C, N, P, O, S, Si, I, H, Cl, …`
   while `jam.chem.VALID_ELEMENTS` starts `C, H, As, B, Br, Cl, …`. A GLACIER
   `frag_form_vecs` row interpreted with jam's ordering yields a silently wrong
   formula. Stage 2 therefore round-trips every formula string through
   `jam.chem.formula_to_vec` / `vec_to_formula` instead of passing raw vectors or
   strings across the boundary.

2. **GLACIER needs `--sparse-out` and `--frag-form-vecs`, never `--binned-out`.**
   `run_shard` asserts `sparse_out`. Without `--frag-form-vecs` there are no
   per-fragment formulas, and `--binned-out` replaces them with a binned spectrum
   that stage 2's featurizer cannot consume.

3. **FRIGID must be conditioned on JAM's predicted fingerprint.**
   Passing the ground-truth fingerprint instead measures FRIGID in isolation and
   silently answers a different, much easier question.

## Environments

Stages 1 and 4 use this project's pixi envs (`default`, `preprocess`). Stages 2
and 3 run under the sibling repositories' own environment managers and are
invoked by absolute interpreter path — those repos are deliberately **not**
declared in this project's `pyproject.toml`.

`resources/checkpoints/` is gitignored (covered by the repo-root `checkpoints/`
entry) and holds the two downloaded checkpoints plus their `args.yaml` files.

---

**Author:** Magdalena Lederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
