---
name: msms-atlas-retrieval
description: Retrieve candidate structures for an experimental MS/MS spectrum from the public ICEBERG PubChem atlas. Use for formula-based or mass-based atlas searches; route direct model prediction and fingerprint comparison to their respective skills.
---
# Public PubChem atlas retrieval

Use this skill to match experimental MS/MS spectra against the public ICEBERG PubChem atlas. For spectra predicted directly with GLACIER or ICEBERG, use `msms-predict`; for molecular fingerprint prediction or comparison, use `msms-mist-fingerprint` or `msms-fingerprint-compare`. The direct mass-only PubChem option below searches PubChem and predicts its candidates rather than retrieving stored atlas spectra.

Read [saved pipeline answers](../../../docs/pipeline-preferences.md) and reuse the collision-energy unit, search route and intended model when their scope/context matches. Ask only for missing or conflicting choices and save the answers. For model-backed work, confirm the exact checkpoint paths with `msms-setup`. With MCP connected, call `server_info` first and use its tools.

## Choose the spectrum source

For **ICEBERG**, prefer suitable public atlas spectra. The atlas contains predictions for PubChem structures generated with **ICEBERG 2.1 trained on NIST'23**. This describes the prediction model's training data; licensed NIST structures are excluded from the public candidate library. Use local ICEBERG predictions when the atlas lacks the required structure, adduct or collision energies, or when the user explicitly requests fresh predictions throughout.

A different local ICEBERG checkpoint may produce different predictions. Mixing those predictions with atlas spectra may affect ranking comparability; do not assume either equivalence or incompatibility without evidence. When the local checkpoint differs, or matching weights have not been verified, tell the user briefly:

> The atlas uses ICEBERG 2.1 trained on NIST'23. Local predictions use your selected checkpoint, which may differ from the atlas weights. Atlas spectra are preferred where suitable; each local prediction is labelled with its checkpoint.

For **GLACIER**, predict every candidate with the confirmed GLACIER weights; no open GLACIER atlas is available. Use `score_candidates` for an explicit candidate list, or `search_pubchem_mass` for direct mass search.

## Atlas retrieval

Reuse the matching dataset/spectrum answer for NCE versus absolute eV, or ask once and save it; do not infer the unit from file headers or numeric values. After NCE is confirmed:

```bash
msms-structure-elucidation run --input sample.ms --collision-unit NCE \
  --formula C12H21NO5 --output-dir results/sample
```

Use `--collision-unit eV` for confirmed absolute eV. Supply `--formula` or `--formulas-file` when formulas are known. Otherwise the CLI searches the top three MSBuddy hypotheses; if none are proposed, it searches up to three formulas from PubChem mass matches. Instrument defaults are Q-TOF 10/20 ppm and Orbitrap 5/10 ppm for precursor/fragments; override with `--ms1-ppm` and `--ms2-ppm`.

The local atlas workflow collects candidate formulas, downloads one MGF per formula through `https://iceberg-ms.mit.edu/download_mgf`, then ranks the structures across those files. `--atlas-mgf` supplies a local MGF for a single formula. The [atlas WebUI](https://github.com/rogerwwww/iceberg-atlas-webui#spectrum-retrieval) also offers **Mass ± ppm** retrieval without a supplied formula: it searches the existing atlas's formula/mass index and ranks stored spectra across the matched formulas. This is a separate interface from the local formula-by-formula download workflow.

Match the experimental adduct and every supplied collision energy. NCE is converted to eV; experimental and atlas energies are paired only at equal rounded integer eV. If any required energy is absent, the CLI predicts every atlas structure for that formula at all supplied energies with local ICEBERG. It also predicts every structure for an adduct absent from the public download. Never rank spectra of a different adduct or substitute off-energy atlas spectra. Provide the confirmed ICEBERG checkpoints and ms-pred checkout in `configs/local.yaml` or via CLI, and include the checkpoint note above when appropriate.

## Direct mass-only PubChem prediction

Use this route for a request to predict all PubChem mass matches, including GLACIER searches. It queries PubChem within the adduct-corrected neutral-mass window, skips formula prediction and atlas retrieval, and predicts all eligible structures for every matched formula. It is separate from the WebUI's mass-based atlas retrieval.

```bash
msms-structure-elucidation run --input sample.ms --collision-unit NCE \
  --mass-only --ms1-ppm 5 --model glacier --output-dir results/sample
```

Use the confirmed energy unit, ppm tolerance and model; select `--model iceberg` for fresh ICEBERG predictions. Configure the exact weights and model environment through `msms-setup`. With MCP, use `search_pubchem_mass` with `model`, `ppm` and `collision_unit`. The CLI `batch` command and this skill's `scripts/run.py` also accept `--mass-only`; the script takes `--spectrum` and `--output` in place of `--input` and `--output-dir`.

The route retains all eligible formulas and structures, preserves PubChem identifiers and mass errors, and ranks across formulas. `--top-k` controls the returned shortlist. Do not combine `--mass-only` with `--formula`, `--formulas-file`, `--atlas-mgf` or `--no-pubchem`; an input header formula is ignored with a warning. See the [technical guide](../../../docs/technical-guide.md#mass-only-pubchem-option) for filters, supported adducts and resumable prediction settings.

## Environment and results

If needed, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) and follow its README **Install & setup** instructions for the host. Verify that the model interpreter imports `ms_pred`, `msbuddy` and `rdkit`; pass it with `--ms-pred-python` or `MS_PRED_PYTHON`. Save local paths in the uncommitted `configs/local.yaml`. `setup_envs.sh` is a CPU venv helper.

Inspect `retrieval.json`, including `formula_results`, energy pairs, source provenance and `qc`. Direct mass search also records candidate manifests, per-formula counts and `prediction_complete`. Empty searches are valid outcomes; incomplete lookups, missing required model assets and prediction failures must be reported explicitly. `no_atlas_coverage` can be followed by PubChem candidate prediction; review the spectrum and formula evidence before choosing FRIGID when no candidate formula is available.

Rank by mean entropy similarity across all supplied collision energies, excluding the precursor. Explained intensity and unmatched peaks support review; similarity is not identification probability. Flag positional isomers that these spectra cannot distinguish. For many features, use `batch` with the selected route and host-tuned worker/memory settings.

Open `msms-visualize` and share its localhost URL as the primary result. The viewer supports paired-energy spectra, predicted fragments, candidate comparisons and saved review notes; the basic `report.html` provides a simpler offline report.
