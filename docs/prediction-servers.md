# Prediction servers

An MCP coordinator prepares structures and acquisition conditions on CPUs, dispatches raw predictions, collects verified artifacts, and scores each experimental spectrum. It can use one local GPU host, a host reached over SSH, Slurm, Kubernetes, or several configured prediction endpoints together. SSH is a transport; the endpoint's backend can be local GPU execution or Slurm.

Use `msms-setup` to find compatible model environments and confirm the selected model and exact weights. Keep all real destinations, partitions, namespaces, images, storage names, paths and credentials out of the public repository. Endpoint settings live under `mcp.servers` in ignored `configs/local.yaml`.

## Configure a pool

Install the same package revision on the coordinator and endpoints. A coordinator needs the scientific CPU environment, native implementation and approved checkpoint identities used for validation, cache decoding and scoring. These CPU assets are not automatically installed by configuring a pool. GPU endpoints need their model runtime and checkpoints; checkpoints are not uploaded on every request.

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
    "models": ["iceberg", "glacier"],
    "max_jobs": 1,
    "mcp": {"gpu_devices": [0]},
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

Only include models and checkpoint paths actually available and confirmed. Each entry may provide its own tuned settings and different file paths. Set its `config` to an absolute host config file to load the model paths and tuning saved by setup; explicit `simulator` and `mcp` endpoint values override that file. `simulator.glacier` can also override GLACIER-specific interpreter and native checkout paths. Weight hashes, native source and package source must match the coordinator's approved request. Different checkpoint contents are rejected; incompatible servers are shown in dispatch errors. Changing hardware tuning does not make a different scientific prediction cache.

For a Slurm endpoint, use `transport: ssh`, `backend: slurm` and set the endpoint's `mcp.slurm` using the supported Slurm resource and environment options. The login host only submits and polls GPU jobs. Job storage and model assets must be available on its compute nodes. Verify that the selected CPU and GPU nodes can read, write and atomically publish files through the chosen shared path before launching a large run; a path responding on the login node can still be unavailable on a legacy compute node. For a local endpoint, use `transport: local` and absolute paths; there is no SSH hop.

Register and inspect the endpoints:

```bash
msms-structure-elucidation setup-pool --servers /path/to/private-servers.json
msms-structure-elucidation mcp --backend pool
```

`setup-pool` probes enabled endpoints before saving; `--no-probe` validates the transport configuration only. It selects this computer as the MCP coordinator and preserves unrelated local settings. To use a remote coordinator, run this command on that host and keep the client's SSH MCP entry pointing there. Keep the source JSON private as well. Register MCP from the coordinator with `mcp-config --write`, reconnect, and call `server_info`. To place CPU orchestration on Slurm, set `mcp.coordinator_backend: slurm` and configure the coordinator's `mcp.slurm.cpu`; otherwise CPU work runs on the coordinator's local CPU queue.

## CPU preparation and GPU groups

The coordinator validates candidates, resolves per-molecule precursor/energy conditions, checks the shared raw cache, and prepares only missing predictions. GLACIER receives native prepared entries; ICEBERG receives prepared molecular identities and condition rows. Native featurization still runs within the predictors, and ICEBERG's generated fragment graphs depend on its generator. Fully precomputing those generated graphs before model inference is not possible.

With `mcp.prediction_group_size: auto`, group size is approximately the selected model's saved `spectra_per_second` multiplied by `target_prediction_seconds`, divided by the number of energies. The default target is 600 seconds. The estimate falls back to 4096 candidates without a measured rate and is capped by `max_group_candidates`. Automatic groups also use a conservative allowance of 20% of currently available coordinator RAM for decoded peaks and cache processing; container limits are respected where cgroup v2 exposes them. It is a scheduling hint: molecule complexity, transfer time and endpoint speed can change actual runtime. Explicit request shard sizes and configured integer group sizes take precedence. Small requests start immediately; there is no ten-minute collection delay.

`prepared_groups` bounds the number of in-flight groups per orchestrator. Its default `auto` prepares two groups per endpoint slot, capped at eight; an integer sets an explicit bound. Multiple groups can run on different endpoints while another is prepared or decoded. A model process loads once per group and handles many normal CUDA minibatches. It exits at group completion; there is no permanently resident model service between groups. Larger groups amortize startup and writer drain, but increase retry cost and time to a complete result. Native output groups are decoded separately to avoid collisions between internal candidate names.

Tune the actual allocated GPU during setup. ICEBERG starts at two parallel GPU workers per GPU and compares three and four within their combined memory budget. GLACIER keeps one process per GPU and starts at batch 128; larger memory permits testing 256, but does not guarantee it will be faster. Choose throughput with memory headroom, not utilization percentage alone. Use the same representative unique-structure cohort, weights and conditions for every trial. Save the measured rate with its model profile; report whole-invocation and sustained native inference measurements separately.

## Availability and ownership

