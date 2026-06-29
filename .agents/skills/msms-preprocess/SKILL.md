---
name: msms-preprocess
description: Convert raw instrument files (.raw, .d, etc.) to mzML or MGF for downstream model input; no-op if the file is already in a supported format.
category: preprocessing
---

# msms-preprocess

## Goal
Convert proprietary instrument files (Thermo `.raw`, Bruker `.d`, etc.) to an open format (mzML or MGF) suitable for the simulator, retrieval, and de novo skills. If the input is already mzML/MGF/MSP, the script exits immediately with the path unchanged.

## Instructions

1. **Check input format** — if already converted, skip to the next skill.

2. **Convert to mzML**:
```bash
# Env: preprocess
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mzML \
    --format mzML
```

3. **Or convert to MGF**:
```bash
# Env: preprocess
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mgf \
    --format mgF
```

## Constraints
- **Environment**: `preprocess` (`pixi run --environment preprocess python ...`)
- **Supported input formats**: `.raw` (Thermo), `.d` (Bruker) — extend as needed
- **Supported output formats**: `mzML`, `MGF`
- **Converter**: defaults to `msconvert` (ProteoWizard); set `--converter` to swap

## References
- Chambers et al., "A cross-platform toolkit for mass spectrometry and proteomics", *Nature Biotechnology*, 2012. [DOI](https://doi.org/10.1038/nbt.2377)

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
