---
name: msms-retrieval
description: Rank structures from an experimental .ms spectrum against the public ICEBERG PubChem atlas, using ms-pred entropy similarity.
---
# Public atlas retrieval

If ms-pred setup is needed, use an existing checkout or clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if absent. Read that checkout's `README.md` **Install & setup** section and follow its instructions for the host's supported CPU or CUDA option. Verify the resulting interpreter can import `ms_pred`, `msbuddy`, and `rdkit`, then pass it with `--ms-pred-python` or `MS_PRED_PYTHON`. `bash setup_envs.sh` is a CPU venv helper; check the upstream README before using it.

Ask the user what collision energies they supplied and whether the labels are NCE or absolute eV. Do not infer units from `.ms` headers or numeric values. Run `msms-structure-elucidation run --input sample.ms --collision-unit NCE --output-dir results/sample --formula C12H21NO5` when the user confirms NCE; use `--collision-unit eV` for confirmed absolute eV. Omit `--formula` to infer hypotheses with MSBuddy; the report labels them inferred. The command downloads the formula MGF through the public `https://iceberg-ms.mit.edu/download_mgf` endpoint, converts NCE to eV with precursor m/z, pairs all experimental energies with nearby atlas eV spectra, ranks by their mean ms-pred entropy similarity, and writes `retrieval.json` and a standalone `report.html`. Inspect the recorded energy pairs and unmatched energies. `--atlas-mgf` accepts a local MGF for offline work. A missing formula or public atlas entry is an unavailable candidate set, not a zero similarity. The public atlas does not include licensed NIST structures.

Entropy similarity and explained peak intensity are evidence, not identification probability. Inspect unmatched high-intensity peaks and competing formula hypotheses before deciding whether to seek new candidates. Use `msms-visualize` to inspect annotated fragments and save human review notes on a local webpage. Use `msms-structure-review` for provider-neutral candidate review and optional GLACIER/ICEBERG scoring.
