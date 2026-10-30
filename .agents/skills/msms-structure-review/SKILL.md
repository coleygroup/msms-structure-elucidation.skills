---
name: msms-structure-review
description: Review candidate structures and formula matches, then score with the confirmed ICEBERG or GLACIER model; reuse suitable public atlas predictions only for the ICEBERG route.
---

# Structure review

Keep the prediction model and exact weights confirmed in `msms-setup`. With MCP connected, call `server_info` first and use `score_candidates` with an explicit `model` and the target `formula`. For GLACIER or a request for fresh predictions throughout, score the complete candidate pool directly with the selected model and weights. For a [mass-only PubChem search](../../../docs/technical-guide.md#mass-only-pubchem-option), use `run --mass-only --ms1-ppm <tolerance> --model <confirmed-model>` or the MCP `search_pubchem_mass` tool; it skips formula prediction and atlas reuse, scores all eligible structures for every matched formula, and combines their scores.

For ICEBERG, prefer suitable atlas spectra. The atlas uses ICEBERG 2.1 trained on NIST'23; if local weights differ or matching weights are unverified, tell the user that combining their predictions may affect ranking comparability and label the sources. The CLI `review` command extends an existing result: it retains candidates, reuses suitable atlas matches and predicts additional structures. Use direct `score_candidates` calls for a ranking based entirely on one selected model and checkpoint.

Read `retrieval.json`, especially the user-confirmed `collision_unit`, `energy_mapping`, each candidate's `energy_alignment`, `formula_hypotheses`, `review_evidence.matched_peaks`, and `review_evidence.strongest_unexplained_peaks`. Check that every supplied experimental energy contributed to the score, and report unmatched energies. The 0.5 weak-match flag is a heuristic, not identification confidence. Propose a JSON array of new SMILES using your preferred model or human reasoning; no LLM provider is required by the CLI.

For `next_step: iceberg-pubchem` (no ICEBERG Atlas entry for the formula), pass `--proposals pubchem` instead of `--smiles-json` to predict PubChem's single-component, uncharged structures of the formula (`--max-structures`, default 500; `--formula` picks one of several searched formulas). For `next_step: review-frigid`, no formula was found by MSBuddy or by a PubChem mass search: review the spectrum and decide whether FRIGID de novo generation (`msms-denovo`) is warranted. Proposals are matched to atlas entries by InChIKey connectivity, because the atlas stores SMILES without stereochemistry. Each simulated candidate records `model_name`, `model_version` (from ms-pred's `model_registry`, e.g. ICEBERG 2.1), `ms_pred_version`, and `model_checkpoint`; report them with the result.

For local GLACIER or ICEBERG inference, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if needed, read the checkout's `README.md` **Install & setup** section, and follow its instructions for the host's CPU or CUDA environment. Reuse a verified existing interpreter when available and pass it with `--ms-pred-python`. Set `--ms-pred-dir` to the checkout; GLACIER launches a relative script from there. Save recurring checkout and checkpoint paths in the uncommitted `configs/local.yaml`.

```bash
msms-structure-elucidation review \
  --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json \
  --ms-pred-dir /path/to/ms-pred \
  --model iceberg \
  --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt
```

The command canonicalizes SMILES, rejects formula mismatches and duplicates, reuses exact-energy atlas matches, then simulates absent structures. Set `--model` explicitly to the selected model. ICEBERG needs generator and intensity checkpoints; GLACIER needs one checkpoint. When the result has multiple formulas, use `--formula` to select the formula for proposals. Model inputs are rounded integer eV after confirmed NCE conversion and use `nce=False`. `.ms >instrumentation` supplies the instrument unless `--instrument` overrides it; `--cuda-devices` or `MSMS_CUDA_DEVICES` enables GPU selection. Use settings measured for the selected model on this host. The anchors in `msms-predict` (**Inference configuration anchors**) apply to ICEBERG; tune GLACIER's native `pred_smis` batch size and CPU workers independently as described in `msms-setup`. The current GLACIER adapter uses one GPU worker. GLACIER checkpoint atom feature width is checked before inference. Model subprocess failure is fatal, prints its output, and leaves the prior result untouched. Generated candidates record `model_collision_energies_ev`. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme); use locally supplied NIST weights only if licensed. The offline HTML report is refreshed after a successful review. Use `msms-visualize` to inspect fragment assignments and save review notes.
