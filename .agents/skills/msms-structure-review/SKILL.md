---
name: msms-structure-review
description: Review weak atlas matches, validate proposed structures against a formula, then reuse atlas predictions or simulate missing structures with GLACIER or ICEBERG.
---

# Structure review

Read `retrieval.json`, especially the user-confirmed `collision_unit`, `energy_mapping`, each candidate's `energy_alignment`, `formula_hypotheses`, `review_evidence.matched_peaks`, and `review_evidence.strongest_unexplained_peaks`. Check that every supplied experimental energy contributed to the score, and report unmatched energies. The 0.5 weak-match flag is a heuristic, not identification confidence. Propose a JSON array of new SMILES using your preferred model or human reasoning; no LLM provider is required by the CLI.

For local GLACIER or ICEBERG inference, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if needed, read the checkout's `README.md` **Install & setup** section, and follow its instructions for the host's CPU or CUDA environment. Reuse a verified existing interpreter when available and pass it with `--ms-pred-python`.

```bash
msms-structure-elucidation review \
  --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json \
  --model glacier \
  --checkpoint /path/to/glacier/best.ckpt
```

The command canonicalizes SMILES, rejects formula mismatches and duplicates, checks the downloaded atlas MGF first, then simulates only absent structures. GLACIER is default. For ICEBERG use `--model iceberg --gen-checkpoint ... --inten-checkpoint ...`. Model inputs are always rounded integer eV values, converted from confirmed NCE using precursor m/z when necessary; the call uses `nce=False`. Generated candidates record the exact `model_collision_energies_ev` list. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme); use locally supplied NIST weights only if licensed. If local weights are unavailable, the JSON records that limitation and retains the atlas results. The offline HTML report is refreshed with the new candidates. Use `msms-visualize` afterward to inspect their fragment assignments and save review notes locally.
