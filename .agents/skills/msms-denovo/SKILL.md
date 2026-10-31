---
name: msms-denovo
description: Generate formula-constrained structures from an experimental .ms spectrum with FRIGID when retrieval or direct model scoring leaves no satisfactory candidate.
---
# FRIGID de novo generation

Use after ICEBERG atlas retrieval or direct candidate scoring with the confirmed model, especially when the strongest experimental peaks remain unexplained. FRIGID requires a neutral molecular formula and peak subformula assignments. The formula may be supplied, inferred with MSBuddy, or taken from a PubChem mass match; treat inferred and mass-matched formulas as hypotheses. A mass-only PubChem search skips formula prediction and does not require FRIGID.

In the elucidation workflow, run this skill only when the user selected FRIGID or both routes after the agent's evidence review, or directly requested FRIGID generation. Weak evidence or an unsuccessful agent-proposal round does not automatically select FRIGID. Honor a skip or agent-only choice, and reuse a saved `denovo_route` only within its matching analysis/entry context; see [pipeline preferences](../../../docs/pipeline-preferences.md).

## Setup

If no FRIGID checkout exists, clone [coleygroup/FRIGID](https://github.com/coleygroup/FRIGID) with its submodules. Read that checkout's `README.md` **Installation** section and follow its environment, submodule patch, and dependency instructions for the checked-out version. Reuse an existing checkout only after reading its README and verifying its environment. `bash .agents/skills/msms-denovo/scripts/setup_env.sh` is a separate venv helper; compare it with the upstream README before using it. The helper patches only a newly cloned FRIGID submodule and does not change an existing user checkout. Set `MSMS_DOWNLOAD_FRIGID_WEIGHTS=1` with the helper to download and verify the public [Zenodo checkpoint archive](https://zenodo.org/records/19685145), or provide local files. Pass the prepared checkout and interpreter as `FRIGID_DIR` and `FRIGID_PYTHON`. Licensed NIST assets remain local to their holder.

## Run

For a completed retrieval result, the CLI runs subformula assignment and FRIGID in sequence:

```bash
msms-structure-elucidation denovo --result results/sample/retrieval.json \
  --frigid-dir .cache/FRIGID --frigid-python .cache/frigid-venv/bin/python \
  --mist-ckpt /path/to/mist.ckpt --dlm-ckpt /path/to/dlm.ckpt
```

This writes `denovo.json` and adds unscored FRIGID proposals to the report. With MCP connected, call `server_info` first and use `generate_structures_frigid`, then `score_candidates` with the confirmed model and weights. For GLACIER or fresh predictions throughout, rescore the whole candidate pool directly; CLI `review` may retain or reuse ICEBERG atlas scores. For the ICEBERG atlas route, CLI `review` can reuse suitable atlas matches and predict missing structures. For direct use of the component scripts without MCP:

```bash
"${MS_PRED_PYTHON:-python}" .agents/skills/msms-subformulae/scripts/run.py \
  --spectrum sample.ms --formula C10H21N5O3 --adduct '[M+H]+' \
  --output-dir results/subformulae
"${FRIGID_PYTHON:-.cache/frigid-venv/bin/python}" \
  .agents/skills/msms-denovo/scripts/run.py \
  --spectrum sample.ms --formula C10H21N5O3 --adduct '[M+H]+' \
  --subform-dir results/subformulae --frigid-dir .cache/FRIGID \
  --frigid-python "${FRIGID_PYTHON:-.cache/frigid-venv/bin/python}" \
  --mist-ckpt /path/to/mist.ckpt --dlm-ckpt /path/to/dlm.ckpt \
  --num-rounds 0 --top-k 10 --output results/denovo.json
```

For ICEBERG-guided refinement set `--num-rounds` above zero and add `--iceberg-gen-ckpt` and `--iceberg-inten-ckpt`. The wrapper uses FRIGID's current `spec2mol_scaling.py` flags, writes its `predictions.csv` under `frigid_raw/`, and validates formula matches with RDKit. `--top-k` limits the returned candidates; it is not passed to upstream FRIGID as a CLI flag. A GPU is recommended for refinement. Model weights and subformula assignments are prerequisites, not bundled outputs.
