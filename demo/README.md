# Plasma unknown 583 demo

The demo is generated from the checked-in [public analysis snapshot](msms-structure-elucidation/source.json.gz). It contains 20 ranked candidates, all five experimental collision energies, mirror plots, clickable predicted-fragment highlights, candidate comparison, and trial review notes stored only in each visitor's browser. Visitors need no local Flask server or prediction model.

The source spectrum is [plasma_unknown_583.ms](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms). Its provider confirmed that the supplied 10, 20, 30, 40, and 50 values are **NCE**, even though the file labels them eV. At precursor m/z 538.3864, the workflow converted them to rounded model energies of 11, 22, 32, 43, and 54 eV. The analysis inferred three formulas and scored 158 public ICEBERG 2.1 PubChem atlas structures. The snapshot contains no licensed NIST predictions or checkpoints.

## Build the page locally

From the repository root, run:

```bash
python scripts/build_demo.py --snapshot demo/msms-structure-elucidation/source.json.gz
```

Open `_site/demo/msms-structure-elucidation/index.html` in a browser. This step uses only Python's standard library. It writes the HTML and its provenance manifest to `_site/`; neither generated file is committed.

## Refresh the analysis snapshot

When the underlying retrieval result changes, use an environment with ms-pred and RDKit, download the source spectrum, and run:

```bash
msms-structure-elucidation run --input plasma_unknown_583.ms --collision-unit NCE --output-dir results/plasma_unknown_583 --top-k 20
python scripts/build_demo.py --spectrum plasma_unknown_583.ms --result results/plasma_unknown_583/retrieval.json
```

Review and commit the updated `source.json.gz`. The builder checks the spectrum hash, confirmed NCE-to-eV mapping, public candidate sources, fragment IDs, and absence of local machine paths. It does not rerun retrieval on routine site builds.

## Publication

[demo-pages.yml](../.github/workflows/demo-pages.yml) rebuilds the HTML from the snapshot on **every push to main** and publishes only the generated page and manifest through GitHub Pages. GitHub Pages still needs to be enabled with **GitHub Actions** as the publishing source. The organization's Pages host currently redirects `coleygroup.github.io` to `coley.mit.edu`, so the expected project URL is `https://coley.mit.edu/msms-structure-elucidation.skills/demo/msms-structure-elucidation/` after a successful workflow run. Serving it at `https://iceberg-ms.mit.edu/demo/msms-structure-elucidation` additionally requires a redirect or reverse-proxy rule on the ICEBERG host.
