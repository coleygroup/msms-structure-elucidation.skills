# Example: Convert Thermo .raw to mzML

Use a native msconvert installation with support for the instrument's raw format. Provide your own input file and executable path:

```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input /path/to/sample.raw \
    --output results/sample.mzML \
    --msconvert-bin /path/to/msconvert
```

The output is `results/sample.mzML`. Verify that it contains the expected scan counts and precursor masses before downstream analysis. If msconvert is missing, follow the [installation guidance](../../SKILL.md#if-msconvert-is-missing): look up current native or Docker instructions for the conversion host and let the user perform installation and setup. Exporting mzML on the instrument workstation is also an option.
