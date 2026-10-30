# Technical guide: MS/MS structure elucidation

Portable tools and skills for assigning candidate small-molecule structures from experimental MS/MS. By default, `run` and `batch` rank precomputed ICEBERG 2.1 spectra from the public PubChem atlas. With `--mass-only`, they search PubChem by monoisotopic mass, skip formula prediction and atlas retrieval, and predict every matched candidate with the confirmed ICEBERG or GLACIER model.

Before prediction, confirm the intended model and exact weights using [msms-setup](../.agents/skills/msms-setup/SKILL.md). Either ICEBERG's generator/intensity checkpoint pair or a GLACIER checkpoint is sufficient. For ICEBERG, prefer suitable atlas spectra: the public PubChem atlas was generated with ICEBERG 2.1 trained on NIST'23. When local weights differ, or matching weights have not been verified, tell the user that combining local and atlas predictions may affect ranking comparability, and identify each prediction's source and checkpoint. GLACIER predicts every candidate because no open GLACIER atlas is available. A request for fresh predictions throughout uses the selected local weights for the complete candidate pool.

Use `run` or `batch` for formula-based atlas retrieval, and add `--mass-only` for direct PubChem mass search and prediction with either model. For explicit candidate lists, use `score_candidates` with the selected `model` and target `formula`. The `review` command extends a result by retaining existing candidates and reusing suitable atlas matches; its `--model` selects the model for additional predictions. Use direct scoring when the complete ranking must come from one selected model and checkpoint.

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

