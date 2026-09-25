---
description: Elucidate structures from a SMILES list by cascading the GLACIER simulator into the JAM fingerprint predictor into the FRIGID decoder.
---

# MS/MS Cross-Model Cascade

This workflow guides you through turning a list of candidate structures into
ranked decoded structures by chaining three in-house models, and through
measuring how much structural information survives each hand-off.

**Scientific Problem:** Structure elucidation from MS/MS rarely fails in one
place. A simulator can predict the right fragment formulas while a downstream
encoder still produces a fingerprint too coarse to identify the molecule, and a
decoder can produce chemically valid molecules that are simply the wrong isomer.
Running the three models in isolation on their own benchmarks hides this: each
reports accuracy against its own ground-truth input, never against the noisy
output the previous model would actually hand it.

This workflow closes that loop. Starting from known structures, it simulates
their `[M+H]+` spectra, encodes those simulated spectra into fingerprints,
decodes those predicted fingerprints into structures, and scores every stage
against the same ground truth. The result answers a question no single model's
benchmark does: given a spectrum we can only simulate and a fingerprint we can
only predict, how often does the decoder recover the true structure, and which
stage is the bottleneck?

The collision-energy grid (20 / 40 / 60 eV) is part of the question. Low energies
give few fragments and an under-determined fingerprint; high energies fragment
extensively but lose the intact-molecule evidence. Running all three per molecule
shows which energy the cascade actually needs.

## Cascade, not parallel fusion

Per `research-standards.md` step 4, the orchestration mode here is **cascade**,
and this distinction governs how results are reported:

- **GLACIER** outputs fragment m/z, intensities and per-fragment formulas. It
  never proposes a structure, so it cannot be a candidate source. Its output is
  insufficient on its own: many distinct molecules share a fragment-formula set,
  and the simulation is conditioned on a structure already being known.
- **JAM** outputs a 4096-bit Morgan fingerprint. A fingerprint is a lossy,
  non-invertible substructure summary — insufficient on its own because it names
  no atoms, bonds or connectivity, and many molecules map to overlapping bit
  sets.
- **FRIGID** is the only stage that emits structures.

Therefore the **final answer for each compound is FRIGID's top-1 candidate**.
Stage 1 and stage 2 metrics are diagnostics that localize error, not independent
votes to be ranked against FRIGID's. Do not average confidence across the three
models or present them as three opinions on one question; each stage's only
consumer is the next stage.

## Steps

1. **Classify the request and prepare the output directory.**
   This is an Elucidation Task under `research-standards.md`, so create a
   timestamped directory under `results/` before running anything, and write the
   plan (inputs, stages, collision-energy grid, adduct restriction) for user
   confirmation first.

2. **Validate the inputs.**
   Read the SMILES file yourself and confirm its shape — one SMILES per line, no
   header, no index column — rather than trusting a prior description. Canonicalize
   each SMILES with RDKit and compute its molecular formula; a molecule RDKit
   cannot parse is dropped and reported as a caveat, never silently skipped.
   Per-molecule ids are generated positionally (`<prefix>_<0-based index>`) because
   the input carries no id column.

3. **Simulate spectra — skill `msms-cross-model-elucidation`, step 2.**
   Run GLACIER over every molecule at 20, 40 and 60 eV with `ionization` fixed to
   `[M+H]+`. Pass `--sparse-out` (required) and `--frag-form-vecs` (needed for the
   per-fragment formulas stage 4 consumes); never pass `--binned-out`, which
   discards them. GLACIER runs in the `ms-pred` sibling repo's own venv.

4. **Predict fingerprints — skill `msms-cross-model-elucidation`, step 3.**
   Convert each simulated spectrum into a subformula-assignment tree and run JAM's
   `MistNet` encoder. Re-emit every fragment formula through jam's own
   formula-vector helpers: `ms_pred` and `jam` order their element vectors
   differently, and passing raw strings between them silently corrupts the
   formulas. Binarize at the documented default threshold (0.15) unless a
   validation split is available to select one.

5. **Decode structures — skill `msms-cross-model-elucidation`, step 4.**
   Condition FRIGID's sampler on JAM's **predicted** fingerprint, not the ground
   truth: decoding the predicted fingerprint is the point of the cascade. Filter
   candidates to the target molecular formula, treated as known from the precursor
   mass, and collect up to 10 unique formula-matching candidates per (molecule,
   collision energy).

6. **Decide per compound.**
   Report FRIGID's top-1 candidate as the answer. Use JAM's fingerprint Tanimoto
   at that collision energy as the confidence proxy, and the formula-matched
   candidate count as a coverage indicator: a record with zero formula-matching
   candidates is an honest failure to report, not a record to retry until it
   succeeds. If no collision energy yields a candidate, report the compound as
   undetermined.

7. **Report — skill `msms-cross-model-elucidation`, step 5.**
   Build the notebook and the per-compound markdown report. Every compound gets
   its top candidate, that candidate's Tanimoto and exact-match status, the model
   that produced it, and the run's caveats — above all that the driving spectra are
   simulated rather than measured, and that the formula filter assumes a known
   precursor formula.

## Decision logic

- If GLACIER writes no spectrum for a molecule (RDKit parse failure, unsupported
  element, more than 100 heavy atoms), drop that molecule and record why.
- If a stage-1 spectrum has no usable peaks after dropping zero-intensity
  padding, skip that (molecule, collision energy) pair and record it; do not
  skip the molecule's other collision energies.
- If FRIGID exhausts `--max-attempts` without a formula match, report zero
  candidates for that pair rather than relaxing the formula filter.
- Never substitute the ground-truth fingerprint for JAM's prediction to make
  stage 3 succeed; that measures FRIGID alone and answers a different question.

## References

- **GLACIER / ms-pred** — joint fragment and intensity MS/MS simulation,
  `coleygroup/ms-pred`. Checkpoint: `CRG-MIT/GLACIER`
  (`glacier_all_random_unmatched`).
- **JAM / MIST** — Goldman et al., "Annotating metabolite mass spectra with
  domain-inspired chemical formula transformers", *Nature Machine Intelligence* 5
  (2023) 965–979. doi:10.1038/s42256-023-00708-3. Checkpoint: `CRG-MIT/MIST-JAM`
  (`mist_jam_all_maxtrain_random_split`).
- **FRIGID** — masked diffusion language model decoding molecular fingerprints to
  SAFE strings.
- **SAFE** — Noutahi et al., "Gotta be SAFE: a new framework for molecular
  design", *Digital Discovery* 3 (2024) 796–804. doi:10.1039/D4DD00019F.
- **Morgan fingerprints** — Rogers & Hahn, "Extended-Connectivity Fingerprints",
  *J. Chem. Inf. Model.* 50 (2010) 742–754. doi:10.1021/ci100050t.
- **MassSpecGym** — Bushuiev et al., "MassSpecGym: A benchmark for the discovery
  and identification of molecules", *NeurIPS Datasets and Benchmarks* (2024).
  arXiv:2410.23326 — context for the retrieval and de novo metrics this cascade
  reports.
