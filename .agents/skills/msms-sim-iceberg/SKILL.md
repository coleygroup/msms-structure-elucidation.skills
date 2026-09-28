---
name: msms-sim-iceberg
description: Predict LC-MS/MS spectra and fragment assignments from SMILES with ICEBERG when a precomputed public atlas spectrum is unavailable.
---
# ICEBERG simulation

Use public atlas retrieval first for ordinary PubChem structures. For a candidate missing from the atlas, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if no checkout exists, read that checkout's `README.md` **Install & setup** section, and follow its CPU or CUDA environment instructions for the host. Verify the resulting interpreter before simulation. `bash setup_envs.sh` is only a CPU venv helper; check the upstream README first. Then supply ICEBERG generator and intensity checkpoints. Open-source MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym). A user with a NIST license can provide local NIST-trained checkpoints; do not distribute them.

For an experimental spectrum and proposed SMILES, use the portable review command:

```bash
msms-structure-elucidation review --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json --model iceberg --ms-pred-dir /path/to/ms-pred \
  --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt
```

For simulation alone, run the existing ms-pred adapter with the model interpreter:

```bash
"${MS_PRED_PYTHON:-python}" .agents/skills/msms-sim-iceberg/scripts/predict_msms.py \
  --smiles 'CCO' --gen_ckpt /path/to/gen.ckpt --inten_ckpt /path/to/inten.ckpt \
  --collision_energies 20 40 --output_dir results/iceberg
```

The adapter accepts integer eV values and calls ICEBERG with `nce=False`. If the source is experimental NCE, confirm that unit with the provider, convert using precursor m/z, and round to integer eV before invoking it. For atlas energy gaps, `run` automatically calls ICEBERG on all atlas structures for that formula with no total candidate cap, using resumable shards; set both checkpoints, an ms-pred checkout, and bounded batch/model workers. Validate collision energies and adduct before comparing predicted and experimental spectra. Similarity is not calibrated confidence.

## Inference configuration anchors

These ms-pred settings (`models.simulator` in `configs/default.yaml`, or `--cuda-devices`, `--model-batch-size`, `--model-cpu-workers` and `--model-gpu-workers` on `run`, `review` and `batch`) were measured to work well on these hosts:

| Host | GPU memory | `cuda_devices` | `batch_size` | `num_cpu_workers` | `num_gpu_workers` |
|---|---|---|---|---|---|
| Laptop, RTX 4070 Laptop GPU, 16 CPU threads, 16 GB RAM | 8 GB | `0` | 16 | 16 | 2 |
| Workstation, RTX A5000 | 24 GB | `[1]` | 128 | 16 | 2 |

Only `batch_size` changes between the two hosts, because GPU memory is the limit. For other hosts:

- **GPU memory is the batch_size limit.** Each ICEBERG GPU worker loads its own model copy onto the GPU, so each copy gets about (free VRAM) / `num_gpu_workers`. Batch size grows faster than VRAM: 3x the memory allowed 8x the batch, because the model weights take a fixed share. Pick the nearest anchor, check `nvidia-smi` during the first shard, and double batch_size while peak use stays under about 80%. On CUDA OOM, halve batch_size before reducing `num_gpu_workers`. Large molecules or a higher `max_nodes` need smaller batches.
- **`num_gpu_workers`: use 2 per visible GPU.** Two ICEBERG processes per GPU overlap one worker's CPU-side fragment-DAG work with the other's GPU work. Workers are assigned round-robin across `CUDA_VISIBLE_DEVICES`, so 2 GPUs means 4. GLACIER always uses 1, because more than 1 splits its output into shard files.
- **`num_cpu_workers`: about the number of logical CPU threads, up to 16.** On GPU runs, ICEBERG uses these workers only to prepare entries. GLACIER uses them as DataLoader featurization workers. On CPU-only runs, ICEBERG runs one full model per CPU worker, so the limit is RAM, not threads. Start with 4 on a 16 GB machine.
- **`batch` runs several features at once, if CPU and RAM permit.** Each feature runs in its own worker process, which loads ms-pred once and uses one CPU core. Formula inference, atlas download and scoring for many features therefore run in parallel while ICEBERG jobs use the GPU. `models.batch.max_workers: auto`, the default, sizes the pool as min(8, CPU threads, (RAM − `min_free_memory_gb` − 4 GB per model job) / 1.5 GB). A worker measured about 1.4 GB. On the 16 GB laptop above that gives 4; a 32 GB or larger host gets 8. A new feature starts only while free RAM stays above `min_free_memory_gb`. Model jobs still queue behind `max_model_jobs`, and the GPU runs `max_model_jobs × num_gpu_workers` model copies at once, so keep `max_model_jobs: 1` per GPU and parallelize through `num_gpu_workers`.
- **Increase `shard_size` on large GPUs.** Every shard starts a fresh ms-pred subprocess and reloads the checkpoints. With batch_size 128, 2 GPU workers and 5 energies, a 256-SMILES shard is only 10 batches, so setup time dominates. 1024–2048 keeps an A5000 busy. Changing shard_size or batch_size changes the shard signatures, so a partial run restarts those shards instead of resuming them.
- CPU-only: leave `cuda_devices: null` and `num_gpu_workers` has no effect. Use batch_size 1–8.
