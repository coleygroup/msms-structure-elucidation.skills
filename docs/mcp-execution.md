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
| `scheduler_process` | Default `true`: one detached local dispatcher; workers publish state and wait. |
| `max_live_pipelines` | Cap live CPU pipelines including GPU/cache/CPU waiters; `auto` is CPU slots plus the prepared limit. |
| `route_canary` | Default `true`: one successful PubChem production request precedes bulk admission for that model/configuration. |
| `environment_failure_limit` | Default 3: hold new admissions for repeated classified environment errors; a failed environment canary holds immediately. |
| `worker_environment` | Explicit environment such as a verified `SSL_CERT_FILE`; cannot override scheduler GPU visibility or pipeline ownership. |
| `min_free_memory_gb` | Available-RAM floor for starting new local CPU jobs; default 2 GiB. Applied when the platform reports available RAM. |
| `max_prepared_jobs` | Pause new CPU preparations when this many orchestrators await inference. `auto` allows two per GPU, with a minimum of two. Already admitted CPU jobs may finish preparing. |
| `prediction_group_size` | `auto` estimates candidates from saved model throughput, target duration and energies, falling back to 4096. An integer sets an explicit size. CUDA batch size is independent. |
| `target_prediction_seconds` | Approximate GPU work per group, default 600 seconds; does not delay small requests. |
| `max_group_candidates` | Upper bound on automatically sized groups, default 32768. |
| `prepared_groups` | `auto`: two in-flight groups per endpoint slot, capped at eight per orchestrator. |

The RAM floor is an admission check, not a hard memory limit. Tune CPU slots, preparation workers, group size and model batch size together for the host. A larger prediction group amortizes Python startup, checkpoint loading and writer startup while retaining resumable groups. Native model processes still load their weights for each uncached group; the adapter does not keep a resident model across requests.

### Size independent CPU feature jobs

`max_cpu_jobs` controls independent feature preparation/scoring jobs; the default of two is a conservative starting point. GLACIER's `num_cpu_workers` controls featurization inside each native prediction process. Measure and tune these separately.

Use `server_info.cpu_workload` to distinguish working CPU jobs from `waiting_gpu`, `waiting_cache`, `waiting_cpu`, submitted jobs and the coordinator queue. The report includes the effective resident-pipeline limit, the active CPU and prepared-waiter limits when enforced locally, and the resources configured for new Slurm CPU jobs. Check `max_prepared_jobs` and prediction-group look-ahead when a pool's GPUs lack upcoming work. Submitted Slurm jobs have not necessarily started. A running CPU allocation can be waiting for predictions; use host CPU samples and job phases together.

Budget CPU cores and RAM for all resident parents after reserving capacity for the operating system, other users and local GPU inference workers. Measure each CPU job's complete process tree or cgroup, including scientific subprocesses, and add memory headroom. Check native thread pools against the per-job CPU allocation. On Slurm, use the intended compute-node/account capacity and scheduler policy; the login host's CPU count is not that budget. On Kubernetes, use the applicable requests, limits and remaining quota.

Call MCP `plan_cpu_concurrency` with `cpu_budget`, `memory_budget_gb`, `cpus_per_job`, `memory_per_job_gb` and an optional `max_jobs`. Without MCP:

```bash
msms-structure-elucidation plan-cpu \
  --cpu-budget 24 --memory-budget-gb 64 \
  --cpus-per-job 2 --memory-per-job-gb 4
```

The read-only planner returns a capacity ceiling and an `mcp_settings` fragment; insufficient budgets return no fragment. The example allows at most 12 parents. The fragment bounds both `max_cpu_jobs` and `max_live_pipelines`, including resident waiters. With Slurm, `max_live_pipelines` is the submission cap for CPU parents; `max_cpu_jobs` alone contributes to the automatic cap and does not impose a separate active-job limit. CPU parents keep their CPU/RAM allocations during prediction waits, so increasing their count can increase reservations without increasing useful CPU work.

Compare representative completed-feature throughput, preparation/scoring times, peak memory and prediction-wait fractions at increasing concurrency, keeping inputs, weights, energies and cache conditions fixed. Increasing concurrency only helps while the relevant resources and downstream capacity have headroom. Save measured limits and CPU task resources in ignored `configs/local.yaml`; a capacity ceiling is not a measured optimum. Configure `mcp.slurm.cpu` or Kubernetes CPU resources to match the measured job footprint. Apply changes through a controlled coordinator/dispatcher reload while retaining existing native jobs, request identities and partial results.

Tune the models differently. For ICEBERG, start with two parallel GPU workers per GPU and compare two to four, adjusting the batch size to fit their combined memory use. `models.simulator.num_gpu_workers` is the total across configured `cuda_devices`; the scheduler scales it to the GPUs allocated to each job. For GLACIER, keep one prediction process per GPU and increase `models.simulator.glacier.batch_size` until throughput plateaus or memory limits are reached. Keep CPU workers and writers fixed initially, then adjust them if native timing shows feature or writer waits. Save measured settings in `configs/local.yaml`.

Local scheduling uses one detached process per job root. Startup is arbitrated on local temporary storage; the driver holds one shared ownership lease. Waiting workers never schedule the queue. CPU-ready requests resume in FIFO order before new preparations, and compact live-job and parent/child indexes keep closed history out of scheduling scans. A complete raw-cache hit uses a read snapshot without writer leases. Cancellation and failed-child propagation preserve the affected requests and evidence.

