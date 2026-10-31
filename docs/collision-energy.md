# Collision-energy settings and stepped acquisition

## Inspect acquisition metadata first

When raw or mzML files are supplied, first inspect the acquisition method, precursor activation records and available vendor scan/trailer metadata. Reuse a setting already verified for the same inputs and scan group. For proprietary raw data, use an available vendor reader or a metadata-preserving conversion through `msms-preprocess`; do not install privileged converters automatically.

For mzML, inspect MS2 activation records before asking the user:

```bash
msms-structure-elucidation inspect-energy --input sample.mzML \
  --precursor-mz 500.25 --rt-seconds 300
```

The optional precursor and retention-time filters select the relevant MS2 scans. The command reports scan IDs, source parameters, units, acquisition mode and complete energy lists. It reads explicit PSI-MS [normalized collision energy and collision energy terms](https://github.com/HUPO-PSI/psi-ms-CV/blob/master/psi-ms.obo), including referenced parameter groups. Absolute collision energy requires explicit eV units; normalized collision energy identifies NCE. Explicit stepped activation metadata can establish the component list. Keep settings scoped to the actual precursor/scans rather than adopting the first file-wide value.

If settings or units are missing, contradictory, expressed as an unresolved center/spread, or lost during conversion, ask the user. Do not infer them from the instrument name, filename, numeric range or a converted `.ms` header alone. Different energies in separate scans do not establish a stepped acquisition. Metadata values establish acquisition settings, not a uniquely calibrated physical fragmentation spectrum.

Save verified settings with their source and input/scan context. Use `preferences --answer collision_unit NCE --source metadata --scope dataset-01 --context-json '{"input_sha256":"<actual hash>","scan_ids":["<actual scan>"]}'` only after resolving the acquisition records. Use `--source user` for user answers. Preserve evidence for acquisition mode and component energies too.

## Separate spectra versus one stepped spectrum

For separate experimental spectra acquired at 20, 40 and 60 NCE, retain three collision blocks. Predict each corresponding energy and compare each experimental spectrum with its own prediction; aggregate the per-energy scores.

For **one measured stepped spectrum at 20/40/60 NCE**, the standard process is:

1. Preserve the explicit component list and unit.
2. Convert each NCE component to model eV using the precursor m/z and upstream `nce_to_ev`; round for model inference. Predict every required energy, or reuse matching ICEBERG atlas components under the selected atlas policy.
3. Combine component predictions with equal weights, summing matching peak intensities, then base-peak normalize the combined spectrum. This has the same normalized result as an equal-weight arithmetic mean. Do not independently renormalize components at this merge stage or replace the components with their mean energy.
4. Score the combined prediction once against the single measured spectrum. Keep the component predictions, settings and merge provenance for inspection. Preserve fragment distinctions when peaks of the same mass have different assignments.

The current stepped mode implements equal component weights. If acquisition metadata specifies unequal dwell/energy weights, request an appropriate supported input/processing route; do not silently substitute equal weights. If several component settings round to one model energy, predict that energy once but retain its repeated contribution in the merge.

## Prepare and run stepped inputs

For feature MGF with verified stepped settings, convert the single measured composite using:

```bash
msms-structure-elucidation convert-mgf --input features.mgf \
  --collision-unit NCE --stepped-energies 20 40 60 --output-dir converted
```

`--raw-spectra` (also accepted as `--raw-mzxml`) can instead supply mzML/mzXML files with linked MS2 energy records. MGF `STEPPED_COLLISION_ENERGIES` is an explicit list; a plain ambiguous multi-number `COLLISION_ENERGY` is not silently truncated to its first value. Do not combine unrelated energies/scans under one stepped setting merely because feature finding merged their peaks.

The `.ms` representation uses explicit metadata and exactly one observed collision block:

```text
>compound example
>parentmass 500.25
>ionization [M+H]+
>stepped_collision_energies 20,40,60

>collision 20 NCE
100.0 10
150.0 5
```

With `stepped_collision_energies` present, the scalar block label identifies the measured block; it does not limit scoring to that energy. The complete component list controls prediction and merging. Run `run`, `batch`, `review`, or MCP `score_candidates` with the verified collision unit as usual. Keep distinct stepped schemes in separate `.ms` inputs.

For forward prediction without an experimental spectrum, pass `--stepped` to the `msms-predict/scripts/predict_msms.py` launcher, or `stepped: true` to MCP `predict_spectra`, together with the explicit energy list and unit. Component spectra and a `stepped` combined spectrum are returned. Without that flag, predictions stay energy-resolved.
