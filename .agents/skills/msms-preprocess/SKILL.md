---
name: msms-preprocess
description: Convert raw instrument files (.raw, .d, etc.) to mzML or MGF for downstream model input; no-op if the file is already in a supported format.
---

# msms-preprocess

## Goal
Convert proprietary instrument files (Thermo `.raw`, Bruker `.d`, etc.) to an open format (mzML or MGF) suitable for the simulator, retrieval, and de novo skills. For mzML/MGF input, the script passes through the existing path. Use an existing `.ms` spectrum directly with the downstream skills.

## Prerequisites

Raw conversion requires an existing native msconvert (ProteoWizard) executable that supports the instrument format on the conversion host. Select it with `--msconvert-bin /path/to/msconvert`, set `MSCONVERT_BIN`, or place it on `PATH`. An explicitly selected path takes precedence.

If a suitable converter is unavailable, use the instrument workstation or another user-selected conversion host to export mzML or MGF, then continue with that file. Do not install system services or require privileged access for this workflow. mzML-to-`.ms` export uses `pyteomics` and does not require msconvert.

## Instructions

1. **Check input format** — if already converted, skip to the next skill.

2. **Convert to mzML**:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mzML \
    --msconvert-bin /path/to/msconvert
```

3. **Or convert to MGF**:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw \
    --output results/sample.mgf \
    --format MGF
```

4. **Mixed-polarity `.raw` files** — most instrument methods acquire positive and negative ESI in one run. Splitting can be done either here (msconvert) or in `msms-feature-detect` itself (MZmine's chromatogram builder, via `--polarity "+"` / `--polarity "-"` — see its SKILL.md, selecting polarity there avoids an extra conversion pass). To split here instead:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_pos.mzML --polarity positive
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_neg.mzML --polarity negative
```
`--polarity` defaults to `any` (no filtering) — safe to omit for files already acquired in a single polarity, or if you plan to split downstream in `msms-feature-detect` instead.

5. **GNPS/MZmine feature MGF to `.ms`** — after confirming NCE versus absolute eV, run `msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --output-dir converted`. Add `--raw-mzxml` with the raw mzXML files or their folder to read precursor-specific collision energies from referenced MS2 scans, or, for GNPS MGF that names only `SOURCE_FILE`, from that run's MS2 scans of the same precursor near `RTINSECONDS` (`--ms2-rt-window`). Convert the full MGF with `--feature-ids` to pick features: adducts, multimers and in-source fragments are assigned from all co-eluting features (`--ion-rt-window`, `--elucidate-all-ions`, `--no-ion-identity`). `SOURCE_SCAN` and `SCANS` can identify the MS1 apex and are never used as MS2 energy evidence. A `needs_energy` manifest row requires a verified `--energy` override or raw MS2 metadata. Bruker Q-TOF collision values are absolute eV set per precursor m/z; inspect the MS2 scan in raw mzXML. For the ICEBERG atlas route, process `converted/manifest.csv` with `msms-structure-elucidation batch`. For a mass-only PubChem search with either model, add `--mass-only --ms1-ppm <tolerance> --model <confirmed-model>` to `batch`; it skips formula prediction and atlas retrieval and writes the same batch review page. Follow the [scientific workflow](../../workflows/msms-elucidation.md).

## Constraints
- **Environment**: `preprocess` (`python ...`)
- **Supported input formats**: `.raw` (Thermo), `.d` (Bruker) — extend as needed
- **Supported output formats**: `mzML`, `MGF`
- **Converter**: user-configured native msconvert (ProteoWizard); vendor-format support depends on the installation and host platform.
- **Conversion host**: use a host with a suitable converter, or provide already exported mzML/MGF files.
- **Polarity filtering**: `--polarity {positive,negative,any}` maps to msconvert's own `polarity` filter. Equivalent to (and interchangeable with) splitting in `msms-feature-detect --polarity "+"/"-"` — pick whichever fits your pipeline; MZmine's version needs the literal `+`/`-` symbol, not a word.

## References
- Chambers et al., "A cross-platform toolkit for mass spectrometry and proteomics", *Nature Biotechnology*, 2012. [DOI](https://doi.org/10.1038/nbt.2377)

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
