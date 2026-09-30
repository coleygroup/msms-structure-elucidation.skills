---
name: msms-preprocess
description: Convert raw instrument files (.raw, .d, etc.) to mzML or MGF for downstream model input; no-op if the file is already in a supported format.
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
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mzML
```

3. **Or convert to MGF**:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mgf \
    --format MGF
```

4. **Mixed-polarity `.raw` files** — most instrument methods acquire positive and negative ESI in one run. Splitting can be done either here (msconvert) or in `msms-feature-detect` itself (MZmine's chromatogram builder, via `--polarity "+"` / `--polarity "-"` — see its SKILL.md, this is now the preferred path since it avoids an extra conversion pass). To split here instead:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_pos.mzML --polarity positive
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_neg.mzML --polarity negative
```
`--polarity` defaults to `any` (no filtering) — safe to omit for files already acquired in a single polarity, or if you plan to split downstream in `msms-feature-detect` instead.

5. **GNPS/MZmine feature MGF to `.ms`** — after confirming NCE versus absolute eV, run `msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --output-dir converted`. Add `--raw-mzxml` with the raw mzXML files or their folder to read precursor-specific collision energies from referenced MS2 scans, or, for GNPS MGF that names only `SOURCE_FILE`, from that run's MS2 scans of the same precursor near `RTINSECONDS` (`--ms2-rt-window`). `SOURCE_SCAN` and `SCANS` can identify the MS1 apex and are never used as MS2 energy evidence. A `needs_energy` manifest row requires a verified `--energy` override or raw MS2 metadata. Bruker Q-TOF collision values are absolute eV set per precursor m/z; inspect the MS2 scan in raw mzXML. Process `converted/manifest.csv` with `msms-structure-elucidation batch`.

## Constraints
- **Environment**: `preprocess` (`python ...`)
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
