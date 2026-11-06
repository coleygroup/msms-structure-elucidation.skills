# Technical guide: MS/MS structure elucidation

Portable tools and skills for assigning candidate small-molecule structures from experimental MS/MS. By default, `run` and `batch` rank precomputed ICEBERG 2.1 spectra from the public PubChem atlas. With `--mass-only`, they search PubChem by monoisotopic mass, skip formula prediction and atlas retrieval, and predict every matched candidate with the confirmed ICEBERG or GLACIER model.

Before prediction, confirm the intended model and exact weights using [msms-setup](../.agents/skills/msms-setup/SKILL.md). Either ICEBERG's generator/intensity checkpoint pair or a GLACIER checkpoint is sufficient. For ICEBERG, prefer suitable atlas spectra: the public PubChem atlas was generated with ICEBERG 2.1 trained on NIST'23. When local weights differ, or matching weights have not been verified, tell the user that combining local and atlas predictions may affect ranking comparability, and identify each prediction's source and checkpoint. GLACIER predicts every candidate because no open GLACIER atlas is available. A request for fresh predictions throughout uses the selected local weights for the complete candidate pool.

Follow [formula search](formula-search.md): without a supplied formula, recommend BUDDY’s top three and ask once whether to use them or PubChem mass filtering, reusing a matching saved choice. Use `run` or `batch` for formula-guided searches: ICEBERG prefers the atlas; GLACIER retrieves the PubChem candidates for each selected formula and predicts them. Add `--mass-only` for direct PubChem mass filtering with either model. For explicit candidate lists, use `score_candidates` with the selected `model` and target `formula`. The `review` command extends a result by retaining existing candidates and reusing suitable atlas matches; its `--model` selects the model for additional predictions. Use direct scoring when the complete ranking must come from one selected model and checkpoint.

## Manual CLI setup

For agent-driven analyses, install the [workflow completion hooks](workflow-hooks.md) in the client project as well as the CLI. Codex and Claude Code share the same checker and scientific workflow. Register the requested analysis before launching jobs; hooks inspect its saved evidence and request continued work when normal turn completion would leave required steps unfinished.

