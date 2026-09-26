# Example: Convert Thermo .raw to mzML

## Goal
Convert a Thermo `.raw` file from an LC-MS/MS experiment to mzML.

## Prerequisites
Docker must be available and the pwiz image pulled:
```bash
docker pull proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses
```

## Steps
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input examples/example-mzml/input.raw \
    --output examples/example-mzml/output.mzML
```

## Expected Output
`output.mzML` — standard mzML file readable by pyteomics, pymzml, and all downstream skills.

## TODO
Add a small representative `.raw` file once data is available. Compare spectrum count and precursor masses against a reference conversion.
