# Example: De novo prediction for an unknown spectrum

## Goal
Generate candidate structures for an unknown compound from an experimental mzML file.

## Steps
```bash
# Env: denovo
python .agents/skills/msms-denovo/scripts/run.py \
    --spectrum examples/example-basic/query.mzML \
    --top_k 10 \
    --output examples/example-basic/output.json
```

## Expected Output
`output.json` with ranked SMILES candidates and scores.

## TODO
Add example spectrum and expected top candidate once the model is implemented.
