# Example: Retrieve candidates for an unknown spectrum

## Goal
Query the spectral database with an experimental mzML file and return the top 10 candidate structures.

## Steps
```bash
# Env: retrieval
python .agents/skills/msms-retrieval/scripts/run.py \
    --spectrum examples/example-basic/query.mzML \
    --top_k 10 \
    --output examples/example-basic/output.json
```

## Expected Output
`output.json` with ranked candidates. The top candidate should match the known reference compound (add reference SMILES and score once implemented).

## TODO
Add example spectrum file and expected top hit once database is set up.
