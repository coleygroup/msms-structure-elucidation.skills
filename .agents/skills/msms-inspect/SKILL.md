---
name: msms-inspect
description: Interactive TIC/MS1/MS2 chromatogram viewer for mzML files; lets you browse retention time, inspect precursor masses, and export individual spectra as .ms files.
---

# msms-inspect

## Goal
Visually explore an LC-MS/MS run: browse the total ion chromatogram (TIC), inspect MS1 spectra at any retention time, drill into MS2 fragmentation for any precursor, and export selected spectra as ms-pred `.ms` files for downstream elucidation.

## Instructions

1. **Launch the inspector**:
```bash
# Env: preprocess
python .agents/skills/msms-inspect/scripts/run.py \
    --input results/sample.mzML
```

2. **With export enabled** (press `E` on a selected MS2 to save it as `.ms`):
```bash
# Env: preprocess
python .agents/skills/msms-inspect/scripts/run.py \
    --input results/sample.mzML \
    --export-dir results/precursors/
```

## Usage

The viewer has three panels stacked vertically:

| Panel | What it shows | Interaction |
|-------|--------------|-------------|
| TIC | Total ion current vs. retention time | Click to jump to that RT |
| MS1 | Full mass spectrum at the selected RT | Click a peak to load its MS2 |
| MS2 | Fragmentation spectrum for the selected precursor | Press `E` to export as `.ms` |

## Constraints
- **Environment**: `preprocess` (`python ...`)
- **Input**: `.mzML` file (convert from `.raw` first using `msms-preprocess`)
- **Display**: requires a display (X11/Wayland). On headless servers, forward X11 via `ssh -X` or use a Jupyter notebook.
- **Export tolerance**: MS2 scans within ±0.01 Da of the clicked precursor m/z are merged into one `.ms` file.

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