Omit `--formula` to search the top three MSBuddy hypotheses. Use `--formulas-file formulas.txt` for a user supplied list, one formula per line. MSBuddy mass tolerances follow the instrument (Q-TOF 10/20 ppm, Orbitrap 5/10 ppm precursor/fragment; `--ms1-ppm`, `--ms2-ppm`). If MSBuddy proposes no formula, formulas of PubChem structures matching the precursor mass are searched instead. A result without atlas coverage records `next_step: iceberg-pubchem` (predict the formula's PubChem structures with `review --proposals pubchem`), and one with no formula anywhere records `next_step: review-frigid`. `--atlas-mgf` is for a single formula offline. The command writes `retrieval.json` and a basic offline `report.html`; use the richer viewer below for fragment inspection and notes. Formula outcomes are recorded separately in `formula_results`. `ranked`, `needs_formula`, `no_atlas_coverage`, and `no_candidates` are valid outcomes with exit code 0; operational errors return nonzero. Entropy similarity is a ranking statistic, not identification confidence. Candidates are ranked by the mean entropy similarity over paired collision energies, with the precursor peak excluded; explained intensity only breaks exact ties and is reported as evidence. Candidates with the same formula, identical matched peaks and entropy within 0.03 of a higher-ranked one are flagged as potentially indistinguishable.

### Mass-only PubChem option

Two mass-search interfaces are available. The [atlas WebUI's Mass ± ppm mode](https://github.com/rogerwwww/iceberg-atlas-webui#spectrum-retrieval) retrieves stored spectra by searching the atlas's formula/mass index. It requires no supplied formula or live PubChem query. The local atlas CLI instead collects candidate formulas, downloads their formula MGF files, and ranks the stored spectra. For ICEBERG, prefer suitable atlas spectra through either retrieval interface.

The local `--mass-only` option below is a direct PubChem search followed by prediction of every eligible candidate. Use it for GLACIER or a request for fresh predictions throughout.

Instead of predicting a formula, search PubChem directly by monoisotopic mass within a chosen ppm tolerance. Skip MSBuddy and other formula-prediction methods entirely; take formulas from the matching PubChem records, then predict and rank the structures for every matched formula:

1. Confirm the precursor m/z, adduct, charge and mass tolerance. Convert the precursor to neutral monoisotopic mass using that adduct and charge, and search PubChem within ±ppm of that neutral mass. Record the mass window and any element or structure filters.
2. Collect every matched formula and all eligible PubChem structures for each formula. Deduplicate structures and retain their formula, PubChem identifiers and mass error. Do not select only the most common formulas or the top three hypotheses. Check retrieval limits and pagination; report incomplete searches or failed lookups explicitly.
3. Confirm the prediction model and exact weights as described in [msms-setup](../.agents/skills/msms-setup/SKILL.md), and confirm NCE or eV before scoring. Predict every candidate with that model at the experimental adduct and all supplied collision energies. This route uses fresh predictions throughout, including when ICEBERG is selected. With MCP connected, call `server_info` first, then use `search_pubchem_mass` with the confirmed `model`, `ppm` and `collision_unit`; it handles all matched formulas and returns a resumable job.
4. Combine candidates from all matched formulas into one ranking using the same mean entropy similarity over all supplied energies, with the precursor excluded. Keep formula and mass error visible, and use the same weights and scoring settings for every formula. Rank by score across the combined pool rather than by each formula's local rank. Report candidate counts, failed predictions and any unprocessed candidates; do not describe a partial search as exhaustive.

Use `--mass-only` to select this route. The following examples assume that the model and exact weights are confirmed and configured in `configs/local.yaml`, and that the provider confirmed NCE:

```bash
msms-structure-elucidation run --input sample.ms --collision-unit NCE \
  --mass-only --ms1-ppm 5 --model glacier --output-dir results/sample

msms-structure-elucidation batch --manifest converted/manifest.csv \
  --mass-only --ms1-ppm 5 --model glacier --output-dir results/batch

python .agents/skills/msms-atlas-retrieval/scripts/run.py --spectrum sample.ms \
  --collision-unit NCE --mass-only --ms1-ppm 5 --model glacier \
  --output results/sample/retrieval.json
```

Use `--model iceberg` for ICEBERG. Supply `--checkpoint` for GLACIER, or `--gen-checkpoint` and `--inten-checkpoint` for ICEBERG, when the confirmed weights are not already configured. Set `--ms-pred-dir` and `--ms-pred-python` if needed. `--model-shard-size`, batch size and worker settings control bounded, resumable prediction jobs. `--top-k` limits the returned shortlist, not the number of structures searched or predicted.

The [PubChem mass-range API](https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest-tutorial) returns CIDs for the selected neutral-mass interval. The mass-only path reads every returned CID in property batches, retains all eligible formulas and structures without a formula or candidate-count cap, and deduplicates the models' stereo-free structures by canonical SMILES/InChIKey while retaining all original PubChem SMILES, CIDs and stereoisomer/tautomer aliases. Equivalent model inputs are predicted once and retain their aliases in `pubchem_smiles`. Supported singly charged adducts are converted to neutral mass; unsupported charge/adduct combinations fail explicitly. The default element filter is `CHNOPS`; change it with `--formula-elements CHNOPSClBr` when appropriate. Neutral, single-component, non-isotope-labelled, closed-shell structures are retained.

`--mass-only` conflicts with `--formula`, `--formulas-file`, `--atlas-mgf` and `--no-pubchem`. An input `.ms >formula` is ignored with a warning. In atlas mode, `--ms1-ppm` sets the tolerance for MSBuddy and the PubChem formula fallback.

The route writes `retrieval.json`, `report.html`, a complete `pubchem_candidates.json` manifest, per-formula canonical candidate manifests, and full prediction/scoring shards under `model_shards/`. Results include CID provenance, mass error, per-formula counts/status and `prediction_complete`; use the standard `visualize` command or batch review page. Resuming reuses matching prediction shards; changing the input, candidate list, instrument, selected model or checkpoint identity invalidates them. Newly computed shards bypass ms-pred's internal prediction cache. No matches returns `no_candidates`; incomplete PubChem responses and model failures write error receipts and exit nonzero. Remaining formulas are marked `not_processed` after a failure.

For MCP, `search_pubchem_mass(collision_unit="NCE", model="glacier", ppm=5, spectrum_ms=...)` runs this same route on the configured server. Poll returned job IDs with `get_job`. The result includes the standard retrieval/report paths for inspection and visualization.

The public formula-download endpoint is `https://iceberg-ms.mit.edu/download_mgf?formula=...&adduct=...`. It supplies ICEBERG 2.1 predictions for PubChem structures, generated with NIST'23-trained weights. Licensed NIST candidate structures are excluded from the public download. No atlas files or model weights are bundled here.

## Host tuning

`msms-structure-elucidation setup` reads the CPU threads, RAM and GPUs, chooses ms-pred inference settings from measured anchors, and saves them to `configs/local.yaml`, which Git ignores. If ms-pred and ICEBERG checkpoints are available, it also times ICEBERG to choose batch_size. Every command applies that file on top of `configs/default.yaml` unless `--config` or `MSMS_CONFIG` is given. Use `--remote HOST --remote-repo PATH` to tune a GPU host over ssh, adding `--remote-prefix` for environment activation or a scheduler such as `srun`. For GLACIER, use `--no-benchmark` and tune its native `pred_smis` inference settings separately on each machine through the configured prediction path. Start from GLACIER's defaults, measure batch size and CPU worker throughput and memory use, and keep one GPU worker with the current adapter. ICEBERG tuning results do not establish GLACIER performance. Save the selected model, checkpoint and measured settings in `configs/local.yaml`; see the [msms-setup skill](../.agents/skills/msms-setup/SKILL.md).

## MCP server

`msms-structure-elucidation mcp` (or `python -m msms_structure_elucidation.mcp`) serves the model tools over MCP stdio:

| Tool | What it does |
|---|---|
| `server_info` | Reports the backend, host, GPUs, and which models are configured |
| `predict_spectra` | Forward prediction (SMILES → spectra and fragments) with ICEBERG or GLACIER |
| `score_candidates` | Ranks candidate SMILES against a spectrum |
| `retrieve_atlas` | Public atlas retrieval |
| `search_pubchem_mass` | Search by ppm mass, skip formula prediction and the atlas, predict every candidate, and rank across formulas |
| `generate_structures_frigid` | FRIGID de novo generation |
| `predict_fingerprint_mist` | MIST spectrum → fingerprint |
| `get_job`, `cancel_job`, `list_jobs` | Follow or stop submitted jobs |
| `pubchem_isomers`, `pubchem_compound` | PubChem lookups |

**Inputs and outputs.** Spectra go in as `.ms` text, or as a path on the server host. Every model tool requires `collision_unit` (`NCE` or `eV`).

**Where the server runs.** It runs on the host that holds the models and needs only `mcp` and this package, for example a `.cache/mcp-venv` made with `pip install -e .[mcp]`. It calls the ms-pred and FRIGID interpreters named in `configs/local.yaml` (`models.simulator.python`, `models.denovo.frigid_python`) and never imports them itself.

**How jobs run.**
- Each model call becomes a job directory under `mcp.job_root`, holding the request, status, result and logs.
- A tool waits up to `mcp.wait_seconds` and then returns either the result or a job id for `get_job`.
- Jobs are detached, so they survive a dropped client connection.
- An identical request returns the earlier job.

**Backends and SSH.** `mcp.backend` selects how the MCP server runs jobs. SSH is the connection to that server, configured separately through the client's `execution.mode: remote`, SSH destination and generated `.mcp.json` entry. `local` means local to the MCP server host, which may be a remote GPU machine reached over SSH. There is no `mcp.backend: ssh` value.

| Connection to the MCP server | Server's `mcp.backend` | Where jobs run |
|---|---|---|
| Same machine | `local` | On that machine |
| SSH to a GPU host | `local` | On the remote GPU host |
| SSH to a Slurm login node | `slurm` | On allocated compute nodes |

- `local`: runs each job on an idle GPU on the server host that has at least `gpu_min_free_fraction` of its memory free, and waits while every GPU is busy.
- `slurm`: submits each job with `sbatch`, using only the options set under `mcp.slurm`. Those options are `partition`, `account`, `qos`, `gpus`, `cpus_per_task`, `mem`, `time`, `requeue`, `extra_args`, `setup` shell lines, and `cpu` resources for CPU-only tasks.

**Setup.** `setup` writes `configs/local.yaml` and `mcp-config` writes the client entry; see the [msms-setup skill](../.agents/skills/msms-setup/SKILL.md).

```bash
# On a Slurm login node: list partitions, then save the chosen settings and benchmark in a job
msms-structure-elucidation setup --scheduler slurm
msms-structure-elucidation setup --scheduler slurm --slurm-partition <partition> --slurm-gpus <gpu-request> \
  --slurm-time 04:00:00 --slurm-setup '<environment activation>' --ms-pred-dir <ms-pred> ...
# On the client: print or merge the .mcp.json entry for the configured host
msms-structure-elucidation mcp-config --write
```

For a remote host, the client entry runs `ssh -o BatchMode=yes <ssh-destination> 'cd <remote-repo> && <server-python> -m msms_structure_elucidation.mcp'`. ssh must log in without a prompt. If the host asks for a password or a second factor, open a persistent connection first (`ControlMaster`/`ControlPersist` in `~/.ssh/config`) and keep it open while the client runs.

## Optional models

`ms-pred` supplies GLACIER, ICEBERG, and spectral utilities. Clone the [official ms-pred repository](https://github.com/coleygroup/ms-pred), read its README, and follow its environment setup for your host. Set `--ms-pred-dir` to that checkout for local inference; the worker runs there because GLACIER uses a relative script path. Use the open-source MassSpecGym checkpoint links in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme), or provide local licensed NIST checkpoints if your license permits. GLACIER needs one checkpoint; ICEBERG needs generation and intensity checkpoints. Model calls receive rounded integer eV with `nce=False`. The `.ms` `>instrumentation` value is used for model calls, with `--instrument` taking priority. Use `--cuda-devices 0` or `MSMS_CUDA_DEVICES=0` to select a GPU. Tune batch size and ms-pred worker counts (`--model-batch-size`, `--model-cpu-workers`, `--model-gpu-workers`) for the selected model on each host. The anchors in `.agents/skills/msms-predict/SKILL.md` are ICEBERG starting points; tune GLACIER independently as described above.

Put local paths and defaults in the uncommitted `configs/local.yaml`, which overrides the shared [configs/default.yaml](../configs/default.yaml), or pass `--config path/to/config.yaml`. CLI options override config; `MS_PRED_DIR`, `MS_PRED_PYTHON`, and `MSMS_CUDA_DEVICES` are also supported. Paths in the YAML file are relative to that file. The GLACIER preflight checks that the checkpoint's atom feature width matches the ms-pred checkout. If it reports a mismatch, select a compatible checkout and checkpoint. Licensed NIST weights stay local.

FRIGID is installed separately when needed:

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
# To also download the public FRIGID checkpoint archive (about 2.8 GB):
MSMS_DOWNLOAD_FRIGID_WEIGHTS=1 bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

You may instead provide your own checkpoint files. The [FRIGID public weights](https://zenodo.org/records/19685145) do not include licensed NIST assets. Set the FRIGID interpreter, checkout, and checkpoint paths as described in `msms-denovo/SKILL.md`. No setup script modifies an existing `ms-pred` or FRIGID checkout.

## Agent portability

The canonical skills live in `.agents/skills` and are linked under `.claude/skills` for Claude Code. For installation into another project, copy the needed skill directories from `.agents/skills` into that agent's project or user skills directory so the `SKILL.md` files and their scripts stay together. Codex commonly uses `.agents/skills` in a project or `~/.codex/skills` for user skills; Claude Code uses `.claude/skills` in a project or `~/.claude/skills` for user skills. The scientific workflow is [msms-elucidation.md](../.agents/workflows/msms-elucidation.md). The CLI uses no LLM or MCP dependency; an agent can review `retrieval.json` and supply new candidate SMILES through the `msms-structure-review` skill. The optional [MCP server](#mcp-server) exposes the same models as tools.

For raw files or mzML, export a feature-level `.ms` spectrum with the preprocessing and inspection skills first. For GNPS/MZmine MGF:

```bash
msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --raw-mzxml raw_mzxml_folder/ --output-dir converted
# ICEBERG atlas route by default; add --mass-only --ms1-ppm 5 --model glacier for direct mass search.
msms-structure-elucidation batch --manifest converted/manifest.csv --output-dir results/batch --ms-pred-dir /path/to/ms-pred
```

The converter includes MS2 entries only. `SOURCE_SCAN` or `SCANS` can point to the MS1 apex, so the converter never uses them to infer MS2 collision energy. It reads MGF collision fields or the `collisionEnergy` of referenced `MERGED_SCANS`/MS2 scans in mzXML. `--raw-mzxml` takes several files or folders; each entry reads the run named by its `SOURCE_FILE` (or the only file given). When an entry names no MS2 scan, as in GNPS feature-finding MGF, the converter uses that run's MS2 scans whose precursor is within 0.01 Da (or 20 ppm) of `PEPMASS` and whose retention time is within `--ms2-rt-window` seconds (default 30) of `RTINSECONDS`, records them in `ms2_scans` with `energy_source` `mzXML:MS2_precursor_match`, and accepts the energy only when those scans agree.

Feature finders export every feature as [M+H]+ by default, so the converter also derives each feature's ion from the features co-eluting with it (within `--ion-rt-window` seconds, default 3). It uses every feature in the MGF, including those outside `--feature-ids`. Co-eluting features whose neutral masses agree under different adducts ([M+H]+, [M+Na]+, [M+NH4]+, [M+K]+, [M+2Na-H]+, [M+H+HCOONa]+, [M+H-H2O]+; negative-mode counterparts) share one molecule, and each is written with its own `>ionization`. A feature whose neutral mass is the sum of two co-eluting molecules, at least one confirmed by its own adduct partner, is a `multimer` ([2M+H]+ or [M1+M2+H]+). A lighter feature without adduct partners whose m/z is a peak in the MS2 of a heavier co-eluting molecule, at a plausible neutral loss (at least 14 Da, organic mass defect), is an `in_source_fragment`. The partner check keeps a precursor's two complementary fragments from passing as dimer monomers. Multimers, in-source fragments and adducts the models do not support get that manifest status with the related features instead of being converted. Pass `--elucidate-all-ions` to convert them anyway, or `--no-ion-identity` to keep the MGF's own or the default adduct. A non-default adduct stated in the MGF (`ADDUCT`/`ION`) takes precedence. MSBuddy receives the adduct. Atlas coverage depends on the requested adduct. If a downloaded MGF contains a different adduct, `run` uses its structure list and predicts every structure with local ICEBERG for the experimental adduct (`reason: atlas_lacks_adduct`). Missing or inconsistent values produce `needs_energy` in the manifest; use `--energy` only after confirming the value. Bruker Q-TOF collision settings are usually absolute eV, set per precursor m/z; check the actual MS2 scan in raw mzXML and confirm the unit with the provider. Batch shares an atlas cache and resumes completed features. It processes several features at once in worker processes when CPU and RAM permit, so atlas lookups run in parallel and the GPU stays busy while other features run formula, atlas and scoring steps. `max_workers: auto`, the default, uses up to 8 workers, sized from CPU threads and RAM at about 1.5 GB per worker, and `setup` saves the value. Model jobs are limited to one at a time by default, and a new feature starts only while free RAM stays above `--min-free-memory-gb`. Override with `--max-workers`, `--max-model-jobs` and `--min-free-memory-gb`. Install `.[batch]` for `psutil`. As its final step, batch writes `review_report.html`, one self-contained review page (the `visualize --export` page) for every finished feature, and prints the command that serves the interactive viewer; `--serve` starts that viewer right away and `--no-review-page` skips the page. See [the scientific workflow](../.agents/workflows/msms-elucidation.md) for the full route. The clinical demo uses three unknowns from `ms-pred/data/exp_specs/clinical/`: `csf_unknown.ms`, `plasma_unknown_198.ms`, and `plasma_unknown_583.ms`. See the [demo guide](../demo/README.md) for the spectra and interactive viewer.

## Interactive fragment review

After retrieval or candidate review, open the separate `msms-visualize` skill:

```bash
"${MS_PRED_PYTHON:-python}" -m pip install -e '.[visualize]'
msms-structure-elucidation visualize --result results/sample/retrieval.json
# Several unknowns: one page with a searchable list of all of them
msms-structure-elucidation visualize --result results/*/retrieval.json
```

Open the printed localhost URL for an overview of every unknown and a review workspace: hover or click peaks in the mirror plot to highlight predicted fragments on the structure, compare candidates, and record decisions and fragment comments, which autosave. Add `--export review_report.html` to write one self-contained read-only report instead of serving. Notes are written to `review_notes.json` beside the result; `retrieval.json` remains the computed record. The viewer uses cached atlas predictions and makes no model or network calls. Existing results can recover fragment IDs from their cached atlas MGF; pass `--atlas-mgf` if that file was moved.
