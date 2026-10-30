# Example: Convert Thermo .raw to mzML

Use a native msconvert installation with support for the instrument's raw format. Provide your own input file and executable path:

```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input /path/to/sample.raw \
    --output results/sample.mzML \
    --msconvert-bin /path/to/msconvert
```

The output is `results/sample.mzML`. Verify that it contains the expected scan counts and precursor masses before downstream analysis. If a suitable converter is unavailable on this host, export mzML on the instrument workstation and use the exported file directly.