Use an existing verified ms-pred environment when available. If setup is needed, clone [ms-pred](https://github.com/coleygroup/ms-pred), read its official README, and follow the environment instructions for your host. The `setup_envs.sh` script here is only a CPU venv helper.

```bash
python -m pip install -e .
# Point to the verified Python with ms-pred, MSBuddy, and RDKit.
export MS_PRED_PYTHON=/path/to/ms-pred-python
# Use NCE here only after the spectrum provider has confirmed it.
msms-structure-elucidation run --input sample.ms --collision-unit NCE --output-dir results/sample --formula C12H21NO5
```

For raw/mzML input, first inspect the relevant acquisition records using [collision-energy handling](collision-energy.md), then pass the verified `--collision-unit NCE` or `--collision-unit eV`. Ask the provider only if metadata is missing, ambiguous or conflicting. Known stepped acquisitions predict all components and merge intensities before normalization for one composite-spectrum comparison. File headers can be wrong; the chosen unit and any conflict are recorded. NCE is converted with precursor m/z. Experimental and atlas energies are rounded to integer eV and paired only when those integers are equal. Every supplied energy must contribute to a candidate score. If an atlas formula has any missing energy, the workflow simulates **every atlas structure for that formula at every supplied energy** with local ICEBERG in resumable shards. It never substitutes an off-energy atlas spectrum. Supply an ms-pred checkout and ICEBERG weights before that fallback is needed. Without those assets, `retrieval.json` records `blocked_model_assets`, the underlying error is printed, and the command exits nonzero.

Omit `--formula` to search the top three MSBuddy hypotheses. Use `--formulas-file formulas.txt` for a user supplied list, one formula per line. MSBuddy mass tolerances follow the instrument (Q-TOF 10/20 ppm, Orbitrap 5/10 ppm precursor/fragment; `--ms1-ppm`, `--ms2-ppm`). If BUDDY/MSBuddy returns no formula, ask whether to switch to mass filtering. The default does not silently change routes. The explicit `--pubchem-fallback` option permits the limited fallback to up to three PubChem mass-matched formulas; it is distinct from the full `--mass-only` search. A result without atlas coverage records `next_step: iceberg-pubchem` (predict the formula's PubChem structures with `review --proposals pubchem`), and one with no formula anywhere records `next_step: review-frigid`. `--atlas-mgf` is for a single formula offline. The command writes `retrieval.json` and a basic offline `report.html`; use the richer viewer below for fragment inspection and notes. Formula outcomes are recorded separately in `formula_results`. `ranked`, `needs_formula`, `no_atlas_coverage`, and `no_candidates` are valid outcomes with exit code 0; operational errors return nonzero. Entropy similarity is a ranking statistic, not identification confidence. Candidates are ranked by the mean entropy similarity over paired collision energies, with the precursor peak excluded; explained intensity only breaks exact ties and is reported as evidence. Candidates with the same formula, identical matched peaks and entropy within 0.03 of a higher-ranked one are flagged as potentially indistinguishable.

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

The [PubChem mass-range API](https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest-tutorial) returns CIDs for the selected neutral-mass interval. The mass-only path reads every returned CID in property batches, retains all eligible formulas and structures without a formula or candidate-count cap, and deduplicates the models' stereo-free structures by canonical SMILES/InChIKey while retaining all original PubChem SMILES, CIDs and stereoisomer/tautomer aliases. Equivalent model inputs are predicted once and retain their aliases in `pubchem_smiles`. Supported singly charged adducts are converted to neutral mass; unsupported charge/adduct combinations fail explicitly. The default element filter is `CHNOPSClBrFISiBSe` (C, H, N, O, P, S, halogens, Si, B and Se); change it with `--formula-elements` when appropriate. The indexed mass search is performed once and CID properties are fetched in batches of 500, without enumerating theoretical elemental formulas. Check compatibility with the selected model for custom elements. Neutral, single-component, non-isotope-labelled, closed-shell structures are retained. Candidate filtering excludes any structure containing a negatively charged carbon atom, including net-neutral zwitterions such as `C[N+](C)(C)[C-]`. This rule also applies to atlas candidates and proposed structures; the entire candidate is excluded before scoring.

`--mass-only` conflicts with `--formula`, `--formulas-file`, `--atlas-mgf` and `--no-pubchem`. An input `.ms >formula` is ignored with a warning. In atlas mode, `--ms1-ppm` sets the tolerance for MSBuddy and the PubChem formula fallback.

The route writes `retrieval.json`, `report.html`, a complete `pubchem_candidates.json` manifest, per-formula canonical candidate manifests, and full prediction/scoring shards under `model_shards/`. Results include CID provenance, mass error, per-formula counts/status and `prediction_complete`; use the standard `visualize` command or batch review page. Resuming reuses matching prediction shards; changing the input, candidate list, instrument, selected model or checkpoint identity invalidates them. Missing raw predictions bypass ms-pred’s internal request cache and enter the shared prediction cache described below; compatible candidate/energy predictions are reused across unknowns. No matches returns `no_candidates`; incomplete PubChem responses and model failures write error receipts and exit nonzero. Remaining formulas are marked `not_processed` after a failure.

For MCP, `search_pubchem_mass(collision_unit="NCE", model="glacier", ppm=5, spectrum_ms=...)` runs this same route on the configured server. Poll returned job IDs with `get_job`. The result includes the standard retrieval/report paths for inspection and visualization.

The public formula-download endpoint is `https://iceberg-ms.mit.edu/download_mgf?formula=...&adduct=...`. It supplies ICEBERG 2.1 predictions for PubChem structures, generated with NIST'23-trained weights. Licensed NIST candidate structures are excluded from the public download. No atlas files or model weights are bundled here.

## Shared predictions

`models.simulator.reuse_predictions: true` enables a shared raw-spectrum cache on the model host. `models.simulator.prediction_cache` selects its directory; an empty path uses the platform user cache. The database stores raw peaks and aligned fragment IDs for each canonical stereo-free molecule and actual integer eV setting. It does not store experimental scores. Each unknown is scored and reviewed independently.

Reuse requires the same model, checkpoint content hashes, model and adapter source, molecular structure, adduct, normalized instrument and sparse-peak count. Same-formula unknowns can reuse the same candidate predictions when these settings match. Different energy sets reuse their common components and predict only missing structure/energy pairs. Native NCE conversion is resolved for each precursor before lookup. Stepped acquisition merges the cached components separately for its observed composite. Candidate locks prevent concurrent feature jobs from computing the same missing spectrum twice; commits contain only validated complete groups. Cache receipts record scientific identity and reused/new spectrum counts.

Batch size, CPU workers, HDF5 writer/chunk counts and feature-cache budget can be tuned without changing the scientific cache identity. Weight contents or source-code changes invalidate reuse. Set `reuse_predictions: false` for an explicitly requested recomputation; changing `prediction_cache` selects a different cache. Keep caches and weights outside Git and do not publish licensed/private assets. Formula-guided GLACIER retrieval reads the complete PubChem CID list and property batches for each selected formula, without the capped structure shortlist used by the legacy `review --proposals pubchem` extension.

## Host tuning

`msms-structure-elucidation setup` reads the CPU threads, RAM and GPUs, chooses ms-pred inference settings from measured anchors, and saves them to `configs/local.yaml`, which Git ignores. With the selected model's ms-pred checkout and checkpoints available, it benchmarks that model to choose its batch size; ICEBERG also compares GPU worker counts. Every command applies that file on top of `configs/default.yaml` unless `--config` or `MSMS_CONFIG` is given. Use `--remote HOST --remote-repo PATH` to tune a GPU host over ssh, adding `--remote-prefix` for environment activation or a scheduler such as `srun`. Tune GLACIER independently on each machine through its native `pred_smis` path. Use `--no-benchmark` only to save a heuristic starting profile when a representative benchmark cannot run. Use the separate `models.simulator.glacier` profile: batch size 128, 4 CPU workers per process, 1 GPU worker and 1 prediction process per GPU, 2 HDF5 writers, 4 HDF5 chunks, feature cache 256 and sparse peaks 100. The launcher forwards these settings to native `predict_smis_joint.py`. The checkout must support `--num-writers`, `--num-h5-chunks` and `--feat-cache-size`; incompatible runtimes fail explicitly instead of ignoring the profile. Native sharding starts one prediction process per visible GPU, and the reader loads all HDF5 chunks/shards. Measure throughput and memory before changing them; generic ICEBERG tuning settings are not GLACIER defaults. ICEBERG tuning results do not establish GLACIER performance. Save the selected model, checkpoint and measured settings in `configs/local.yaml`; see the [msms-setup skill](../.agents/skills/msms-setup/SKILL.md).

## MCP server

`msms-structure-elucidation mcp` (or `python -m msms_structure_elucidation.mcp`) serves the model tools over MCP stdio:

| Tool | What it does |
|---|---|
| `server_info` | Reports the backend, host, GPUs, and which models are configured |
| `predict_spectra` | Forward prediction (SMILES → spectra and fragments) with ICEBERG or GLACIER |
| `score_candidates` | Ranks candidate SMILES against a spectrum |
| `retrieve_atlas` | Public atlas retrieval |
| `infer_formulas` | BUDDY/MSBuddy top three formulas, without candidate search or spectral prediction |
| `search_pubchem_formulas` | Selected formula hypotheses: GLACIER predicts their PubChem candidate pools; ICEBERG prefers the atlas |
| `search_pubchem_mass` | Search by ppm mass, skip formula prediction and the atlas, predict every candidate, and rank across formulas |
| `generate_structures_frigid` | FRIGID de novo generation |
| `predict_fingerprint_mist` | MIST spectrum → fingerprint |
| `get_job`, `cancel_job`, `list_jobs` | Follow or stop submitted jobs |
| `pubchem_isomers`, `pubchem_compound` | PubChem lookups |

**Inputs and outputs.** Spectra go in as `.ms` text, or as a path on the server host. Every model tool requires `collision_unit` (`NCE` or `eV`).

**Where the server runs.** It needs only `mcp` and this package, for example a `.cache/mcp-venv` made with `pip install -e .[mcp]`. It can run on the model host or dispatch complete requests to configured workers. A pool coordinator needs setup-verified model identities; workers hold their scientific interpreters, models, PubChem index and caches. Preparation, prediction and output/scoring stay on the selected SSH machine or cluster shared storage. Final results and viewer renderings return to the coordinator. Locally configured ms-pred and FRIGID interpreters remain separate from the server environment.

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
| Coordinator with several endpoints | `pool` | Across configured local, SSH, Slurm or Kubernetes endpoints |
| Coordinator with a shared Kubernetes workspace | `kubernetes` | In bounded GPU Jobs |

- `local`: runs preparation and scoring in independent CPU slots. Missing predictions wait for an idle GPU on the server host with at least `gpu_min_free_fraction` of its memory free; the GPU is released before experimental scoring. Complete prediction-cache hits need no GPU allocation.
- `slurm`: submits CPU orchestrators and missing-prediction GPU stages separately with `sbatch`, using only the options set under `mcp.slurm`. Those options are `partition`, `account`, `qos`, `gpus`, `cpus_per_task`, `mem`, `time`, `requeue`, `extra_args`, `setup` shell lines, and `cpu` resources for CPU-only tasks.

Use [prediction server pools](prediction-servers.md) for multiple hosts, Kubernetes configuration, availability observations, grouping, verified result collection and cleanup. SSH remains the transport to individual GPU or Slurm endpoints; `pool` selects among them.

See [MCP execution stages and resource settings](mcp-execution.md) for CPU limits, bounded preparation, prediction group size, shared-node paths, stage monitoring and utilization measurement.

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

`ms-pred` supplies GLACIER, ICEBERG, and spectral utilities. Clone the [official ms-pred repository](https://github.com/coleygroup/ms-pred), read its README, and follow its environment setup for your host. Set `--ms-pred-dir` to that checkout for local inference; the worker runs there because GLACIER uses a relative script path. Use the open-source MassSpecGym checkpoint links in the [ms-pred README](https://github.com/coleygroup/ms-pred#readme), or provide local licensed NIST checkpoints if your license permits. GLACIER needs one checkpoint; ICEBERG needs generation and intensity checkpoints. Model calls receive rounded integer eV with `nce=False`. The `.ms` `>instrumentation` value is used for model calls, with `--instrument` taking priority. Use `--cuda-devices 0` or `MSMS_CUDA_DEVICES=0` to select a GPU. Tune batch size and ms-pred worker counts (`--model-batch-size`, `--model-cpu-workers`, `--model-gpu-workers`) for the selected model on each host. For ICEBERG, start with two parallel workers per GPU and compare two to four within memory limits. For GLACIER, keep one prediction process per GPU and primarily tune batch size. Use enough candidates to measure sustained inference separately from startup and scoring; see [MCP execution and measurement](mcp-execution.md). Save host-specific measurements in `configs/local.yaml`.

Put local paths and defaults in the uncommitted `configs/local.yaml`, which overrides the shared [configs/default.yaml](../configs/default.yaml), or pass `--config path/to/config.yaml`. CLI options override config; `MS_PRED_DIR`, `MS_PRED_PYTHON`, and `MSMS_CUDA_DEVICES` are also supported. Paths in the YAML file are relative to that file. The GLACIER preflight checks that the checkpoint's atom feature width matches the ms-pred checkout. If it reports a mismatch, select a compatible checkout and checkpoint. Licensed NIST weights stay local.

FRIGID is installed separately when needed:

```bash
bash .agents/skills/msms-denovo/scripts/setup_env.sh
# To also download the public FRIGID checkpoint archive (about 2.8 GB):
MSMS_DOWNLOAD_FRIGID_WEIGHTS=1 bash .agents/skills/msms-denovo/scripts/setup_env.sh
```

You may instead provide your own checkpoint files. The [FRIGID public weights](https://zenodo.org/records/19685145) do not include licensed NIST assets. Set the FRIGID interpreter, checkout, and checkpoint paths as described in `msms-denovo/SKILL.md`. No setup script modifies an existing `ms-pred` or FRIGID checkout.

## Agent portability

The canonical skills live in `.agents/skills` and are linked under `.claude/skills` for Claude Code. The end-to-end entrypoint is [msms-structure-elucidation-skills](../.agents/skills/msms-structure-elucidation-skills/SKILL.md); it coordinates the specialized skills using [msms-elucidation.md](../.agents/workflows/msms-elucidation.md). Keep the full checkout accessible because the skills reference its workflow, documentation and CLI. For installation into another project, expose the collection's skill directories in that agent's project or user skills directory using links or copies; retain the checkout location when copying so repository references can be resolved there. Keep each `SKILL.md` with its scripts and references. Codex commonly uses `.agents/skills` in a project or `~/.codex/skills` for user skills; Claude Code uses `.claude/skills` in a project or `~/.claude/skills` for user skills. The CLI uses no LLM or MCP dependency; the agent assesses `retrieval.json` through `msms-evidence-review` and, if selected, supplies new candidate SMILES through `msms-structure-proposal`. The optional [MCP server](#mcp-server) exposes the same models as tools.

### Saved pipeline answers

Read [pipeline preferences](pipeline-preferences.md) before asking users for previously supplied choices. `msms-structure-elucidation preferences` reads/saves them in the uncommitted local config. Feature finding recommends GNPS2 Everything Bagel; when no feature list or saved selection is available, ask once between GNPS2 Everything Bagel, msconvert + MZmine, and the user's workflow, then save the answer. Dataset/spectrum scientific answers are scoped to their inputs; they are not global defaults.

### LC-MS features and MGF conversion

For LC-MS feature finding, use [msms-feature-detect](../.agents/skills/msms-feature-detect/SKILL.md): it offers the [GNPS2 Everything Bagel workflow](https://gnps2.org/workflowinput?workflowname=everything_bagel_workflow), its [browser interface](https://featurefinding.gnps2.org/), and local MZmine. GNPS2 runs on uploaded/selected files; browser feature finding uses local files. Use compatible centroid LC-MS data, separate polarities/acquisition methods, and inspect feature quality before scoring. Keep the feature table, MGF, task URL (when applicable) and parameters together.

For GNPS2 Everything Bagel, export `aligned_features.csv` and `aligned_features_filled.mgf` from the same GNPS2 task, or download the browser's feature table and feature MGF. Verify that MGF `SCANS` matches table `row ID`, then convert with:

```bash
msms-structure-elucidation convert-mgf --input aligned_features_filled.mgf --feature-id-field SCANS --collision-unit eV --output-dir converted
```

Confirm the collision unit first; `eV` here is an example. `--feature-id-field SCANS` preserves aligned row IDs in the manifest, `.ms` compound names, feature selection and ion relationships. These IDs do not identify raw MS2 scans. For known stepped acquisition, preserve the explicit component list with `--stepped-energies` and follow [stepped scoring](collision-energy.md). Other unresolved merged-energy spectra require original MS2 records or an energy-separated export. Missing energy remains `needs_energy`; the feature table and GNPS2 annotations do not establish collision energy. Features without MS/MS stay in the table but are not model-scored.

For raw files or mzML, export feature spectra with the preprocessing, feature-detection or inspection skills first. For other GNPS/MZmine MGF:

```bash
msms-structure-elucidation convert-mgf --input features.mgf --collision-unit eV --raw-mzxml raw_mzxml_folder/ --output-dir converted
# ICEBERG atlas route by default; add --mass-only --ms1-ppm 5 --model glacier for direct mass search.
msms-structure-elucidation batch --manifest converted/manifest.csv --output-dir results/batch --ms-pred-dir /path/to/ms-pred
```

The converter includes MS2 entries only. `SOURCE_SCAN` or `SCANS` can point to the MS1 apex, so the converter never uses them to infer MS2 collision energy. It reads MGF collision fields or the `collisionEnergy` of referenced `MERGED_SCANS`/MS2 scans in mzXML. `--raw-mzxml` takes several files or folders; each entry reads the run named by its `SOURCE_FILE` (or the only file given). When an entry names no MS2 scan, as in GNPS feature-finding MGF, the converter uses that run's MS2 scans whose precursor is within 0.01 Da (or 20 ppm) of `PEPMASS` and whose retention time is within `--ms2-rt-window` seconds (default 30) of `RTINSECONDS`, records them in `ms2_scans` with `energy_source` `mzXML:MS2_precursor_match`, and accepts the energy only when those scans agree.

Feature finders export every feature as [M+H]+ by default, so the converter also derives each feature's ion from the features co-eluting with it (within `--ion-rt-window` seconds, default 3). It uses every feature in the MGF, including those outside `--feature-ids`. Co-eluting features whose neutral masses agree under different adducts ([M+H]+, [M+Na]+, [M+NH4]+, [M+K]+, [M+2Na-H]+, [M+H+HCOONa]+, [M+H-H2O]+; negative-mode counterparts) share one molecule, and each is written with its own `>ionization`. A feature whose neutral mass is the sum of two co-eluting molecules, at least one confirmed by its own adduct partner, is a `multimer` ([2M+H]+ or [M1+M2+H]+). A lighter feature without adduct partners whose m/z is a peak in the MS2 of a heavier co-eluting molecule, at a plausible neutral loss (at least 14 Da, organic mass defect), is an `in_source_fragment`. The partner check keeps a precursor's two complementary fragments from passing as dimer monomers. Multimers, in-source fragments and adducts the models do not support get that manifest status with the related features instead of being converted. Pass `--elucidate-all-ions` to convert them anyway, or `--no-ion-identity` to keep the MGF's own or the default adduct. A non-default adduct stated in the MGF (`ADDUCT`/`ION`) takes precedence. MSBuddy receives the adduct. Atlas coverage depends on the requested adduct. If a downloaded MGF contains a different adduct, `run` uses its structure list and predicts every structure with local ICEBERG for the experimental adduct (`reason: atlas_lacks_adduct`). Missing or inconsistent values produce `needs_energy` in the manifest; use `--energy` only after confirming the value. Bruker Q-TOF collision settings are usually absolute eV, set per precursor m/z; check the actual MS2 scan in raw mzXML and confirm the unit with the provider. Batch shares an atlas cache and resumes completed features. It processes several features at once in worker processes when CPU and RAM permit, so atlas lookups run in parallel and the GPU stays busy while other features run formula, atlas and scoring steps. `max_workers: auto`, the default, uses up to 8 workers, sized from CPU threads and RAM at about 1.5 GB per worker, and `setup` saves the value. Model jobs are limited to one at a time by default, and a new feature starts only while free RAM stays above `--min-free-memory-gb`. Override with `--max-workers`, `--max-model-jobs` and `--min-free-memory-gb`. Install `.[batch]` for `psutil`. As its final step, batch writes `review_report.html`, one self-contained review page (the `visualize --export` page) for every finished feature, and prints the command that serves the interactive viewer; `--serve` starts that viewer right away and `--no-review-page` skips the page. See [the scientific workflow](../.agents/workflows/msms-elucidation.md) for the full route. The clinical demo uses three unknowns from `ms-pred/data/exp_specs/clinical/`: `csf_unknown.ms`, `plasma_unknown_198.ms`, and `plasma_unknown_583.ms`. See the [demo guide](../demo/README.md) for the spectra and interactive viewer.

## Job monitoring and incremental review

Follow [job monitoring and early review](job-monitoring.md): keep supervising queued/running jobs after launch, inspect live progress and failures, and review stable per-feature results while the rest of the batch continues. Do not wait for the consolidated report to begin the workflow's agent review. Save per-feature `evidence_review.json` files with source identity and coverage state, mark partial results as provisional, and refresh them when outputs change. Progress viewers are labelled partial; a complete batch requires verified final outputs and full feature accounting.

## Interactive fragment review

[msms-evidence-review](../.agents/skills/msms-evidence-review/SKILL.md) assesses existing candidates, including high-scoring matches, against explicit matched and unexplained peaks across all supplied energies. Its continuous queue refills available reviewer slots from ready initial assessments and required refreshes while the main agent monitors jobs; deeper investigations remain in a separate follow-up queue. Reviewers return separate drafts; the main agent checks their reasoning and current source identity, publishes accepted `evidence_review.json` files, and asks about optional routes. Configure delegation under `pipeline.evidence_review`; direct review remains available when subagents are unavailable.

[msms-structure-proposal](../.agents/skills/msms-structure-proposal/SKILL.md) guides the agent to propose candidate SMILES from explicit peak evidence: inspect matched/unmatched peaks for each current candidate, derive structural constraints, record graph edits and counterevidence in `proposal_evidence.json`, and hand a plain SMILES array (`proposals.json`) to [msms-predict](../.agents/skills/msms-predict/SKILL.md#score-candidate-structures) for scoring. The CLI `review --smiles-json` validates and scores that supplied list; `--proposals pubchem` retrieves database candidates. Neither CLI option invents structures from peaks. Use [msms-denovo](../.agents/skills/msms-denovo/SKILL.md) for FRIGID spectrum-to-structure generation. Proposed structures remain hypotheses until assessed against the data; the workflow does not establish chemical novelty or a unique identification.

After retrieval or candidate review, open the separate `msms-visualize` skill:

```bash
"${MS_PRED_PYTHON:-python}" -m pip install -e '.[visualize]'
msms-structure-elucidation visualize --result results/sample/retrieval.json
# Several unknowns: one page with a searchable list of all of them
msms-structure-elucidation visualize --result results/*/retrieval.json
```

Open the printed localhost URL for an overview of every unknown and a review workspace: hover or click peaks in the mirror plot to highlight predicted fragments on the structure, compare candidates, and record decisions and fragment comments, which autosave. Add `--export review_report.html` to write one self-contained read-only report instead of serving. Notes are written to `review_notes.json` beside the result; `retrieval.json` remains the computed record. The viewer uses cached atlas predictions and makes no model or network calls. Existing results can recover fragment IDs from their cached atlas MGF; pass `--atlas-mgf` if that file was moved.

**Download SMILES CSV** saves `ranked_smiles.csv` with one row for every loaded unknown, including those hidden by filters or on other pages. The columns are `unknown_id,top1_smiles,top1_similarity,...,top10_smiles,top10_similarity`; `unknown_id` is the ID shown in the viewer. Candidate order and entropy similarity values match the saved rankings, and missing ranks or scores are blank. This download also works offline in newly exported reports, retaining the top 10 rankings even when the interactive report displays fewer candidates.

Spectrum QC is enabled by default in the viewer: an unknown must have at least **5 non-precursor peaks** with intensity **≥10% of the base peak**. Both thresholds are adjustable; turn off **Spectrum QC** to inspect every unknown. QC uses the full experimental spectra, merges all energies within 20 ppm by summed intensity, excludes peaks within ±20 ppm of precursor m/z before finding the base peak, and counts each merged peak once. Empty spectra fail; unknowns without a precursor m/z cannot be assessed and are hidden while QC is enabled. The **QC peaks** column shows the count at the selected intensity threshold. QC updates summary counts, charts, the unknown table, search palette and previous/next navigation; it does not change saved spectra, rankings or reviews. New static exports retain QC summaries from the full spectra even when their displayed spectra are trimmed.

Datasets with more than 100 unknowns use pages of 100 in the overview. Search, filters, sorting, and previous/next unknown navigation apply across all unknowns. Unknowns without comparable candidates still show their experimental spectra with energy selection, peak inspection, and zoom; multiple energies are merged by default. These views are also available in exported reports.
