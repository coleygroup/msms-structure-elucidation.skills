# Technical guide: MS/MS structure elucidation

Portable tools and skills for assigning candidate small-molecule structures from experimental MS/MS. The default path ranks precomputed ICEBERG 2.1 spectra from the public PubChem atlas; expensive local simulation and de novo generation are optional.

## Manual CLI setup

Use an existing verified ms-pred environment when available. If setup is needed, clone [ms-pred](https://github.com/coleygroup/ms-pred), read its official README, and follow the environment instructions for your host. The `setup_envs.sh` script here is only a CPU venv helper.

```bash
python -m pip install -e .
# Point to the verified Python with ms-pred, MSBuddy, and RDKit.
export MS_PRED_PYTHON=/path/to/ms-pred-python
# Use NCE here only after the spectrum provider has confirmed it.
msms-structure-elucidation run --input sample.ms --collision-unit NCE --output-dir results/sample --formula C12H21NO5
```

Ask the spectrum provider whether the supplied collision energies are NCE or absolute eV, then pass the confirmed `--collision-unit NCE` or `--collision-unit eV`. File headers can be wrong; the chosen unit and any conflict are recorded. NCE is converted with precursor m/z. Experimental and atlas energies are rounded to integer eV and paired only when those integers are equal. Every supplied energy must contribute to a candidate score. If an atlas formula has any missing energy, the workflow simulates **every atlas structure for that formula at every supplied energy** with local ICEBERG in resumable shards. It never substitutes an off-energy atlas spectrum. Supply an ms-pred checkout and ICEBERG weights before that fallback is needed. Without those assets, `retrieval.json` records `blocked_model_assets`, the underlying error is printed, and the command exits nonzero.

Omit `--formula` to search the top three MSBuddy hypotheses. Use `--formulas-file formulas.txt` for a user supplied list, one formula per line. MSBuddy mass tolerances follow the instrument (Q-TOF 10/20 ppm, Orbitrap 5/10 ppm precursor/fragment; `--ms1-ppm`, `--ms2-ppm`). If MSBuddy proposes no formula, formulas of PubChem structures matching the precursor mass are searched instead. A result without atlas coverage records `next_step: iceberg-pubchem` (predict the formula's PubChem structures with `review --proposals pubchem`), and one with no formula anywhere records `next_step: review-frigid`. `--atlas-mgf` is for a single formula offline. The command writes `retrieval.json` and a basic offline `report.html`; use the richer viewer below for fragment inspection and notes. Formula outcomes are recorded separately in `formula_results`. `ranked`, `needs_formula`, `no_atlas_coverage`, and `no_candidates` are valid outcomes with exit code 0; operational errors return nonzero. Entropy similarity is a ranking statistic, not identification confidence. Explained intensity breaks close score ties, and candidates with no diagnostic matched peaks are flagged as potentially indistinguishable.

The public atlas endpoint is `https://iceberg-ms.mit.edu/download_mgf?formula=...&adduct=...`. Its spectra are ICEBERG predictions for PubChem structures; licensed NIST structures are excluded. No atlas files or model weights are bundled here.

## Host tuning

`msms-structure-elucidation setup` reads the CPU threads, RAM and GPUs, chooses ms-pred inference settings from measured anchors, and saves them to `configs/local.yaml`, which Git ignores. If ms-pred and ICEBERG checkpoints are available, it also times ICEBERG to choose batch_size. Every command applies that file on top of `configs/default.yaml` unless `--config` or `MSMS_CONFIG` is given. Use `--remote HOST --remote-repo PATH` to tune a GPU host over ssh, adding `--remote-prefix` for environment activation or a scheduler such as `srun`. See the [msms-setup skill](../.agents/skills/msms-setup/SKILL.md).

## Optional models

`ms-pred` supplies GLACIER, ICEBERG, and spectral utilities. Clone the [official ms-pred repository](https://github.com/coleygroup/ms-pred), read its README, and follow its environment setup for your host. Set `--ms-pred-dir` to that checkout for local inference; the worker runs there because GLACIER uses a relative script path. Use the open-source MassSpecGym checkpoint links in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme), or provide local licensed NIST checkpoints if your license permits. GLACIER needs one checkpoint; ICEBERG needs generation and intensity checkpoints. Model calls receive rounded integer eV with `nce=False`. The `.ms` `>instrumentation` value is used for model calls, with `--instrument` taking priority. Use `--cuda-devices 0` or `MSMS_CUDA_DEVICES=0` to select a GPU. Batch size and ms-pred worker counts (`--model-batch-size`, `--model-cpu-workers`, `--model-gpu-workers`) should match the GPU memory; the measured anchors (8 GB → 16, 24 GB → 128, 16 CPU and 2 GPU workers) are in `.agents/skills/msms-sim-iceberg/SKILL.md`.

Put local paths and defaults in [configs/default.yaml](../configs/default.yaml), or pass `--config path/to/config.yaml`. CLI options override config; `MS_PRED_DIR`, `MS_PRED_PYTHON`, and `MSMS_CUDA_DEVICES` are also supported. Paths in the YAML file are relative to that file. The public GLACIER checkpoint can be incompatible with newer ms-pred feature layouts: the preflight compares checkpoint atom feature width with the selected checkout before inference and reports the mismatch. In a local probe, the public checkpoint expected 88 base atom features while ms-pred commit `577b452` produced 89; choose a compatible checkout or checkpoint. The same checkpoint may produce better or different results than other public weights, and licensed NIST weights stay local.

FRIGID is installed separately when needed:

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
# To also download the public FRIGID checkpoint archive (about 2.8 GB):
MSMS_DOWNLOAD_FRIGID_WEIGHTS=1 bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

You may instead provide your own checkpoint files. The [FRIGID public weights](https://zenodo.org/records/19685145) do not include licensed NIST assets. Set the FRIGID interpreter, checkout, and checkpoint paths as described in `msms-denovo/SKILL.md`. No setup script modifies an existing `ms-pred` or FRIGID checkout.

## Agent portability

The canonical skills live in `.agents/skills` and are linked under `.claude/skills` for Claude Code. For installation into another project, copy the needed skill directories from `.agents/skills` into that agent's project or user skills directory so the `SKILL.md` files and their scripts stay together. Codex commonly uses `.agents/skills` in a project or `~/.codex/skills` for user skills; Claude Code uses `.claude/skills` in a project or `~/.claude/skills` for user skills. The scientific workflow is [msms-elucidation.md](../.agents/workflows/msms-elucidation.md). The CLI uses no LLM or MCP dependency; an agent can review `retrieval.json` and supply new candidate SMILES through the `msms-structure-review` skill. Optional MCP servers are separate adapters.

For raw files or mzML, export a feature-level `.ms` spectrum with the preprocessing and inspection skills first. For GNPS/MZmine MGF:

```bash
msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --raw-mzxml raw.mzXML --output-dir converted
msms-structure-elucidation batch --manifest converted/manifest.csv --output-dir results/batch --ms-pred-dir /path/to/ms-pred
```

The converter includes MS2 entries only. `SOURCE_SCAN` or `SCANS` can point to the MS1 apex, so the converter never uses them to infer MS2 collision energy. It reads MGF collision fields or the `collisionEnergy` of referenced `MERGED_SCANS`/MS2 scans in mzXML. Missing or inconsistent values produce `needs_energy` in the manifest; use `--energy` only after confirming the value. Bruker Q-TOF collision settings are usually absolute eV, set per precursor m/z; check the actual MS2 scan in raw mzXML and confirm the unit with the provider. Batch shares an atlas cache, resumes completed features, defaults to one worker and one model job, and checks available memory before scheduling. Set `--max-workers`, `--max-model-jobs`, and `--min-free-memory-gb` as needed. Install `.[batch]` for `psutil`. See [the scientific workflow](../.agents/workflows/msms-elucidation.md) for the full route. The supplied CSF example is `ms-pred/data/exp_specs/clinical/csf_unknown.ms`.

## Interactive fragment review

After retrieval or candidate review, open the separate `msms-visualize` skill:

```bash
"${MS_PRED_PYTHON:-python}" -m pip install -e '.[visualize]'
msms-structure-elucidation visualize --result results/sample/retrieval.json
# Several unknowns: one page with a searchable list of all of them
msms-structure-elucidation visualize --result results/*/retrieval.json
```

Open the printed localhost URL for an overview of every unknown and a review workspace: hover or click peaks in the mirror plot to highlight predicted fragments on the structure, compare candidates, and record decisions and fragment comments, which autosave. Add `--export review_report.html` to write one self-contained read-only report instead of serving. Notes are written to `review_notes.json` beside the result; `retrieval.json` remains the computed record. The viewer uses cached atlas predictions and makes no model or network calls. Existing results can recover fragment IDs from their cached atlas MGF; pass `--atlas-mgf` if that file was moved.
