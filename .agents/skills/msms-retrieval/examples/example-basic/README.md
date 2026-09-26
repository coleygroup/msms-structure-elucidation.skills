# Example: Retrieve candidates for an unknown spectrum

## Goal
Query the public ICEBERG PubChem atlas with an experimental `.ms` spectrum and return the top 10 candidate structures.

## Steps
```bash
# Confirm collision units with the spectrum provider before running.
python .agents/skills/msms-retrieval/scripts/run.py \
    --spectrum /path/to/sample.ms \
    --collision-unit NCE \
    --top-k 10 \
    --output results/sample/retrieval.json
```

## Expected Output
`retrieval.json` with ranked candidates and `report.html` with energy-specific mirror spectra. The JSON records the source unit, NCE-to-eV conversion, and each experimental-to-atlas energy pair.
