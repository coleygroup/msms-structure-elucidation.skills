# Prediction servers

For one machine or one cluster, use a single [execution backend](mcp-execution.md). An SSH connection to MCP on a GPU machine can use `backend: local` on that machine and all its allowed GPUs without a pool. Pool configuration is optional and is needed only to combine workers. Keep an independently verified single-backend configuration available when introducing a second server.

An MCP coordinator places requests on configured workers, monitors them, and collects final ranked results and reports. Each worker owns CPU preparation, its GPU queue, CPU output/scoring and its prediction cache. On an SSH GPU server these stages run on that machine. Slurm and Kubernetes workers keep their stages on their own cluster and shared storage; CPU jobs do not reserve GPUs or require the exact same compute node as a GPU job. SSH is a transport, independent of the worker's scheduler.

Use `msms-setup` to find compatible model environments and confirm the selected model and exact weights. Keep all real destinations, partitions, namespaces, images, storage names, paths and credentials out of the public repository. Endpoint settings live under `mcp.servers` in ignored `configs/local.yaml`.

## Configure a pool

Install the same package revision on the coordinator and workers. The coordinator needs this package, MCP and confirmed scientific identities; `setup-pool` records matching remote weight and native-source hashes. Workers need the scientific CPU environment, native implementation, approved checkpoints and their local PubChem index/cache. Checkpoints and native HDF5 outputs are not uploaded on every request.

Create a private JSON file containing a mapping like this, replacing every placeholder:

```json
{
  "gpu-host": {
    "transport": "ssh",
    "host": "<ssh-destination>",
    "repo": "<absolute-checkout>",
    "python": "<server-python>",
    "job_root": "<absolute-private-job-root>",
    "backend": "local",
    "features": true,
    "feature_slots": 8,
    "postprediction_slots": 16,
    "models": ["iceberg", "glacier"],
    "max_jobs": 1,
    "mcp": {"gpu_devices": [0]},
    "feature_config": {
      "mcp": {"feature_preparation_workers": 2, "feature_scoring_workers": 1, "feature_output_workers": 1},
      "models": {"simulator": {"prediction_cache": "<absolute-worker-cache>"},
                 "retrieval": {"pubchem_formula_map": "<absolute-worker-PubChem-index>"}}
    },
    "simulator": {
      "python": "<model-python>",
      "ms_pred_dir": "<native-checkout>",
      "gen_checkpoint": "<generator>",
      "inten_checkpoint": "<intensity>",
      "checkpoint": "<glacier-checkpoint>",
      "batch_size": 128,
      "num_cpu_workers": 16,
      "num_gpu_workers": 2,
      "glacier": {
        "batch_size": 128, "num_cpu_workers": 4, "num_gpu_workers": 1,
        "num_writers": 2, "num_h5_chunks": 4, "feat_cache_size": 256, "sparse_k": 100
      }
    }
  }
}
```

Only include models and checkpoint paths actually available and confirmed. Each entry may provide its own tuned settings and different file paths. Set its `config` to an absolute host config file to load the model paths and tuning saved by setup; explicit `simulator` and `mcp` endpoint values override that file. `simulator.glacier` can also override GLACIER-specific interpreter and native checkout paths. Weight hashes, native source and package source must match the coordinator's approved request. Different checkpoint contents are rejected; incompatible servers are shown in dispatch errors. Hardware tuning is excluded from the scientific cache identity; compare spectra on representative common inputs before reusing a cache across a newly tuned configuration.

For a Slurm endpoint, use `transport: ssh`, `backend: slurm` and set the endpoint's `mcp.slurm` using the supported Slurm resource and environment options. The login host only submits and polls GPU jobs. Job storage and model assets must be available on its compute nodes. Verify that the selected CPU and GPU nodes can read, write and atomically publish files through the chosen shared path before launching a large run; a path responding on the login node can still be unavailable on a legacy compute node. For a local endpoint, use `transport: local` and absolute paths; there is no SSH hop.

Register and inspect the endpoints:

```bash
msms-structure-elucidation setup-pool --servers /path/to/private-servers.json
msms-structure-elucidation mcp --backend pool
```

`setup-pool` probes enabled endpoints before saving. With `features: true`, it checks each configured model and saves `mcp.approved_model_identities`; all eligible workers for a model must match its approved weights and source. On workers reporting `stage_backlog`, `feature_slots` bounds requests needing preparation/prediction. `postprediction_slots` independently bounds the CPU output/scoring backlog and defaults to twice `feature_slots`. Reaching either bound stops new placement; admitted work continues to completion. Older workers without stage accounting retain the total-active-request bound. Put worker-specific CPU pools, caches and scheduler resources in its host `config` or `feature_config`; explicit `simulator` and `mcp` values override that configuration. Omit the PubChem-index override only when its conventional path is available on the worker. `--no-probe` validates configuration only and cannot establish model readiness.

