# Local MZmine feature detection

## Prerequisites

Provide an existing MZmine executable and a `.mzuser` login file through `--mzmine-bin` and `--mzmine-user`, or the `MZMINE_BIN` and `MZMINE_USER` environment variables. Configure account access using the installed MZmine version's instructions. The Python wrapper uses `pyteomics` to read scan times, and PyYAML for optional per-sample configuration.

## Detect features

```bash
python .agents/skills/msms-feature-detect/scripts/run.py \
    --input /path/to/sample.mzML \
    --output-dir results/features \
    --mzmine-bin /path/to/mzmine \
    --mzmine-user /path/to/user.mzuser
```

Start with mzML containing the required MS1/MS2 scans. An MGF cannot replace LC-MS chromatograms for feature detection; use an existing feature MGF directly with `convert-mgf`.

The wrapper writes a batch XML and `<input-stem>_sirius.mgf` in the output directory. It imports scans, detects masses, builds chromatograms, smooths and deconvolves peaks, associates MS2 scans, then exports feature spectra.

For mixed-polarity data, run separately with `--polarity "+"` and `--polarity "-"`. These runs receive `_pos` and `_neg` output suffixes; verify `IONMODE` in each exported MGF. Use the accepted literal symbols rather than names for the polarity.

## Raw input and multiple samples

Convert raw data before invoking MZmine:

```bash
python .agents/skills/msms-preprocess/scripts/run.py \
    --input /path/to/sample.raw --output results/sample.mzML \
    --msconvert-bin /path/to/msconvert
```

Run feature detection on each exported mzML, using a separate output directory for each sample. If native conversion is unavailable on this host, start from files exported on the instrument workstation.

## Retention-time range and sample configuration

The wrapper reads the input's scan retention times and pads the detected range by `--rt-crop-margin` (default 0.1 min). For a selected range, pass a per-sample YAML file with `--config`:

```yaml
input: /path/to/sample.mzML
rt_range: [5.0, 15.0]  # minutes; optional
notes: "Sample context"  # printed to stderr
```

`--input` overrides the YAML input path. Keep sample-specific settings and executable paths outside the shared templates.

## Settings reference

| Parameter | Default | Meaning |
|---|---|---|
| `--ms1-noise-level` | 1000.0 | MS1 centroid noise floor (absolute intensity) |
| `--ms2-noise-level` | 100.0 | MS2 centroid noise floor |
| `--mz-tol-abs` | 0.02 Da | Absolute m/z tolerance used for scan-to-scan chromatogram linking, MS1↔MS2 precursor pairing, and MS2 merge-across-scans |
| `--mz-tol-ppm` | 10.0 ppm | Relative component of the same tolerance (whichever is looser wins, per MZmine convention) |
| `--min-consecutive-scans` | 4 | Minimum consecutive MS1 scans required to call something a chromatogram |
| `--min-consecutive-intensity` | 5000.0 | Minimum intensity for a scan to count toward the consecutive-scan requirement |
| `--min-feature-height` | 500000.0 | Minimum apex intensity to keep a feature |
| `--isolation-window-width` | 4.0 Da | Precursor isolation window width used when merging MS2 scans for export (informational default; real per-scan isolation windows are read from the mzML during MS1↔MS2 pairing) |
| `--rt-crop-margin` | 0.1 min | Padding added around the file's own detected [min, max] scan RT before cropping |
| retention time filter (fixed) | 0.08 min (4.8 s) | MS2 scan must fall within the feature's RT edges ± this margin to be paired |
| deconvolution algorithm (fixed) | local minimum search | Splits a chromatogram into features at local intensity minima |


## Validate the export

- Review noise, intensity and mass-tolerance settings for the instrument and sample. Feature counts depend on these thresholds.
- Confirm the output MGF is non-empty and contains the intended polarity, retention-time range and precursor assignments.
- Compare exported feature IDs with the feature table and inspect features with multiple associated MS2 scans. Review MZmine's merge/select/export settings if these features are absent.
- Inspect the generated batch XML against the installed MZmine version. The wrapper uses `--ignore-parameter-warnings`, so verify that version-specific export defaults produce the intended spectra before processing a full batch.

## Reference

Schmid et al., "Integrative analysis of multimodal mass spectrometry data in MZmine 3", *Nature Biotechnology*, 2023. [DOI](https://doi.org/10.1038/s41587-023-01690-2)
