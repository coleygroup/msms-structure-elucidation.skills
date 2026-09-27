# Plasma unknown 583 demo

The [interactive demo](msms-structure-elucidation/index.html) is a static export of one real retrieval result. It contains 20 ranked candidates, all five experimental collision energies, mirror plots, clickable predicted-fragment highlights, candidate comparison, and trial review notes stored only in each visitor's browser. No local Flask server or model is used by visitors.

The spectrum is [`plasma_unknown_583.ms`](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms). Its provider confirmed that the supplied 10, 20, 30, 40, and 50 values are **NCE**, even though the file labels them eV. At precursor m/z 538.3864, the workflow converted them to rounded model energies of 11, 22, 32, 43, and 54 eV. The result uses public ICEBERG 2.1 PubChem atlas spectra; it contains no licensed NIST predictions or checkpoints.

To rebuild from an existing ms-pred environment, download the source spectrum and run from the repository root:

```bash
msms-structure-elucidation run --input plasma_unknown_583.ms --collision-unit NCE --output-dir results/plasma_unknown_583 --top-k 20
python scripts/build_demo.py --spectrum plasma_unknown_583.ms --result results/plasma_unknown_583/retrieval.json
```

The builder checks the normalized source hash, the confirmed energy mapping, public candidate sources, and fragment IDs before writing `demo/msms-structure-elucidation/index.html` and `manifest.json`. It removes the analysis machine's local paths from the public snapshot. The HTML can be opened directly from disk; it does not fetch data from the atlas.

GitHub Pages publishes only these two files through [demo-pages.yml](../.github/workflows/demo-pages.yml). GitHub Pages must be enabled for this repository with **GitHub Actions** as the publishing source. The organization's Pages host currently redirects `coleygroup.github.io` to `coley.mit.edu`, so the expected project URL is `https://coley.mit.edu/msms-structure-elucidation.skills/demo/msms-structure-elucidation/` after a successful workflow run. Serving that page at `https://iceberg-ms.mit.edu/demo/msms-structure-elucidation` additionally requires a redirect or reverse-proxy rule on the ICEBERG host.