`server_info` reports endpoint CPU/RAM and GPU observations. Local GPU scheduling checks compute processes and free memory again under its scheduler lock before launch. Slurm reports node CPU allocation/state and GPU GRES plus queue state. Kubernetes reports node capacity, namespace quotas and pod requests; its scheduler owns admission. Allocatable GPU count is not an idle-GPU count. Cluster capacity across namespaces, affinity, taints, reservations and quotas can still keep a submitted job pending.

The pool prefers observed idle GPUs, then bounded scheduler queues, subject to each endpoint's `max_jobs`, model eligibility and configured priority. `max_jobs: auto` uses the local endpoint's detected GPU slot limit; scheduler endpoints default to one queued allocation unless an explicit limit is supplied. It never probes or dispatches to unconfigured hosts. Inventory refresh uses `poll_seconds`. It does not promise optimal queue-time prediction or automatic cross-cluster migration of an already assigned job.

The owning MCP process dispatches network requests. Scientific CPU workers publish work into the shared job store, including when CPU preparation runs on Slurm; they do not need SSH or Kubernetes credentials. Keep the MCP coordinator running while pool work is pending. Remote jobs and staged outputs survive a client disconnection; reconnect the coordinator to continue dispatch and collection.

An assignment is persisted before network submission. Reconnecting retries the same endpoint with the same submission token. A lost reply does not dispatch a duplicate to another host. A new attempt is distinct from reconnecting to an existing failed job. After assignment, an unreachable endpoint retains ownership until recovery or explicit cancellation. This favors correct single execution over speculative duplicate jobs. Parent cancellation is routed to its assigned children. If a host is unreachable, its child remains marked as awaiting cancellation confirmation; reconnecting retries cancellation.

## Collection, reuse and cleanup

Completed native output files and logs are streamed back to the coordinator in a tar archive. Extraction accepts only declared regular files, verifies every size and SHA256 hash, and atomically publishes the collection. The coordinator records a receipt before requesting remote cleanup. Failed transfers retain their remote outputs; cleanup errors remain visible in job status and are retried from the saved acknowledgement. Failed or cancelled runs are retained for diagnosis.

For Kubernetes, require Job `Complete`, Pod `Succeeded`, exit code zero and a runner result. Archive scheduler records and logs before deleting a completed test or production Job. Cleanup never deletes a model checkout, checkpoint, shared prediction cache, existing PVC or arbitrary remote directory. Endpoint cleanup deletes only the acknowledged job directory. The staged adapters use node-local scratch and remove successful temporary native output directories after publication.

The coordinator's raw prediction cache uses structure, energies, adduct, instrument, model/checkpoint/source identity and scientific output settings. Concurrent unknowns with overlapping candidates wait for the same cache owner; each unknown keeps its own experimental scores and evidence review. Sharing a formula alone is insufficient if acquisition conditions differ. Keep the coordinator's cache and accepted final results; deleting those would force recomputation.

Collection ends at the **coordinator**. Run it on the user's computer when automatic local collection is needed. When MCP itself runs over SSH on a remote coordinator, the agent must copy the final reports/results to the user's computer with the configured SSH transfer before claiming local delivery. A remote MCP server cannot write into an arbitrary client filesystem by itself.

## Kubernetes endpoints

Use a pinned runtime image containing compatible model dependencies and the package, or an explicitly configured bootstrap command prefix that installs nothing privileged. Model checkouts and approved weights must already be staged in persistent storage. Do not bake licensed weights or credentials into a public image.

A pool endpoint uses `transport: kubernetes`, `backend: kubernetes`, an absolute `job_root` on a persistent volume, and:

- `gateway`: explicit `context`, `namespace`, `pod`, and optional container name (default `gateway`). This is a bounded CPU workspace pod with the package installed and the same storage paths mounted as GPU jobs.
- `mcp.kubernetes`: explicit `context`, `namespace`, `image`, `shared_workspace: true`, `volumes`, `volume_mounts`, CPU/RAM `resources`, `deadline_seconds`, GPU `gpu_resource` and count, and optional `nodeSelector`, `affinity`, `tolerations`, `env`, `working_dir` or `command_prefix`.
- `simulator`: paths inside runner pods and the independently tuned model settings, as above.

The coordinator invokes `kubectl` with explicit context and namespace on every operation; authentication remains on that computer. The gateway transfers files and manages staged job records through `kubectl exec`; it needs no Kubernetes API credentials or permission to submit Jobs. The coordinator submits and monitors finite GPU Jobs. Renew or replace an expired gateway through setup without deleting its persistent data. Use appropriate resources and GPU-product affinity, then check live quotas before submission. Test objects must finish, be archived and be deleted by exact name.

For `mcp.backend: kubernetes` without a pool, `mcp.kubernetes.python` selects the runner interpreter inside the image (default `python`). The owning MCP process submits and confirms child jobs; CPU pods publish their requests on shared storage without API credentials. The coordinator itself must see the persistent job root at the same path as runner pods. A pool's gateway transport supplies the file-transfer route when the user's computer does not mount that storage.

See the Kubernetes documentation for [GPU device resources](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/) and [resource quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/).
