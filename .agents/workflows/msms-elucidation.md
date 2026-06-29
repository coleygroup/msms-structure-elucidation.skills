---
description: Workflow for end-to-end MS/MS structure elucidation using simulator, retrieval, and de novo models in parallel or cascade mode.
---

# MS/MS Structure Elucidation

This workflow guides you through elucidating the structure of an unknown compound from a raw or pre-processed MS/MS spectrum.

**Scientific Problem:** Untargeted metabolomics and natural product discovery routinely produce MS/MS spectra for compounds absent from reference libraries. This workflow addresses that gap by combining three complementary in-house models: a fragmentation simulator (for candidate confirmation), a spectral database retrieval model (for known-compound matching), and a de novo structure generator (for truly unknown compounds). Results are fused into a ranked candidate list.

---

## Inputs
- A spectrum file: `.raw`, `.d`, mzML, or MGF
- (Optional) a candidate SMILES for the simulator
- `configs/default.yaml` with model checkpoints and database paths

## Step 1 — Preprocess

If the input is a raw instrument file, convert it first:

```bash
# Env: preprocess
python .agents/skills/msms-preprocess/scripts/run.py \
    --input <input_file> \
    --output results/<name>.mzML \
    --format mzML
```

Skip this step if the input is already mzML or MGF.

## Step 2 — Run the agent

```bash
snowmageddon run --input results/<name>.mzML
```

The agent (`claude-opus-4-8` with adaptive thinking) decides which MCP tools to call and in what order. It has four tools available:

- `retrieve_candidates` — spectral database search (best for known compounds in libraries)
- `simulate_spectrum_iceberg` — ICEBERG forward simulation (verify a candidate SMILES)
- `predict_structure_denovo` — de novo structure generation (best for novel unknowns)
- `pubchem_isomers` / `pubchem_compound` — PubChem lookup for formula expansion or metadata

Default strategy (encoded in the system prompt):
1. Run `retrieve_candidates` first. If score ≥ 0.8, verify with `simulate_spectrum_iceberg`.
2. If retrieval confidence is low, run `predict_structure_denovo`.
3. Use PubChem tools to expand or validate the final candidate.

Pass `--mode cascade` or `--mode parallel` as a hint to the agent; it does not force hard control.

## Step 3 — Review outputs

All outputs land in `results/<timestamp>/`:
- `trace.jsonl` — full reasoning trace (thinking blocks, tool calls, tool results, final answer)
- Model output files written by each tool (e.g. `spectrum.png`, `fragments.json`, `retrieval.json`)

## Step 4 — Report

The agent prints a ranked summary to stdout. The full trace in `trace.jsonl` documents which model produced each candidate, confidence scores, and the agent's reasoning.

---

## Configuration

Set `mode: parallel` or `mode: cascade` in `configs/default.yaml` as an orchestration hint. The CLI reads this automatically and passes it to the agent's context.

---

## References
- TODO: add references for each model once implemented.