Setup selects this computer as the coordinator and preserves unrelated settings. To use a remote coordinator, run setup there and keep the client's SSH MCP entry pointing there. Keep the source JSON private. Register MCP with `mcp-config --write`, reconnect, and call `server_info`. `mcp.feature_placement: workers` selects whole-request placement. The optional `coordinator` mode supports GPU-group endpoints with CPU preparation and scoring configured on the coordinator; it requires the coordinator's scientific assets. `coordinator_backend` applies to that mode and draining existing staged jobs.

## CPU preparation and GPU groups

During setup, start each worker at 2 preparation CPU services : 1 GPU slot : 0.5 scoring CPU services : 1 output/cache CPU service, rounding each stage up. Save explicit `feature_preparation_workers`, `feature_scoring_workers` and `feature_output_workers` per worker, then tune within CPU/RAM and scheduler budgets. `max_cpu_jobs` defaults to their automatic sum. GPU slots count configured concurrent capacity; native ICEBERG/GLACIER worker processes are separate.

The selected worker validates candidates, resolves per-molecule precursor/energy conditions, checks its shared raw cache, and prepares only missing predictions. It balances unknowns over CPU lists and groups compatible candidate work into its GPU queue. GLACIER receives native prepared entries; ICEBERG receives prepared molecular identities and condition rows. Native featurization still runs within the predictors, and ICEBERG's generated fragment graphs depend on its generator. After the native HDF5 writers close, the GPU allocation ends and separate CPU output services decode and cache predictions. Scoring services score each unknown and prepare its viewer structures while reserved preparation services keep feeding GPU jobs. Native HDF5 stays on that worker.

The feature pipeline targets `mcp.prediction_group_spectra: 131072` unique structure/actual-energy pairs per bulk GPU job, with `prediction_group_min_spectra: 100001`. A CPU worker assembles and checksums the native manifest before GPU admission. Compatible conditions can share a batch even when their energy sets differ: every structure retains exactly its requested energies. Weights, native implementation, adduct, instrument, cache namespace and runtime must agree. Overlapping unknowns share predictions while retaining independent scores.

Small workloads run promptly below the bulk threshold. Larger ready pools wait for compatible preparation to grow; an unchanged pool flushes after `prediction_coalesce_seconds` (60 by default), with a maximum collection wait of `prediction_coalesce_max_seconds` (300). Once outstanding preparation finishes, the genuine remainder runs immediately. These bounds prevent low-volume work from waiting for nonexistent spectra. Counts exclude cached or duplicate pairs; work is never padded. `max_group_candidates` separately bounds the number of structures. Size CPU output memory and wall time for decoding the full bulk manifest.

`feature_prediction_groups: auto` admits two groups per GPU slot, including CPU assembly, GPU waiting and output publication. A local worker feeds one shared queue consumed by its GPUs; Slurm workers submit to Slurm. A model process loads once per group and handles many CUDA minibatches. It exits at group completion. Larger groups amortize startup and writer drain, but increase retry cost and time to a complete result. CPU output publication validates exact per-structure energy coverage before adding records to the shared cache.

The per-request compatibility path uses `prediction_group_size: auto`: saved model `spectra_per_second` times `target_prediction_seconds` (600), divided by energy count. When `prediction_group_spectra` is configured, its per-energy structure count is also a lower sizing goal before the RAM and `max_group_candidates` caps. Without either hint the fallback is 4096 structures. Explicit integer sizes override that compatibility path. `prepared_groups` bounds its in-flight groups. The feature pipeline uses the spectrum-count settings above instead.

Tune the actual allocated GPU during setup. ICEBERG starts at two parallel GPU workers per GPU and compares three and four within their combined memory budget. GLACIER keeps one process per GPU and starts at batch 128; larger memory permits testing 256, but does not guarantee it will be faster. Choose throughput with memory headroom, not utilization percentage alone. Use the same representative unique-structure cohort, weights and conditions for every trial. Save the measured rate with its model profile; report whole-invocation and sustained native inference measurements separately.

## Availability and ownership

`server_info` reports endpoint CPU/RAM and GPU observations. Local GPU scheduling checks compute processes and free memory again under its scheduler lock before launch. Slurm reports node CPU allocation/state and GPU GRES plus queue state. Kubernetes reports node capacity, namespace quotas and pod requests; its scheduler owns admission. Allocatable GPU count is not an idle-GPU count. Cluster capacity across namespaces, affinity, taints, reservations and quotas can still keep a submitted job pending.

