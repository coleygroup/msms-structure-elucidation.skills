# MS/MS structure elucidation

Portable tools and skills for assigning candidate small-molecule structures from experimental MS/MS. The default path ranks precomputed ICEBERG 2.1 spectra from the public PubChem atlas; expensive local simulation and de novo generation are optional.

## Quick start

```bash
python -m pip install -e .
# If ms-pred is already installed, point to that Python. Otherwise:
bash setup_envs.sh
export MS_PRED_PYTHON="$PWD/.cache/ms-pred-venv/bin/python"
msms-structure-elucidation run --input sample.ms --collision-unit NCE --output-dir results/sample --formula C12H21NO5
```

Ask the spectrum provider whether the supplied collision energies are NCE or absolute eV, then pass the confirmed `--collision-unit NCE` or `--collision-unit eV`. File headers can be wrong; the chosen unit and any header conflict are recorded. The public atlas labels energies in eV. NCE is converted using precursor m/z, and all experimental energies are paired with nearby atlas energies (at most 2 eV apart) for scoring. Omit `--formula` for MSBuddy formula inference. The output marks inferred formulas. An offline atlas MGF can be passed with `--atlas-mgf`. The command writes `retrieval.json` and a self-contained interactive `report.html` with structure cards and collision-energy mirror spectra. Similarity is a ranking statistic, not identification confidence. A missing formula or absent public atlas entry is reported separately from a poor match.

The public atlas endpoint is `https://iceberg-ms.mit.edu/download_mgf?formula=...&adduct=...`. Its spectra are ICEBERG predictions for PubChem structures; licensed NIST structures are excluded. No atlas files or model weights are bundled here.

## Optional models

`ms-pred` supplies GLACIER (default forward simulator), ICEBERG, and spectral utilities. Use the open-source MassSpecGym checkpoint links in [ms-pred's README](https://github.com/coleygroup/ms-pred#readme), or provide local licensed NIST checkpoints if your license permits. GLACIER needs one checkpoint; ICEBERG needs generation and intensity checkpoints. Pass their paths to the review skill. Model inference runs only for candidate structures missing from the precomputed atlas. Model calls always receive rounded integer eV values; confirmed NCE inputs are converted with precursor m/z before rounding, and the call uses `nce=False`.

FRIGID is installed separately when needed:

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
# To also download the public FRIGID checkpoint archive (about 2.8 GB):
MSMS_DOWNLOAD_FRIGID_WEIGHTS=1 bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

You may instead provide your own checkpoint files. The [FRIGID public weights](https://zenodo.org/records/19685145) do not include licensed NIST assets. Set the FRIGID interpreter, checkout, and checkpoint paths as described in `msms-denovo/SKILL.md`. No setup script modifies an existing `ms-pred` or FRIGID checkout.

## Agent portability

The same skills live in `.agents/skills` and are linked under `.claude/skills` for Claude Code. `AGENTS.md` contains the shared agent guidance and points to the scientific workflow. The CLI uses no LLM or MCP dependency; an agent can review `retrieval.json` and supply new candidate SMILES through the `msms-structure-review` skill. Optional MCP servers are separate adapters.

For raw files or mzML, export a feature-level `.ms` spectrum with the preprocessing and inspection skills first. See `.agents/workflows/msms-elucidation.md` for the full route. The supplied CSF example is `ms-pred/data/exp_specs/clinical/csf_unknown.ms`.

## Interactive fragment review

After retrieval or candidate review, open the separate `msms-visualize` skill:

```bash
"${MS_PRED_PYTHON:-python}" -m pip install -e '.[visualize]'
msms-structure-elucidation visualize --result results/sample/retrieval.json
# Several unknowns: one page with a searchable list of all of them
msms-structure-elucidation visualize --result results/*/retrieval.json
```

Open the printed localhost URL for an overview of every unknown and a review workspace: hover or click peaks in the mirror plot to highlight predicted fragments on the structure, compare candidates, and record decisions and fragment comments, which autosave. Add `--export review_report.html` to write one self-contained read-only report instead of serving. Notes are written to `review_notes.json` beside the result; `retrieval.json` remains the computed record. The viewer uses cached atlas predictions and makes no model or network calls. Existing results can recover fragment IDs from their cached atlas MGF; pass `--atlas-mgf` if that file was moved.