The local driver survives client disconnection and exits after 60 idle seconds. `scheduler-service.json` reports its host, PID, heartbeat and admission state. Write `scheduler-stop.json` in that root to stop scheduling without cancelling existing allocations; remove it only for an authorized resume. Restart the driver to apply configuration changes. Stop old dispatchers before upgrading and preserve requests, caches, checkpoints and partial results; never replace code underneath live workers. `scheduler_process: false` is for a caller that continuously owns and pumps the backend itself.

With Slurm, CPU orchestrators use `mcp.slurm.cpu` resources without requesting a GPU, including its CPU-specific setup and extra arguments. Each missing-prediction stage gets a separate GPU allocation using `mcp.slurm`. CPU allocations remain alive while waiting. The coordinator bounds live CPU parents and route admission before submission; Slurm controls actual allocation. Keep one active MCP coordinator per queue. The job root and model assets must be accessible from both CPU and GPU nodes; native outputs are published into the shared job directory.

For several SSH/local/Slurm/Kubernetes endpoints, use `mcp.backend: pool` and the [prediction server configuration](prediction-servers.md). The coordinator owns CPU preparation, shared raw-cache reuse and experimental scoring. It dispatches groups to compatible endpoints, verifies streamed artifacts before remote cleanup, and retains endpoint ownership through connection failures. Kubernetes uses finite Jobs, explicit context/namespace, persistent storage and a bounded CPU gateway for remote file transfer.

## Monitoring and measurement

Validate the actual intended route on the execution host before a large campaign. A direct-scoring test does not exercise PubChem connectivity. Use production's CPU interpreter, environment, scientific runtime and scheduler profile. Test fresh inference when needed, verified collection, cache reuse and independent experimental scores. A cache-only canary does not establish GPU execution. Asset readiness in `server_info` describes configuration, not completed validation.

The first PubChem request is a production-route canary by default. Later requests remain queued until it succeeds. TLS, missing-dependency and model-configuration failures are classified; environment holds persist across reconnects without cancelling unrelated running work. `server_info` exposes admission scopes; held jobs include `admission_hold`. Repair the environment with TLS verification enabled, verify the CPU path, then call `resume_admission(scope, reason)`; the next queued request becomes the canary. Explicitly retry any failed attempts, preserving their evidence. Set a required, verified CA bundle through `mcp.worker_environment.SSL_CERT_FILE`. PubChem's request budget is shared across coordinator processes.

For a batch, submit independent MCP jobs with `wait_seconds=0`, then poll their IDs. Submit dependent steps only when their required inputs are ready. Waiting for each whole feature before submitting the next leaves no upcoming work for the preparation queue.

GPU children save `gpu-samples.csv` and `gpu-metrics.json`; `get_job` returns their `gpu_metrics`. `mcp.gpu_monitor_interval_seconds` sets the sampling interval (default 1 second, zero disables it). Samples use scheduler GPU IDs when available and include zeros, allocated-task timing, peak sampled memory and observed foreign compute processes. These whole-task measurements include startup, native preparation, writer drain and publication; derive sustained inference separately from native log boundaries and validate spectrum counts before calculating throughput.

`get_job` exposes `phase`, accumulated `phase_seconds` and the current `prediction_job`. Phases distinguish CPU preparation, waiting for a shared prediction, waiting for a GPU, GPU prediction, waiting for a CPU slot and CPU scoring. Poll the parent until terminal; an internal prediction completing does not mean experimental scoring has finished. See [job monitoring](job-monitoring.md).

For low GPU utilization or a long completion estimate, locate the slow phase before increasing GPU workers or batch size. Check CPU saturation, preparation queues, shared-cache waits, fragment merging and scoring as well as native inference timing. Stepped scoring decodes fragment masks once per temporary component and uses the native merge and normalization; raw cached spectra remain reusable when only this scoring calculation changes. Validate scoring optimizations against fixed saved predictions, comparing masses, intensities, fragment IDs, similarities, explained intensity and matched peaks exactly. Report a CPU replay speedup separately from measured inference or whole-workload gains. Keep fixes in the shared implementation and leave supplied model checkouts unchanged.

To measure an installation, freeze inputs, native source, weights and scientific settings, use separate cold prediction caches, and compare revisions on the same idle GPU. [The local benchmark script](../scripts/benchmark_mcp_pipeline.py) runs a fixed JSON request list through the local backend and runner, records phase transitions and samples `nvidia-smi` utilization every 0.5 seconds. It excludes client transport and fixture preparation; it does not benchmark remote or pooled backends. Compare terminal feature accounting and hashes of peaks, fragments and scores as well as elapsed time. Sampled utilization is an observation, not a guarantee of saturation.

Report whole-workload utilization and sustained inference utilization separately. Use a representative prediction group large enough to measure many batches after startup, define the inference window from native logs, and include throughput, peak memory and sampling interval. A brief peak or an average over only nonzero samples does not establish sustained utilization. A short inference loop cannot diagnose CPU starvation or establish the best batch size; inspect native feature-wait, forward and writer-wait timing before changing the architecture.

[The scheduler benchmark](../scripts/benchmark_mcp_scheduler.py) exercises concurrent CPU handoffs on an isolated job root, including NFS, checking request accounting, waiting time and observed process bounds. It loads no models and does not measure GPU throughput. Keep explicit group-size overrides only when measured: a small override takes precedence over the duration target and causes frequent model restarts. Persistent model serving across features is not implemented.
