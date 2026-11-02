---
name: msms-predict
description: Predict MS/MS spectra and fragment assignments from SMILES with GLACIER or ICEBERG, and score supplied candidate structures against experimental spectra.
---
# MS/MS prediction with GLACIER or ICEBERG

Use this skill to predict spectra and fragments for known candidate SMILES. Read [saved pipeline answers](../../../docs/pipeline-preferences.md) for the model, exact weights, adduct, instrument, collision energies and NCE/eV unit. Reuse confirmations within their recorded scope/context; ask only for missing or conflicting details and save the answers. Use `msms-setup` to configure the model host and interpreter; either model is sufficient.

| Model | Required weights | Prediction route |
|---|---|---|
| GLACIER | One joint-model checkpoint | Predict every candidate; no open GLACIER atlas is available. |
| ICEBERG | Generator and intensity checkpoints | Prefer suitable atlas spectra; predict missing spectra or all candidates when direct predictions are requested. |

The public atlas uses ICEBERG 2.1 trained on NIST'23. If local ICEBERG weights differ or matching weights have not been verified, tell the user that combining their predictions may affect ranking comparability, prefer suitable atlas spectra, and identify the local checkpoint. Use `msms-atlas-retrieval` to rank experimental spectra through the atlas or a mass-only PubChem search, and `msms-structure-proposal` for proposed candidates.

## Collision-energy handling

For supplied raw/mzML data, first inspect acquisition metadata yourself using `msms-structure-elucidation inspect-energy --input sample.mzML` and available vendor method/scan records. Use explicit, consistent settings and units for the relevant MS2 scans; ask the user only when they remain unresolved or conflict. Follow [collision-energy handling](../../../docs/collision-energy.md) and save the verified setting with its source/input context.

Distinguish three separate spectra from one stepped acquisition. For a single 20/40/60 stepped-NCE spectrum, predict the three component energies, combine their intensities with equal weights, normalize the combined prediction, and score it once. Preserve all component settings; do not average NCE numbers or average three similarities against the same measured spectrum. Forward prediction uses `--stepped` or MCP `stepped: true`; scoring uses the explicit `.ms` stepped metadata described in the guide.

## With the prediction server

Call `server_info` first, then `predict_spectra` with the selected `model` (`glacier` or `iceberg`), candidate `smiles`, confirmed `collision_unit`, `collision_energies`, `adduct` and `instrument`. Set `include_fragments: true` for per-peak fragment annotations. For an experimental spectrum and a candidate list, use `score_candidates` with the same selected model and an explicit target formula.

Jobs may return a `job_id`; keep polling with `get_job(job_id, wait_seconds=30)` while work is queued or running, following [job monitoring](../../../docs/job-monitoring.md). The full result contains predicted spectra, fragment IDs, model version and checkpoint provenance. For a direct mass-only PubChem search, use `search_pubchem_mass`; it predicts every eligible candidate with the selected model and ranks across formulas.

## Reuse predictions across unknowns

Before launching inference, reuse the shared model-host prediction cache (`models.simulator.reuse_predictions: true`). Unknowns sharing a formula often reuse the same PubChem candidate pool. Deduplicate candidates and retrieve/predict the union of required structures and actual rounded eV energies once where their settings match. The cache stores raw spectra and fragment IDs per structure and energy; score every unknown against its own experimental spectra and write separate evidence reviews.

A formula alone is not a reuse key. Match canonical stereo-free structure, model, exact weight content, model/adapter source, adduct, instrument, sparse-peak settings and actual model eV. Different NCE values can map to the same eV, and identical NCE values with different precursor/adduct settings can map differently. Stepped spectra reuse their component predictions but merge and score for each acquisition separately. Predict missing structure/energy pairs only; never copy another unknown’s matched peaks, scores or review. Candidate locks prevent concurrent unknowns from computing the same missing prediction twice. Cache receipts record reused/new spectrum counts and scientific identities. Keep the cache on the model host, outside Git; configure its path in `models.simulator.prediction_cache`. Set `reuse_predictions: false` only when recomputation is explicitly needed.

## GLACIER defaults

Use `models.simulator.glacier` as GLACIER’s own profile; generic simulator batch/worker settings are ICEBERG settings. Begin with:

| Setting | Default |
| --- | ---: |
| Batch size | 128 |
| CPU workers per prediction process | 4 |
| GPU workers per prediction process | 1 |
| Prediction processes per GPU | 1 |
| HDF5 writers | 2 |
| HDF5 chunks | 4 |
| Feature cache size | 256 |
| Sparse peaks | 100 |

