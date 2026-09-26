---
name: msms-feature-detect
description: Automatically detect LC-MS features in an mzML file and export per-feature averaged MS2 spectra, replacing manual precursor/scan picking.
---

# msms-feature-detect

## Goal
Given an mzML file, automatically detect chromatographic features (mass detection → chromatogram building → peak deconvolution), pair each feature with its MS2 scans (matched by real instrument isolation window and retention-time overlap, not a manual guess), average the MS2 fragments across matching scans, and export one spectrum per feature in SIRIUS/`.mgf` format for FRIGID/ICEBERG. Runs [MZmine 4](https://mzmine.github.io/) headlessly.

## Prerequisites

1. **MZmine 4 binary.** Extract without root (no system install needed):
   ```bash
   dpkg-deb -x mzmine_4.7.8_amd64.deb ~/mzmine-4.7.8
   ```
   Binary ends up at `~/mzmine-4.7.8/opt/mzmine/bin/mzmine`.

2. **MZmine user account.** MZmine 4 requires a free account even for headless CLI use — sign up at [mzmine.github.io](https://mzmine.github.io/), then log in once interactively:
   ```bash
   ~/mzmine-4.7.8/opt/mzmine/bin/mzmine -login-console
   ```
   This writes a `.mzuser` file to `~/.mzmine/users/<username>.mzuser`, reusable for future headless runs (offline, for a limited duration per MZmine's docs).

## Instructions

```bash
# Env: preprocess
python .agents/skills/msms-feature-detect/scripts/run.py \
    --input path/to/sample.mzML \
    --output-dir results/ \
    --mzmine-bin ~/mzmine-4.7.8/opt/mzmine/bin/mzmine \
    --mzmine-user ~/.mzmine/users/<username>.mzuser
```

`--input` also accepts `.mgf`. `--mzmine-bin` / `--mzmine-user` can instead be set via `MZMINE_BIN` / `MZMINE_USER` in `.env`.

Output: `<output-dir>/<input-stem>_sirius.mgf` — one spectrum block per detected feature, MS2 fragments averaged across all matching scans.

### Auto RT cropping

Before building the batch, the script reads the input file's own first/last MS1 scan retention time and crops to that range (padded by `--rt-crop-margin`, default 0.1 min). This replaces mzMine GUI's "Auto range" button, which bakes a fixed number into a saved `.mzbatch` and does not recompute per file — see `--rt-crop-margin` to widen the padding.

### Per-sample config file

Instead of `--input`, pass `--config path/to/sample.yaml`:

```yaml
input: path/to/sample.mzML
notes: "Free-text context for this sample — printed to stderr, not used by the pipeline."
rt_range: [5.0, 15.0]  # optional — overrides auto-detected RT crop, minutes
```

`--input` on the command line overrides `input:` in the config if both are given.

### Batch-generate per-file .mzbatch from a folder of .raw files

```bash
.agents/skills/msms-feature-detect/scripts/make_batches.sh [-p pos|neg|both] [-r] [-b mzmine-bin] [-u mzmine-user] path/to/raw_folder
```

For each `<name>.raw` (or `<name>.mzML`) in `raw_folder`, writes `raw_folder_processed/<name>/<name>_pos.mzbatch` and/or `<name>_neg.mzbatch` (derived from `resources/pos.mzbatch` / `neg.mzbatch`, one file per batch — MZmine must not mix polarities in one run), with all output paths (metadata, feature table, SIRIUS export, annotations) pointed at that same per-sample subfolder. `-p` defaults to `both`. Pass `-r` to also invoke `mzmine` on each generated batch immediately (needs `-b`/`-u` or `MZMINE_BIN`/`MZMINE_USER` in `.env` — see Prerequisites above); without `-r` it only writes the `.mzbatch` files for you to run yourself.

### End-to-end from a folder of .raw files (skips MZmine's broken built-in MSConvert)

```bash
.agents/skills/msms-feature-detect/scripts/raw_to_batches.sh [-p pos|neg|both] [-r] [-b mzmine-bin] [-u mzmine-user] path/to/raw_folder
```

Converts each `.raw` to `.mzML` first (via `msms-preprocess/scripts/run.py`, which shells out to docker msconvert directly), writing to `raw_folder_mzml/`, then runs `make_batches.sh` against that folder. Use this instead of pointing `make_batches.sh` straight at `.raw` files — see Known issues below.

## How it works (the pipeline)

1. **Import** the mzML.
2. **Mass detection** (MS1 and MS2 separately) — centroid picking above a noise floor.
3. **Chromatogram building** (ADAP) — groups consecutive MS1 scans with matching m/z into candidate chromatographic traces.
4. **Smoothing** (Savitzky-Golay) then **deconvolution** (local minimum search) — splits traces into individual features (peak apex + boundaries), and pairs each feature with MS2 scans whose precursor m/z and retention time fall inside it.
5. **Export** (SIRIUS module) — merges/averages the paired MS2 scans per feature (weighted-average m/z, summed intensity) and writes one `.mgf` block per feature.

This replaces the old manual notebook workflow (`msms-inspect/examples/explore.ipynb`), where a person had to eyeball the TIC, pick an RT, pick a precursor m/z, and guess a tolerance window.

## Settings reference (for domain-expert review)

| Parameter | Default | Meaning |
|---|---|---|
| `--ms1-noise-level` | 1000.0 | MS1 centroid noise floor (absolute intensity) |
| `--ms2-noise-level` | 100.0 | MS2 centroid noise floor |
| `--mz-tol-abs` | 0.02 Da | Absolute m/z tolerance used for scan-to-scan chromatogram linking, MS1↔MS2 precursor pairing, and MS2 merge-across-scans. Widened from a naive 0.003 Da after finding this instrument's scan-to-scan mass calibration drifts by up to ~0.09 Da |
| `--mz-tol-ppm` | 10.0 ppm | Relative component of the same tolerance (whichever is looser wins, per MZmine convention) |
| `--min-consecutive-scans` | 4 | Minimum consecutive MS1 scans required to call something a chromatogram |
| `--min-consecutive-intensity` | 5000.0 | Minimum intensity for a scan to count toward the consecutive-scan requirement |
| `--min-feature-height` | 500000.0 | Minimum apex intensity to keep a feature |
| `--isolation-window-width` | 4.0 Da | Precursor isolation window width used when merging MS2 scans for export (informational default; real per-scan isolation windows are read from the mzML during MS1↔MS2 pairing) |
| `--rt-crop-margin` | 0.1 min | Padding added around the file's own detected [min, max] scan RT before cropping |
| retention time filter (fixed) | 0.08 min (4.8 s) | MS2 scan must fall within the feature's RT edges ± this margin to be paired |
| deconvolution algorithm (fixed) | local minimum search | Splits a chromatogram into features at local intensity minima |

## Constraints

- **Environment**: `preprocess` for the Python wrapper (`python ...`). MZmine itself is a separately-installed Java binary, not a an external environment manager dependency.
- **Input format**: `.mzML` or `.mgf` — run `msms-preprocess` first if starting from `.raw`.
- **`--ignore-parameter-warnings` is used internally.** The batch template was authored against MZmine 3 semantics; MZmine 4 warns that some parameter defaults changed (SIRIUS export intensity normalization, MS2 merge/select) and refuses to run without this flag. We verified end-to-end (see below) that the resulting defaults produce correct, sane output on the sample file — MZmine's own docs call this flag "not recommended," so if a run's output looks wrong, this is the first thing to revisit.
- **Mixed polarity: pass `--polarity + ` and `--polarity -` (run twice), not `--polarity Any`.** Most instrument methods acquire both polarities in one `.raw`/`.mzML` file, and chromatogram building must not mix them — a compound's positive- and negative-mode signals are different physical events. The `Polarity` scan-filter parameter **must be the literal symbol `+`/`-`**: the word forms `Positive`/`Negative` and `POSITIVE`/`NEGATIVE` silently fall back to "Any" in this MZmine build with no error (verified — this was the actual bug in earlier testing, not a fundamental MZmine limitation). Verified end-to-end on `data/QE_SN01781L_150430-04.mzML`: `--polarity "+"` produced 314 features, all `IONMODE=POSITIVE`; `--polarity "-"` produced 200 features, all `IONMODE=NEGATIVE` — clean separation, no cross-contamination. Always cross-check the `IONMODE` field in the output `.mgf` after any change to this pipeline or MZmine version, since this class of bug fails silently.
- **Feature yield depends on thresholds.** On the sample file at the old `--min-feature-height` default (10000.0), 1288 raw features were detected but only 5 had a paired MS2 scan passing all thresholds and cleared export filtering; yield has not been re-measured at the current 500000.0 default. Lowering `--min-feature-height` / `--min-consecutive-intensity` will surface more (noisier) features.

## Known issues

- **This MZmine 4.7.8 build's built-in MSConvert hook does not work**, even with the `proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses` docker image pulled and present — import of a `.raw` file fails with `MSConvert not found. Please install MSConvert.` (verified on molgpu06 and molgpu08). Use `raw_to_batches.sh` (above), which pre-converts via docker msconvert directly instead of relying on MZmine to invoke it, or feed MZmine `.mzML` input in the first place.
- **SIRIUS export (`resources_isovalidated/pos.mzbatch`, and likely `resources/pos.mzbatch`) silently drops every feature with more than one paired MS2 scan**, despite `Merge & select fragment scans` being set to `preset_merged` / `REPRESENTATIVE_SCANS` (which should merge or pick a representative scan, not drop the feature). Verified across all 9 reaction runs in `data/260904_mzml_processed_isovalidated/`: in every run, 100% of features in the output `sirius.mgf` have `fragment_scans == 1` in `full_feature_table.csv`, and 0% have `fragment_scans > 1` — even though 28–90% of each run's total MS1 features have `fragment_scans > 1` and are fully absent from the `.mgf` (e.g. `luca_sarpong_2`: 399 features with `fragment_scans > 0`, only 66 exported, all with exactly 1 scan; `rxn_99`: 385 fragmented, 90 exported). This matches the flag above about MZmine 4 changing SIRIUS export MS2 merge/select defaults under `--ignore-parameter-warnings` — worth revisiting first. Next step: try `simple_merged` or `advanced` (`ACROSS_ENERGIES`/`ACROSS_SAMPLES`, `Intensity merge mode: MAXIMUM`) instead of `preset_merged`/`REPRESENTATIVE_SCANS`, and re-verify the `fragment_scans` vs `.mgf` feature-id overlap on one file before re-running the full batch.

## Verification performed
Ran end-to-end against `data/QE_SN01781L_150430-04.mzML` (4252 scans): import → mass detection → chromatogram building → smoothing → deconvolution → SIRIUS export completed in ~7.5s, producing a valid non-empty `.mgf` with real fragment peaks and correct per-feature MS/MS averaging (`SPECTYPE`, `SCANS`, weighted `PEPMASS`).

## References
- Schmid et al., "Integrative analysis of multimodal mass spectrometry data in MZmine 3", *Nature Biotechnology*, 2023. [DOI](https://doi.org/10.1038/s41587-023-01690-2)

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