Whole-request placement considers worker CPU/RAM, available GPUs, active feature counts, `feature_slots`, model eligibility and configured priority. Formula and overlapping precursor-mass affinity keep likely shared candidates on one worker. Prediction caches are worker-local; there is no global cache copy or automatic migration of assigned work. Workers enforce their own CPU limits and GPU queues. `max_jobs: auto` uses the local worker's detected GPU slot limit; scheduler workers default to one queued allocation unless configured explicitly. Only configured hosts are contacted. Inventory refresh uses `poll_seconds`; observations do not promise optimal queue-time prediction.

Execution writes, status/control reads and artifact transfers have separate bounded queues and executors. Submissions, cleanup acknowledgements, cancellation and external-scheduler updates share the write queue. In shared mode, `mcp.pool_submission_workers` defaults to 2, `pool_rpc_workers` to 4 and `pool_transfer_workers` to 2. Full polling queues and retained polling replies cannot consume write capacity; a slow submission also leaves status monitoring available. Each queue retains its own FIFO waiting list. For a colocated worker with `transport: local`, ordinary nonterminal status reads use its configured job root directly. One feature per endpoint retains RPC health polling, and terminal results still require the worker protocol, final manifests and verified collection. The normal MCP launcher pumps the backend in a background thread while the server is alive. A durable remote supervisor can keep that owner alive across client disconnections.

An endpoint's `prefetch_jobs` bounds additional assigned groups staged ahead of execution. `auto` stages up to one extra group per local GPU execution slot and adds no extra scheduler submissions for Slurm/Kubernetes. An explicit nonnegative integer overrides this bound. The endpoint's own GPU concurrency and availability checks still control execution. Thus eight execution slots can have eight additional groups staged, without reserving sixteen GPUs. `prepared_groups` separately bounds each parent's production of groups; increasing endpoint prefetch cannot create missing CPU work. A group starts a fresh native model process; prefetch does not keep model weights resident. For a synchronous embedding, `mcp.pool_async_dispatch: false` restores blocking control operations and disables automatic local prefetch.

The owning MCP process dispatches network requests. Scientific CPU workers publish work into the shared job store, including when CPU preparation runs on Slurm; they do not need SSH or Kubernetes credentials. Keep the MCP coordinator running while pool work is pending. Remote jobs and staged outputs survive a client disconnection; reconnect the coordinator to continue dispatch and collection.

An assignment is persisted before network submission. Reconnecting retries the same endpoint with the same submission token. A lost reply does not dispatch a duplicate to another host. A new attempt is distinct from reconnecting to an existing failed job. After assignment, an unreachable endpoint retains ownership until recovery or explicit cancellation. This favors correct single execution over speculative duplicate jobs. Parent cancellation is routed to its assigned children. If a host is unreachable, its child remains marked as awaiting cancellation confirmation; reconnecting retries cancellation.

For a versioned rollout, keep old workers and endpoint revisions available until their requests finish. Endpoint aliases serving the same hardware can share a `resource_group`; configure matching `max_jobs` and prefetch bounds across those aliases so the group has one combined admission budget. Every request still validates the exact adapter, native source and weight hashes against its selected revision. The job index discovers children published by already-running legacy parents without scanning closed history on each tick. Set a stable hexadecimal `mcp.coordinator_identity` when the owner can move between login hosts. An MCP client attached to a separate persistent owner can use `mcp.dispatcher_role: observer` or `MSMS_MCP_ROLE=observer`: it submits CPU parents and records GPU cancellation requests while the owner dispatches predictions.

Run a persistent dispatcher and its supervisor on a stable, explicitly configured control host. An SSH GPU server can host them while Slurm remains a worker reached through SSH. Keep control state on persistent storage and use an authenticated connection available from that control host. A login-node session and a client connection are separate from the persistent owner. Before moving an owner, verify its lease is free, preserve the coordinator identity, pending submission tokens, scientific contracts and collection receipts, then update the client route.

Set `pool_rpc_workers_per_server` to isolate control requests by `resource_group`; a stalled server then cannot occupy another server's RPC workers. Each group also has an independent submission queue with `pool_submission_workers_per_server` workers, default 1. Artifact transfers retain their separate pool. A value of `0` for `pool_rpc_workers_per_server` uses the shared control and submission executors. Local endpoints honor their configured working directory and command prefix, so a colocated coordinator can still use an isolated worker environment. Whole-request placement reads assignment counts once per pass and interleaves submissions, oldest-due polls and new placements. This keeps large request windows from imposing quadratic history reads or repeatedly polling the earliest features ahead of later requests.