The launcher passes `num_writers`, `num_h5_chunks`, `feat_cache_size` and `sparse_k` to GLACIER’s native `predict_smis_joint.py`, alongside the batch/worker settings. Each visible GPU gets one native prediction process; the backend normally assigns one GPU per job. The reader loads all resulting HDF5 chunk/shard files. Keep one prediction process per GPU. Use a compatible native runtime exposing `--num-writers`, `--num-h5-chunks` and `--feat-cache-size`; the launcher reports missing flags explicitly. Tune batch size, CPU workers and I/O for the machine if measurements justify it, saving overrides under `models.simulator.glacier` in the ignored local config. Sparse peak count changes the prediction and invalidates shared spectra; batch size, CPU workers and I/O tuning do not authorize reuse with different weights or scientific settings.

## Monitor progress and review early

Stay with the job after launch, including detached runs and waits for idle GPUs. Check live job/process states, logs, progress and failures, and keep the user informed. As soon as stable per-feature results are available, use [msms-evidence-review](../msms-evidence-review/SKILL.md) and write a per-feature `evidence_review.json` while other predictions continue. Mark incomplete coverage as provisional and revisit changed results. Resume monitoring between reviews; wait for verified outputs and feature accounting before reporting completion. Follow [job monitoring and early review](../../../docs/job-monitoring.md).

## Score candidate structures

Accept the experimental `.ms` spectrum, a target neutral formula, a JSON array of candidate SMILES, and the reference candidates to compare. Reuse confirmed collision units, adduct, instrument, model and exact weights. Validate SMILES/formulas and deduplicate connectivity, retaining proposal origins and rejected-input reasons. Keep separate formula hypotheses in separate scoring groups.

With MCP connected, call `server_info` first, then `score_candidates` with the complete comparison pool, experimental spectrum, explicit `formula`, confirmed `collision_unit`, `instrument` and `model`. Set `top_k` high enough to retain the candidates needed for comparison and check the scored count. For GLACIER or a request for fresh predictions throughout, score the whole pool with the selected model and weights. CLI `review` retains existing scores and can reuse ICEBERG atlas entries even with `--model glacier`, so it does not provide a uniform GLACIER rescore.

For the ICEBERG route, prefer suitable atlas spectra under the checkpoint/provenance policy above. Without MCP, extend an existing result with validated candidate SMILES using:

```bash
msms-structure-elucidation review \
  --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json \
  --ms-pred-dir /path/to/ms-pred \
  --model iceberg \
  --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt
```

This command validates supplied structures, retains existing candidates, reuses suitable atlas matches and predicts missing structures. It updates `retrieval.json` and `report.html`; it does not generate new structures. Use `--formula` to select a formula when the result contains several. For a PubChem candidate expansion, replace `--smiles-json` with `--proposals pubchem`; `--max-structures` defaults to 500 and is a retrieval cap, not exhaustive coverage. Follow the environment and tuning sections below for interpreter, checkpoint and worker settings.

Keep every supplied experimental energy in the comparison. Convert confirmed NCE to rounded integer eV before inference (`nce=False`); when several inputs round to one model energy, predict it once and compare it against each input spectrum. Preserve model version, checkpoint provenance, energy alignment, predicted spectra, matched peaks, entropy similarity and explained intensity with the results. Return those results to the proposing agent to assess newly explained peaks, lost matches and remaining uncertainties, and use `msms-visualize` for inspection.

## Without MCP

Run the shared prediction launcher on the configured model host. This example uses confirmed absolute eV and GLACIER weights saved in `configs/local.yaml`:

```bash
python .agents/skills/msms-predict/scripts/predict_msms.py \
  --smiles 'CCO' --model glacier \
  --collision-energies 20 40 --collision-unit eV \
  --instrument Orbitrap --output results/prediction/predictions.json
```

Select `--model iceberg` for ICEBERG. Supply `--checkpoint` for GLACIER or `--gen-checkpoint` and `--inten-checkpoint` for ICEBERG when weights are not configured. The launcher accepts `--ms-pred-dir`, `--ms-pred-python`, `--config`, `--cuda-devices` and host-tuned batch/worker settings. It uses the model environment and checkout through the same backend as the MCP tools.

The output JSON contains spectra, fragment IDs and annotations, the supplied energy unit, model settings and checkpoint provenance. `--no-fragments` skips atom-level fragment annotations. The GLACIER checkpoint preflight checks feature compatibility, and GLACIER uses one GPU worker. Model failures return a nonzero exit code.

For an ICEBERG spectrum plot and fragment SMILES, use the ICEBERG-specific helper with the model interpreter:

```bash
"${MS_PRED_PYTHON:-python}" .agents/skills/msms-predict/scripts/predict_iceberg.py \
  --smiles 'CCO' --gen_ckpt /path/to/gen.ckpt --inten_ckpt /path/to/inten.ckpt \
  --collision_energies 20 40 --output_dir results/iceberg
```

