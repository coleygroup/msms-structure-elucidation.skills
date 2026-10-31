---
name: msms-preprocess
description: Convert raw instrument files (.raw, .d, etc.) to mzML or MGF for downstream model input; no-op if the file is already in a supported format.
---

# msms-preprocess

## Goal
Convert proprietary instrument files (Thermo `.raw`, Bruker `.d`, etc.) to an open format (mzML or MGF) suitable for the simulator, retrieval, and de novo skills. For mzML/MGF input, the script passes through the existing path. Use an existing `.ms` spectrum directly with the downstream skills.

## Prerequisites

Raw conversion requires an existing native msconvert (ProteoWizard) executable that supports the instrument format on the conversion host. Select it with `--msconvert-bin /path/to/msconvert`, set `MSCONVERT_BIN`, or place it on `PATH`. An explicitly selected path takes precedence.

mzML-to-`.ms` export needs this package and `pyteomics` in the selected Python environment; it reads activation metadata to preserve units and stepped settings and does not require msconvert.

## If msconvert is missing

1. Identify the operating system, architecture and vendor raw format on the host that will perform conversion. Search for up-to-date installation instructions for that platform, starting with the [official ProteoWizard downloads and installation guide](https://proteowizard.sourceforge.io/download.html). Check vendor-format support and dependencies rather than assuming that any msconvert build can read the input.
2. Provide the user with current native installation instructions and source links, and let them install msconvert. Do not install the converter or its system dependencies automatically, run `sudo`, configure privileged services, or change system group membership on their behalf.
3. An optional [ProteoWizard Docker installation/conversion route](https://hub.docker.com/r/proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses) may also be suggested, using current instructions for the user's platform. Let the user perform Docker setup and conversion. Do not assume Docker is installed or accessible.
4. After the user installs a native executable, set `--msconvert-bin`, `MSCONVERT_BIN`, or `PATH` and verify `msconvert --help` before conversion. For user-run Docker conversion, continue with the exported mzML/MGF file; this wrapper invokes native executables. If installation is unavailable or the user prefers another host, accept files exported on the instrument workstation or another user-selected conversion host.

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

4. **Mixed-polarity `.raw` files** — most instrument methods acquire positive and negative ESI in one run. Split polarities before GNPS2 Everything Bagel feature finding. For local MZmine, `msms-feature-detect` can filter scans with `--polarity "+"` / `--polarity "-"` instead. To split with msconvert:
```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_pos.mzML --polarity positive
python .agents/skills/msms-preprocess/scripts/run.py \
    --input path/to/sample.raw --output results/sample_neg.mzML --polarity negative
```
`--polarity` defaults to `any` (no filtering) — safe to omit for files already acquired in a single polarity, or if you plan to filter polarity in the local MZmine route of `msms-feature-detect` instead.

5. **GNPS/MZmine feature MGF to `.ms`** — for GNPS2 Everything Bagel exports, verify MGF `SCANS` against feature-table `row ID` and add `--feature-id-field SCANS` to the command below. First inspect supplied raw/mzML activation metadata yourself to establish values, units and separate versus stepped acquisition, following [collision-energy handling](../../../docs/collision-energy.md); ask only when unresolved or conflicting. Preserve known stepped components with `--stepped-energies`; obtain original records or energy-separated spectra for unresolved mixtures. Once NCE versus absolute eV is verified, run `msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --output-dir converted`. Add `--raw-mzxml` with the raw mzXML files or their folder to read precursor-specific collision energies from referenced MS2 scans, or, for GNPS MGF that names only `SOURCE_FILE`, from that run's MS2 scans of the same precursor near `RTINSECONDS` (`--ms2-rt-window`). Convert the full MGF with `--feature-ids` to pick features: adducts, multimers and in-source fragments are assigned from all co-eluting features (`--ion-rt-window`, `--elucidate-all-ions`, `--no-ion-identity`). `SOURCE_SCAN` and `SCANS` can identify the MS1 apex and are never used as MS2 energy evidence. A `needs_energy` manifest row requires a verified `--energy` override or raw MS2 metadata. Bruker Q-TOF collision values are absolute eV set per precursor m/z; inspect the MS2 scan in raw mzXML. For the ICEBERG atlas route, process `converted/manifest.csv` with `msms-structure-elucidation batch`. For a mass-only PubChem search with either model, add `--mass-only --ms1-ppm <tolerance> --model <confirmed-model>` to `batch`; it skips formula prediction and atlas retrieval and writes the same batch review page. Follow the [scientific workflow](../../workflows/msms-elucidation.md).

## Constraints
- **Environment**: `preprocess` (`python ...`)
- **Supported input formats**: `.raw` (Thermo), `.d` (Bruker) — extend as needed
- **Supported output formats**: `mzML`, `MGF`
- **Converter**: user-configured native msconvert (ProteoWizard); vendor-format support depends on the installation and host platform.
- **Conversion host**: use a host with a suitable converter, or provide already exported mzML/MGF files.
- **Polarity filtering**: `--polarity {positive,negative,any}` maps to msconvert's own `polarity` filter. The local MZmine route of `msms-feature-detect` can filter polarity with `--polarity "+"/"-"`; split before the GNPS2 Everything Bagel route; MZmine's version needs the literal `+`/`-` symbol, not a word.

## References
- Chambers et al., "A cross-platform toolkit for mass spectrometry and proteomics", *Nature Biotechnology*, 2012. [DOI](https://doi.org/10.1038/nbt.2377)

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