Check both space and file-count quotas during setup and monitoring. Retain shared prediction caches and the receipts needed to reproduce scores. Remove completed native intermediates after validated cache publication; retire redundant result copies only after checksum-acknowledged collection. Archive closed-job provenance compactly instead of retaining per-candidate temporary files or empty obsolete mutex files.

Budget result delivery separately from execution. Finished requests awaiting publication or cleanup should not occupy the execution intake window; retain a separate finite output-backlog limit for disk and CPU headroom. Show both counts in the coordinator's status. For large campaigns, coalesce atomic history writes while retaining claim markers until the accepted rows are durable.

## Collection, reuse and cleanup

Final feature results, reports, precomputed viewer structures, logs and stage receipts are streamed to the coordinator in a tar archive. Checkpoints and shared prediction caches remain on the worker; native HDF5 stays there until validated cache publication and can then be removed under the configured cleanup policy. Extraction accepts only declared regular files, verifies every size and SHA256 hash, and atomically publishes the collection. The coordinator records a receipt before requesting remote feature-directory cleanup. Failed transfers retain remote outputs; cleanup errors remain visible and are retried from the saved acknowledgement. Failed or cancelled runs are retained for diagnosis.

For Kubernetes, require Job `Complete`, Pod `Succeeded`, exit code zero and a runner result. Archive scheduler records and logs before deleting a completed test or production Job. Cleanup never deletes a model checkout, checkpoint, shared prediction cache, existing PVC or arbitrary remote directory. Endpoint cleanup deletes only the acknowledged job directory. The staged adapters use node-local scratch and remove successful temporary native output directories after publication.

Each worker's raw prediction cache uses structure, energies, adduct, instrument, model/checkpoint/source identity and scientific output settings. Concurrent unknowns with overlapping candidates on that worker share prediction ownership; each unknown keeps its own experimental scores and evidence review. Sharing a formula alone is insufficient if acquisition conditions differ. Retain worker caches and accepted final results. Existing coordinator-prepared requests can drain through `feature_placement: coordinator` compatibility without moving their cache or repeating predictions.

Collection ends at the **coordinator**. Run it on the user's computer when automatic local collection is needed. When MCP itself runs over SSH on a remote coordinator, the agent must copy the final reports/results to the user's computer with the configured SSH transfer before claiming local delivery. A remote MCP server cannot write into an arbitrary client filesystem by itself.

## Kubernetes endpoints

Use a pinned runtime image containing compatible model dependencies and the package, or an explicitly configured bootstrap command prefix that installs nothing privileged. Model checkouts and approved weights must already be staged in persistent storage. Do not bake licensed weights or credentials into a public image.

A pool endpoint uses `transport: kubernetes`, `backend: kubernetes`, an absolute `job_root` on a persistent volume, and:

- `gateway`: explicit `context`, `namespace`, `pod`, and optional container name (default `gateway`). This is a bounded CPU workspace pod with the package installed and the same storage paths mounted as GPU jobs.
- `mcp.kubernetes`: explicit `context`, `namespace`, `image`, `shared_workspace: true`, `volumes`, `volume_mounts`, CPU/RAM `resources`, `deadline_seconds`, GPU `gpu_resource` and count, and optional `nodeSelector`, `affinity`, `tolerations`, `env`, `working_dir` or `command_prefix`.
- `simulator`: paths inside runner pods and the independently tuned model settings, as above.

The coordinator invokes `kubectl` with explicit context and namespace on every operation; authentication remains on that computer. The gateway transfers files and manages staged job records through `kubectl exec`; it needs no Kubernetes API credentials or permission to submit Jobs. The worker publishes bounded CPU preparation/output Jobs and GPU prediction Jobs on shared storage; the coordinator relays their manifests, confirms completion and archives scheduler records/logs before deletion. CPU Jobs request no GPUs. Renew or replace an expired gateway through setup without deleting persistent data. Check live quotas and GPU-product affinity before submission. Delete only confirmed, archived objects by exact name.

For `mcp.backend: kubernetes` without a pool, `mcp.kubernetes.python` selects the runner interpreter inside the image (default `python`). The owning MCP process submits and confirms child jobs; CPU pods publish their requests on shared storage without API credentials. The coordinator itself must see the persistent job root at the same path as runner pods. A pool's gateway transport supplies the file-transfer route when the user's computer does not mount that storage.

See the Kubernetes documentation for [GPU device resources](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/) and [resource quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/).
