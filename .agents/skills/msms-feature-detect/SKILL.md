---
name: msms-feature-detect
description: Detect and align LC-MS features with GNPS2 Everything Bagel or local MZmine, then export feature MS/MS spectra for structure elucidation.
---
# LC-MS feature detection

**GNPS2 Everything Bagel is the recommended default.** First check whether the user supplied a feature list, feature table/MGF or selected spectra; if so, use them and skip feature finding. Otherwise read `feature_detect` and the saved project `feature_finder` answer in `configs/local.yaml`. If no answer exists, ask once whether to use GNPS2 Everything Bagel (recommended), msconvert + MZmine, or the user's own workflow. Save their choice and reuse it on later runs as described in [pipeline preferences](../../../docs/pipeline-preferences.md). A suggested default alone does not count as a user answer.

| Route | Where it runs | Requirements |
|---|---|---|
| [GNPS2 Everything Bagel workflow](https://gnps2.org/workflowinput?workflowname=everything_bagel_workflow) | GNPS2 servers | GNPS2 account and data selected/uploaded to GNPS2 |
| [GNPS2 Everything Bagel Web](https://featurefinding.gnps2.org/) | User's browser | A compatible browser and files that fit its current memory/file limits; no local MZmine installation |
| msconvert + MZmine | User-selected host | Conversion when needed, then MZmine executable and `.mzuser` login; see [local instructions and settings](references/mzmine.md) |
| User workflow | As supplied by the user | Save its instructions under `feature_detect.custom.instructions`; import its feature table and spectra |

Run GNPS2 Everything Bagel through its hosted or browser interface. `scripts/run.py` is the local MZmine entry point.

## Prepare LC-MS data

Start with centroid mzML or mzXML containing MS1 chromatograms and the intended MS2 acquisitions. MGF alone cannot supply chromatograms for feature finding; use an existing feature MGF directly in the conversion step below. Keep compatible acquisition methods and polarities together, and process different chromatography methods or polarities separately. For polarity-switching files, split positive/negative scans with `msms-preprocess` before using GNPS2 Everything Bagel. Do not assume DIA or ion-mobility data are supported; check the current workflow documentation for the acquisition type.

Use `msms-preprocess` or files exported on the instrument workstation when conversion is needed. The browser interface also offers selected vendor raw inputs; check its current format support rather than treating every `.raw` or `.d` as interchangeable.

## Find features with GNPS2 Everything Bagel

1. Open the [GNPS2 workflow](https://gnps2.org/workflowinput?workflowname=everything_bagel_workflow), sign in and select the LC-MS files for one compatible dataset. Review the current parameters, including the feature reproducibility setting, then submit through GNPS2. Save the task URL, submitted parameters and input-file identities with the results.
2. Alternatively, open [GNPS2 Everything Bagel Web](https://featurefinding.gnps2.org/), select local files and choose **Find Features**. Feature finding runs in the browser. Optional library search transmits feature spectra to GNPS2; use it only if requested. Check the page's current input-size and browser-memory limits before a large run.
3. Wait for successful completion and inspect chromatograms, precursor assignments, polarity, retention times and features with MS/MS. Parameter optimization does not replace this review.
4. From the GNPS2 task, download `aligned_features.csv` and `aligned_features_filled.mgf` from the same run. From the browser version, use **Download All Features** and **Download Feature MGF**, or the GNPS2 FBMN bundle. Preserve the original files and run settings together.
5. Check that MGF `SCANS` values match feature-table `row ID` values. Keep rows without MS/MS in the table as features without spectra, rather than counting them as scored molecules. Feature IDs are identifiers, not raw MS2 scan numbers.

## Export model inputs

Before conversion or scoring, reuse the matching dataset/spectrum confirmation for **NCE or absolute eV**, or ask once and save the answer with its input context. Inspect the MGF's collision-energy fields and spectrum merge settings. A consensus spectrum combining different energies cannot be labelled as one verified energy; obtain an energy-separated export or its original MS2 spectra before model scoring. Do not invent an energy for missing metadata.

For an export whose `SCANS` matches the table's `row ID`, use:

```bash
msms-structure-elucidation convert-mgf \
    --input /path/to/aligned_features_filled.mgf \
    --feature-id-field SCANS \
    --collision-unit eV \
    --output-dir results/features/converted
```

Here `eV` is an example of a user-confirmed unit. The command writes `.ms` files for eligible spectra and `manifest.csv`/`manifest.json`, preserving the chosen feature IDs. The explicit field selection also applies to `--feature-ids` and ion-identity relationships. Match IDs against the feature table before continuing; titles or entry order need not equal row IDs.

When energy is missing, the manifest records `needs_energy`. Use `--raw-mzxml` only when the export can be linked to the correct source run and MS2 scans; see [energy provenance](../../../docs/technical-guide.md#lc-ms-features-and-mgf-conversion). Use `--energy` only for a verified single-energy spectrum. Aligned feature IDs must never be passed off as raw MS2 scan numbers.

Continue with `msms-structure-elucidation batch --manifest results/features/converted/manifest.csv --output-dir results/features/analysis` after confirming the prediction model and weights. For mass-only PubChem prediction, add `--mass-only --ms1-ppm <tolerance> --model <confirmed-model>`. Preserve GNPS2 library annotations separately from model-scored candidate rankings.

## References

- [GNPS2 Analysis Hub](https://gnps2.org/homepage): current workflow and browser links.
- Shin et al., "The Everything Bagel Feature Finder: Ultra-fast automated feature finding for untargeted metabolomics", bioRxiv, 2026. [DOI](https://doi.org/10.64898/2026.08.17.744735)
- [GNPS2 feature-table/MGF conventions](https://apps.gnps2.org/drug_readouts): GNPS2 Everything Bagel exports and `SCANS`/`row ID` correspondence.
- [Local MZmine instructions and reference](references/mzmine.md).
