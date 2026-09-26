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

Ask the spectrum provider whether the supplied collision energies are NCE or absolute eV, then pass the confirmed `--collision-unit NCE` or `--collision-unit eV`. File headers can be wrong; the chosen unit and any conflict are recorded. NCE is converted with precursor m/z. Experimental and atlas energies are rounded to integer eV and paired only when those integers are equal. Every supplied energy must contribute to a candidate score. If an atlas formula has any missing energy, the workflow simulates **every atlas structure for that formula at every supplied energy** with local ICEBERG in resumable shards. It never substitutes an off-energy atlas spectrum. Supply an ms-pred checkout and ICEBERG weights before that fallback is needed. Without those assets, `retrieval.json` records `blocked_model_assets`, the underlying error is printed, and the command exits nonzero.

Omit `--formula` to search the top three MSBuddy hypotheses. Use `--formulas-file formulas.txt` for a user supplied list, one formula per line. `--atlas-mgf` is for a single formula offline. The command writes `retrieval.json` and an interactive offline `report.html`. Formula outcomes are recorded separately in `formula_results`. `ranked`, `needs_formula`, `no_atlas_coverage`, and `no_candidates` are valid outcomes with exit code 0; operational errors return nonzero. Entropy similarity is a ranking statistic, not identification confidence. Explained intensity breaks close score ties, and candidates with no diagnostic matched peaks are flagged as potentially indistinguishable.

The public atlas endpoint is `https://iceberg-ms.mit.edu/download_mgf?formula=...&adduct=...`. Its spectra are ICEBERG predictions for PubChem structures; licensed NIST structures are excluded. No atlas files or model weights are bundled here.

## Optional models

`ms-pred` supplies GLACIER, ICEBERG, and spectral utilities. Clone the [official ms-pred repository](https://github.com/coleygroup/ms-pred), read its README, and follow its environment setup for your host. Set `--ms-pred-dir` to that checkout for local inference; the worker runs there because GLACIER uses a relative script path. Use the open-source MassSpecGym checkpoint links in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme), or provide local licensed NIST checkpoints if your license permits. GLACIER needs one checkpoint; ICEBERG needs generation and intensity checkpoints. Model calls receive rounded integer eV with `nce=False`. The `.ms` `>instrumentation` value is used for model calls, with `--instrument` taking priority. Use `--cuda-devices 0` or `MSMS_CUDA_DEVICES=0` to select a GPU.

Put local paths and defaults in [configs/default.yaml](configs/default.yaml), or pass `--config path/to/config.yaml`. CLI options override config; `MS_PRED_DIR`, `MS_PRED_PYTHON`, and `MSMS_CUDA_DEVICES` are also supported. Paths in the YAML file are relative to that file. The public GLACIER checkpoint can be incompatible with newer ms-pred feature layouts: the preflight compares checkpoint atom feature width with the selected checkout before inference and reports the mismatch. In a local probe, the public checkpoint expected 88 base atom features while ms-pred commit `577b452` produced 89; choose a compatible checkout or checkpoint. The same checkpoint may produce better or different results than other public weights, and licensed NIST weights stay local.

FRIGID is installed separately when needed:

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
# To also download the public FRIGID checkpoint archive (about 2.8 GB):
MSMS_DOWNLOAD_FRIGID_WEIGHTS=1 bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

You may instead provide your own checkpoint files. The [FRIGID public weights](https://zenodo.org/records/19685145) do not include licensed NIST assets. Set the FRIGID interpreter, checkout, and checkpoint paths as described in `msms-denovo/SKILL.md`. No setup script modifies an existing `ms-pred` or FRIGID checkout.

## Agent portability

The same skills live in `.agents/skills` and are linked under `.claude/skills` for Claude Code. `AGENTS.md` contains the shared agent guidance and points to the scientific workflow. The CLI uses no LLM or MCP dependency; an agent can review `retrieval.json` and supply new candidate SMILES through the `msms-structure-review` skill. Optional MCP servers are separate adapters.

For raw files or mzML, export a feature-level `.ms` spectrum with the preprocessing and inspection skills first. For GNPS/MZmine MGF:

```bash
msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --raw-mzxml raw.mzXML --output-dir converted
msms-structure-elucidation batch --manifest converted/manifest.csv --output-dir results/batch --ms-pred-dir /path/to/ms-pred
```

The converter includes MS2 entries only. `SOURCE_SCAN` or `SCANS` can point to the MS1 apex, so the converter never uses them to infer MS2 collision energy. It reads MGF collision fields or the `collisionEnergy` of referenced `MERGED_SCANS`/MS2 scans in mzXML. Missing or inconsistent values produce `needs_energy` in the manifest; use `--energy` only after confirming the value. Bruker Q-TOF collision settings are usually absolute eV, set per precursor m/z; check the actual MS2 scan in raw mzXML and confirm the unit with the provider. Batch shares an atlas cache, resumes completed features, defaults to one worker and one model job, and checks available memory before scheduling. Set `--max-workers`, `--max-model-jobs`, and `--min-free-memory-gb` as needed. Install `.[batch]` for `psutil`. See `.agents/workflows/msms-elucidation.md` for the full route. The supplied CSF example is `ms-pred/data/exp_specs/clinical/csf_unknown.ms`.

## Interactive fragment review

After retrieval or candidate review, open the separate `msms-visualize` skill:

```bash
"${MS_PRED_PYTHON:-python}" -m pip install -e '.[visualize]'
msms-structure-elucidation visualize --result results/sample/retrieval.json
```

Open the printed localhost URL to select candidates and collision energies, inspect annotated predicted fragments against experimental peaks, and save candidate decisions and fragment comments on the webpage. Notes are written to `review_notes.json` beside the result; `retrieval.json` remains the computed record. The viewer uses cached atlas predictions and makes no model or network calls. Existing results can recover fragment IDs from their cached atlas MGF; pass `--atlas-mgf` if that file was moved.
