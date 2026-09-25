---
name: msms-preprocess
description: Convert raw instrument files (.raw, .d, etc.) to mzML or MGF for downstream model input; no-op if the file is already in a supported format.
category: preprocessing
---

# msms-preprocess

## Goal
Convert proprietary instrument files (Thermo `.raw`, Bruker `.d`, etc.) to an open format (mzML or MGF) suitable for the simulator, retrieval, and de novo skills. If the input is already mzML/MGF/MSP, the script exits immediately with the path unchanged.

## Prerequisites

Conversion requires msconvert (ProteoWizard). On Linux, the script runs it via Docker:

```bash
docker pull proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses
```

Docker must be available on the machine. The script checks for a system `msconvert` binary first; if not found, falls back to Docker automatically.

## Instructions

1. **Check input format** — if already converted, skip to the next skill.

2. **Convert to mzML**:
```bash
pixi run --environment preprocess python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mzML
```

3. **Or convert to MGF**:
```bash
pixi run --environment preprocess python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mgf \
    --format MGF
```

4. **Mixed-polarity `.raw` files** — most instrument methods acquire positive and negative ESI in one run. Splitting can be done either here (msconvert) or in `msms-feature-detect` itself (MZmine's chromatogram builder, via `--polarity "+"` / `--polarity "-"` — see its SKILL.md, this is now the preferred path since it avoids an extra conversion pass). To split here instead:
```bash
pixi run --environment preprocess python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_pos.mzML --polarity positive
pixi run --environment preprocess python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_neg.mzML --polarity negative
```
`--polarity` defaults to `any` (no filtering) — safe to omit for files already acquired in a single polarity, or if you plan to split downstream in `msms-feature-detect` instead.

## Constraints
- **Environment**: `preprocess` (`pixi run --environment preprocess python ...`)
- **Supported input formats**: `.raw` (Thermo), `.d` (Bruker) — extend as needed
- **Supported output formats**: `mzML`, `MGF`
- **Converter**: msconvert (ProteoWizard) via Docker (`proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses`) or system binary
- **Docker stack**: `your script → Docker → Wine → msconvert.exe → .mzML`
- **Machine requirement**: Docker must be installed; on molgpu nodes, molgpu06 has it
- **Polarity filtering**: `--polarity {positive,negative,any}` maps to msconvert's own `polarity` filter. Equivalent to (and interchangeable with) splitting in `msms-feature-detect --polarity "+"/"-"` — pick whichever fits your pipeline; MZmine's version needs the literal `+`/`-` symbol, not a word.

## References
- Chambers et al., "A cross-platform toolkit for mass spectrometry and proteomics", *Nature Biotechnology*, 2012. [DOI](https://doi.org/10.1038/nbt.2377)

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
