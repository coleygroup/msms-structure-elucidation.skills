# CPU preparation, GPU prediction and CPU scoring

MCP forward-prediction and retrieval requests start as CPU orchestrators. They inspect spectra, retrieve and validate candidates, and check the shared raw-prediction cache before requesting GPU resources. A complete cache hit needs no GPU job. Requests waiting for another feature's cached predictions release their local CPU slot so the owner can finish and publish those predictions.

For missing spectra, the orchestrator submits an internal `model_inference` job. GLACIER's native entry preparation runs in the CPU stage and writes prepared entries into the job directory. Its GPU stage uses native `run_shard`, including its CPU featurization workers, batch prefetching and parallel HDF5 writers. ICEBERG prepares molecular identities and condition rows on CPU, then invokes its native `predict_smis` with parallel GPU workers. Native featurization still runs in the predictors; ICEBERG's generated fragment graphs require its generator. Both models retain their selected weights, adduct, instrument, energies, fragments and scientific cache identities.

Native HDF5 writes use GPU-node scratch; complete output files are then published into the shared job directory. The GPU job finishes before the orchestrator decodes and publishes validated raw predictions, scores the experimental spectrum, renders structures and builds the result. Each unknown keeps its own experimental scores and evidence review. Cache and GPU waiters reacquire a CPU slot before resuming. The scheduler prioritizes these resumptions over new preparations.

## Resource settings

Set machine-specific values in ignored `configs/local.yaml`:

| Setting under `mcp` | Meaning |
| --- | --- |
| `max_concurrent_jobs` | Local GPU-job limit; `auto` uses one slot per detected, allowed GPU, or one CPU inference slot without GPUs. |
| `gpu_devices` | Optional list of physical GPU indices allowed for local jobs; `null` allows all detected GPUs. |
| `max_cpu_jobs` | Independent local preparation/scoring limit; default 2. Sleeping orchestrators do not occupy these slots. |
| `min_free_memory_gb` | Available-RAM floor for starting new local CPU jobs; default 2 GiB. Applied when the platform reports available RAM. |
| `max_prepared_jobs` | Pause new CPU preparations when this many orchestrators await inference. `auto` allows two per GPU, with a minimum of two. Already admitted CPU jobs may finish preparing. |
| `prediction_group_size` | `auto` estimates candidates from saved model throughput, target duration and energies, falling back to 4096. An integer sets an explicit size. CUDA batch size is independent. |
| `target_prediction_seconds` | Approximate GPU work per group, default 600 seconds; does not delay small requests. |
| `max_group_candidates` | Upper bound on automatically sized groups, default 32768. |
| `prepared_groups` | `auto`: two in-flight groups per endpoint slot, capped at eight per orchestrator. |

The RAM floor is an admission check, not a hard memory limit. Tune CPU slots, preparation workers, group size and model batch size together for the host. A larger prediction group amortizes Python startup, checkpoint loading and writer startup while retaining resumable groups. Native model processes still load their weights for each uncached group; the adapter does not keep a resident model across requests.

Tune the models differently. For ICEBERG, start with two parallel GPU workers per GPU and compare two to four, adjusting the batch size to fit their combined memory use. `models.simulator.num_gpu_workers` is the total across configured `cuda_devices`; the scheduler scales it to the GPUs allocated to each job. For GLACIER, keep one prediction process per GPU and increase `models.simulator.glacier.batch_size` until throughput plateaus or memory limits are reached. Keep CPU workers and writers fixed initially, then adjust them if native timing shows feature or writer waits. Save measured settings in `configs/local.yaml`.

Local scheduling uses a shared interprocess lock, so the server and detached orchestrators can dispatch work safely. Canceling a parent cancels its active prediction children. A prediction failure is propagated to the parent rather than reported as an empty candidate result. The runner saves the submission-time configuration for provenance. Keep source configuration and model files unchanged until active runs finish.

With Slurm, CPU orchestrators use `mcp.slurm.cpu` resources without requesting a GPU. Each missing-prediction stage gets a separate GPU allocation using `mcp.slurm`. The CPU allocation remains alive while waiting; Slurm controls CPU/GPU admission. Local concurrency settings do not replace Slurm's resource limits. The job root, input files, model checkout and checkpoints must be accessible from both CPU and GPU nodes; native outputs are published into the shared job directory.

For several SSH/local/Slurm/Kubernetes endpoints, use `mcp.backend: pool` and the [prediction server configuration](prediction-servers.md). The coordinator owns CPU preparation, shared raw-cache reuse and experimental scoring. It dispatches groups to compatible endpoints, verifies streamed artifacts before remote cleanup, and retains endpoint ownership through connection failures. Kubernetes uses finite Jobs, explicit context/namespace, persistent storage and a bounded CPU gateway for remote file transfer.

## Monitoring and measurement

For a batch, submit independent MCP jobs with `wait_seconds=0`, then poll their IDs. Submit dependent steps only when their required inputs are ready. Waiting for each whole feature before submitting the next leaves no upcoming work for the preparation queue.

GPU children save `gpu-samples.csv` and `gpu-metrics.json`; `get_job` returns their `gpu_metrics`. `mcp.gpu_monitor_interval_seconds` sets the sampling interval (default 1 second, zero disables it). Samples use scheduler GPU IDs when available and include zeros, allocated-task timing, peak sampled memory and observed foreign compute processes. These whole-task measurements include startup, native preparation, writer drain and publication; derive sustained inference separately from native log boundaries and validate spectrum counts before calculating throughput.

`get_job` exposes `phase`, accumulated `phase_seconds` and the current `prediction_job`. Phases distinguish CPU preparation, waiting for a shared prediction, waiting for a GPU, GPU prediction, waiting for a CPU slot and CPU scoring. Poll the parent until terminal; an internal prediction completing does not mean experimental scoring has finished. See [job monitoring](job-monitoring.md).

To measure an installation, freeze inputs, native source, weights and scientific settings, use separate cold prediction caches, and compare revisions on the same idle GPU. [The local benchmark script](../scripts/benchmark_mcp_pipeline.py) runs a fixed JSON request list through the local backend and runner, records phase transitions and samples `nvidia-smi` utilization every 0.5 seconds. It excludes client transport and fixture preparation; it does not benchmark remote or pooled backends. Compare terminal feature accounting and hashes of peaks, fragments and scores as well as elapsed time. Sampled utilization is an observation, not a guarantee of saturation.

Report whole-workload utilization and sustained inference utilization separately. Use a representative prediction group large enough to measure many batches after startup, define the inference window from native logs, and include throughput, peak memory and sampling interval. A brief peak or an average over only nonzero samples does not establish sustained utilization. A short inference loop cannot diagnose CPU starvation or establish the best batch size; inspect native feature-wait, forward and writer-wait timing before changing the architecture.
