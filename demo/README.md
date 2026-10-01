# Clinical unknowns demo

Open the [interactive clinical unknowns demo](https://coley.mit.edu/msms-structure-elucidation.skills/demo/msms-structure-elucidation/). It combines every unknown currently in [ms-pred's public clinical spectra directory](https://github.com/coleygroup/ms-pred/tree/main/data/exp_specs/clinical): [plasma 583](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms), [CSF](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/csf_unknown.ms), and [plasma 198](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_198.ms). The other files in that directory are named standards, so they are not presented as unknowns.

The page shows 20 ranked public ICEBERG 2.1 PubChem atlas candidates for each unknown. Switch among all five collision energies, inspect mirror plots and clickable predicted fragments, compare candidates, and try the review controls. Trial notes remain in your browser. Similarity and explained intensity are ranking evidence, not identification probabilities. No licensed NIST predictions or checkpoints are included.

The spectrum provider confirmed that the 10, 20, 30, 40, and 50 values in **all three unknowns** are NCE, despite their `eV` file headers. The workflow converted them to rounded integer eV as follows:

| Unknown | Precursor m/z | Model energies (eV) | Public structures scored |
| --- | ---: | --- | ---: |
| Plasma 583 | 538.3864 | 11, 22, 32, 43, 54 | 158 |
| CSF | 260.1715 | 5, 10, 16, 21, 26 | 69 |
| Plasma 198 | 198.1235 | 4, 8, 12, 16, 20 | 8,682 |

Each source file was checked against the public ms-pred version by SHA-256. The [checked-in snapshot](msms-structure-elucidation/source.json.gz) contains the three analysis results and their fragment drawings; the [builder](../scripts/build_demo.py) validates source hashes, energy mapping, atlas provenance, fragment assignments, and absence of private machine paths before publishing.

## Build locally

From the repository root, run:

```bash
python scripts/build_demo.py --snapshot demo/msms-structure-elucidation/source.json.gz
```

Open `_site/demo/msms-structure-elucidation/index.html`. The site build uses only Python's standard library. Generated HTML and its provenance manifest stay uncommitted; [GitHub Actions](../.github/workflows/demo-pages.yml) rebuilds and publishes them on every push to `main`.

## Refresh the analysis snapshot

Use an environment with ms-pred, RDKit, and MSBuddy. Confirm the collision-energy unit with the provider before scoring. For these three spectra, use `--collision-unit NCE`, keep the ranked retrieval results, and run the builder with three `--result` and three `--spectrum` arguments in the table's order:

```bash
python scripts/build_demo.py \
  --result results/plasma_unknown_583/retrieval.json --spectrum plasma_unknown_583.ms \
  --result results/csf_unknown/retrieval.json --spectrum csf_unknown.ms \
  --result results/plasma_unknown_198/retrieval.json --spectrum plasma_unknown_198.ms
```

Review and commit the updated `source.json.gz`. The build workflow does not rerun retrieval. The organization's Pages host maps the repository project path under `coley.mit.edu`; this project page does not replace that site's homepage. Publishing the same demo at `https://iceberg-ms.mit.edu/demo/msms-structure-elucidation` would additionally require a redirect or proxy rule on the ICEBERG host.
