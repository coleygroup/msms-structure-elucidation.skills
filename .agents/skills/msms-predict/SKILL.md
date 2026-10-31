---
name: msms-predict
description: Predict MS/MS spectra and fragment assignments from SMILES with GLACIER or ICEBERG when atlas spectra are unavailable or direct predictions are requested.
---
# MS/MS prediction with GLACIER or ICEBERG

Use this skill to predict spectra and fragments for known candidate SMILES. Read [saved pipeline answers](../../../docs/pipeline-preferences.md) for the model, exact weights, adduct, instrument, collision energies and NCE/eV unit. Reuse confirmations within their recorded scope/context; ask only for missing or conflicting details and save the answers. Use `msms-setup` to configure the model host and interpreter; either model is sufficient.

| Model | Required weights | Prediction route |
|---|---|---|
| GLACIER | One joint-model checkpoint | Predict every candidate; no open GLACIER atlas is available. |
| ICEBERG | Generator and intensity checkpoints | Prefer suitable atlas spectra; predict missing spectra or all candidates when direct predictions are requested. |

The public atlas uses ICEBERG 2.1 trained on NIST'23. If local ICEBERG weights differ or matching weights have not been verified, tell the user that combining their predictions may affect ranking comparability, prefer suitable atlas spectra, and identify the local checkpoint. Use `msms-atlas-retrieval` to rank experimental spectra through the atlas or a mass-only PubChem search, and `msms-structure-review` for proposed candidates.

## With the prediction server

Call `server_info` first, then `predict_spectra` with the selected `model` (`glacier` or `iceberg`), candidate `smiles`, confirmed `collision_unit`, `collision_energies`, `adduct` and `instrument`. Set `include_fragments: true` for per-peak fragment annotations. For an experimental spectrum and a candidate list, use `score_candidates` with the same selected model and an explicit target formula.

Jobs may return a `job_id`; poll with `get_job`. The full result contains predicted spectra, fragment IDs, model version and checkpoint provenance. For a direct mass-only PubChem search, use `search_pubchem_mass`; it predicts every eligible candidate with the selected model and ranks across formulas.

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

These starting points apply to ICEBERG. Tune GLACIER independently on each machine using its native `pred_smis` configuration and the configured prediction path, as described in [msms-setup](../msms-setup/SKILL.md); do not copy ICEBERG batch sizes or CPU worker counts as GLACIER optima. The current GLACIER adapter uses one GPU worker. For ICEBERG on other hosts:

- **GPU memory is the batch_size limit.** Each ICEBERG GPU worker loads its own model copy onto the GPU, so each copy gets about (free VRAM) / `num_gpu_workers`. Model weights take a fixed share of VRAM, so the optimal batch size does not necessarily scale linearly with memory. Pick the nearest anchor, check `nvidia-smi` during the first shard, and double batch_size while peak use stays under about 80%. On CUDA OOM, halve batch_size before reducing `num_gpu_workers`. Large molecules or a higher `max_nodes` need smaller batches.
- **`num_gpu_workers`: use 2 per visible GPU.** Two ICEBERG processes per GPU overlap one worker's CPU-side fragment-DAG work with the other's GPU work. Workers are assigned round-robin across `CUDA_VISIBLE_DEVICES`, so 2 GPUs means 4. The current GLACIER adapter forces 1 GPU worker; tune its other settings separately.
- **`num_cpu_workers`: about the number of logical CPU threads, up to 16.** On GPU runs, ICEBERG uses these workers only to prepare entries. On CPU-only runs, ICEBERG runs one full model per CPU worker, so the limit is RAM, not threads. Start with 4 on a 16 GB machine.
- **`batch` runs several features at once, if CPU and RAM permit.** Each feature runs in its own worker process, which loads ms-pred once and uses one CPU core. Formula inference, atlas download and scoring for many features therefore run in parallel while ICEBERG jobs use the GPU. `models.batch.max_workers: auto`, the default, sizes the pool as min(8, CPU threads, (RAM − `min_free_memory_gb` − 4 GB per model job) / 1.5 GB). Allow memory headroom for each worker and model process. A 16 GB host can start with 4 workers; a 32 GB or larger host can start with 8. A new feature starts only while free RAM stays above `min_free_memory_gb`. Model jobs still queue behind `max_model_jobs`, and the GPU runs `max_model_jobs × num_gpu_workers` model copies at once, so keep `max_model_jobs: 1` per GPU and parallelize through `num_gpu_workers`.
- **Increase `shard_size` on large GPUs.** Every shard starts a fresh ms-pred subprocess and reloads the checkpoints. With batch_size 128, 2 GPU workers and 5 energies, a 256-SMILES shard is only 10 batches, so setup time dominates. Larger shards of 1024–2048 candidates can reduce startup overhead on larger GPUs. Changing shard_size re-chunks the structures, so a partial run restarts those shards. `batch_size`, `cuda_devices` and worker counts do not change predictions and are not part of the shard signature, so completed shards are reused after retuning.
- CPU-only: leave `cuda_devices: null` and `num_gpu_workers` has no effect. Use batch_size 1–8.