This plotting helper accepts integer eV, calls ICEBERG with `nce=False`, and writes `spectrum.png`, `fragments.json` and `input_configs.yaml`. For confirmed NCE inputs, use the shared launcher to preserve the supplied unit, or convert NCE with precursor m/z and round to integer eV before invoking the plotting helper. Install its plotting dependencies in the model environment.

## Environment

If needed, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred), read its README **Install & setup** section, and follow the host's CPU or CUDA instructions. Set the checkout, interpreter and confirmed checkpoint paths in the uncommitted `configs/local.yaml`. `setup_envs.sh` is a CPU venv helper. Public MassSpecGym weights are linked in the ms-pred README; locally supplied NIST-trained weights remain with their licensed holder.

## Inference configuration anchors

Use these ICEBERG starting points in `models.simulator` in the uncommitted `configs/local.yaml`, or pass `--cuda-devices`, `--model-batch-size`, `--model-cpu-workers` and `--model-gpu-workers` on `run`, `review` and `batch`. Tune them for the host's available GPU memory, CPU threads and workload:

| GPU memory | `cuda_devices` example | `batch_size` | `num_cpu_workers` | `num_gpu_workers` |
|---|---|---|---|---|
| 8 GB | `0` | 16 | Up to 16 | 2 |
| 24 GB | `0` | 128 | Up to 16 | 2 |

These starting points apply to ICEBERG. Start GLACIER with its profile above and tune it independently through the native prediction path, as described in [msms-setup](../msms-setup/SKILL.md); do not inherit ICEBERG batch sizes or CPU worker counts. For ICEBERG on other hosts:

- **GPU memory limits batch size.** Each ICEBERG GPU worker loads its own model copy, so workers on the same GPU share its free memory. Model weights take a fixed share of VRAM, so the optimal batch size does not necessarily scale linearly with memory. Pick the nearest anchor, check `nvidia-smi` during the first shard, and double batch_size while throughput improves and peak memory use stays under about 80%. On CUDA OOM, halve batch_size before reducing `num_gpu_workers`. Large molecules or a higher `max_nodes` need smaller batches.
- **`num_gpu_workers`: tune 2–4 per visible GPU for ICEBERG.** Start with 2, then compare 3 and 4 on the same representative workload. Parallel processes overlap CPU-side fragment-DAG work with GPU work. Each process holds a model copy, so retune batch size within the combined memory budget. Workers are assigned round-robin across `CUDA_VISIBLE_DEVICES`; save the total worker count, so 2 GPUs means 4–8 workers. GLACIER uses one prediction process per GPU: increase its batch size until throughput plateaus or memory limits are reached, keeping CPU workers and writers fixed initially. Adjust those only when native timing shows a bottleneck.
- **`num_cpu_workers`: about the number of logical CPU threads, up to 16.** On GPU runs, ICEBERG uses these workers only to prepare entries. On CPU-only runs, ICEBERG runs one full model per CPU worker, so the limit is RAM, not threads. Start with 4 on a 16 GB machine.
- **`batch` runs several features at once, if CPU and RAM permit.** Each feature runs in its own worker process, which loads ms-pred once and uses one CPU core. Formula inference, atlas download and scoring for many features therefore run in parallel while ICEBERG jobs use the GPU. `models.batch.max_workers: auto`, the default, sizes the pool as min(8, CPU threads, (RAM − `min_free_memory_gb` − 4 GB per model job) / 1.5 GB). Allow memory headroom for each worker and model process. A 16 GB host can start with 4 workers; a 32 GB or larger host can start with 8. A new feature starts only while free RAM stays above `min_free_memory_gb`. Model jobs still queue behind `max_model_jobs`, and the GPU runs `max_model_jobs × num_gpu_workers` model copies at once, so keep `max_model_jobs: 1` per GPU and parallelize through `num_gpu_workers`.
- **Group large prediction requests.** Every group loads the checkpoints once and processes many CUDA minibatches. MCP `mcp.prediction_group_size: auto` uses the saved model throughput to target `mcp.target_prediction_seconds` (600 by default), falls back to 4096 candidates when unmeasured, and respects its configured cap. Small requests start immediately. `mcp.prepared_groups` bounds CPU preparations ahead of inference. A [server pool](../../../docs/prediction-servers.md) dispatches groups across compatible endpoints; each unknown is scored independently. Changing group size preserves compatible raw predictions. Batch size, device and worker settings preserve scientific cache identity; small numerical differences can occur in fresh GPU runs.
- **Measure sustained inference and the full workload separately.** After warm-up, use enough representative candidates for many batches. Define the inference window from native logs and report throughput, sampled GPU utilization and peak memory alongside end-to-end elapsed time. Do not infer sustained utilization from a brief peak or diagnose CPU starvation from a workload dominated by startup. Check native feature and writer waits before changing their worker counts.
- CPU-only: leave `cuda_devices: null` and `num_gpu_workers` has no effect. Use batch_size 1–8.
