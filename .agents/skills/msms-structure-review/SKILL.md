---
name: msms-structure-review
description: Review weak atlas matches, validate proposed structures against a formula, then reuse atlas predictions or simulate missing structures with GLACIER or ICEBERG.
---

# Structure review

Read `retrieval.json`, especially the user-confirmed `collision_unit`, `energy_mapping`, each candidate's `energy_alignment`, `formula_hypotheses`, `review_evidence.matched_peaks`, and `review_evidence.strongest_unexplained_peaks`. Check that every supplied experimental energy contributed to the score, and report unmatched energies. The 0.5 weak-match flag is a heuristic, not identification confidence. Propose a JSON array of new SMILES using your preferred model or human reasoning; no LLM provider is required by the CLI.

For local GLACIER or ICEBERG inference, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if needed, read the checkout's `README.md` **Install & setup** section, and follow its instructions for the host's CPU or CUDA environment. Reuse a verified existing interpreter when available and pass it with `--ms-pred-python`. Set `--ms-pred-dir` to the checkout; GLACIER launches a relative script from there. Configure the checkout and checkpoints in `configs/default.yaml` if these are recurring paths.

```bash
msms-structure-elucidation review \
  --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json \
  --ms-pred-dir /path/to/ms-pred \
  --model glacier \
  --checkpoint /path/to/glacier/best.ckpt
```

The command canonicalizes SMILES, rejects formula mismatches and duplicates, reuses exact-energy atlas matches, then simulates absent structures. GLACIER is default. For ICEBERG use `--model iceberg --gen-checkpoint ... --inten-checkpoint ...`. When the result has multiple formulas, use `--formula` to select the formula for proposals. Model inputs are rounded integer eV after confirmed NCE conversion and use `nce=False`. `.ms >instrumentation` supplies the instrument unless `--instrument` overrides it; `--cuda-devices` or `MSMS_CUDA_DEVICES` enables GPU selection. GLACIER checkpoint atom feature width is checked before inference. Model subprocess failure is fatal, prints its output, and leaves the prior result untouched. Generated candidates record `model_collision_energies_ev`. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme); use locally supplied NIST weights only if licensed. The offline HTML report is refreshed after a successful review. Use `msms-visualize` to inspect fragment assignments and save review notes.
