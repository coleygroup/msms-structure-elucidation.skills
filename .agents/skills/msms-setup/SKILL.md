---
name: msms-setup
description: Install these skills, configure local, SSH, Slurm or Kubernetes prediction servers, tune each model host in configs/local.yaml, and register the MCP server. Use when asked to install or set up the skills, or when model hosts change.
---
# Install, find a prediction server, tune

Use this skill when:
- the user asks to install the skills (for example "Install the skill for me: https://github.com/coleygroup/msms-structure-elucidation.skills"),
- `configs/local.yaml` is missing or was tuned on another host,
- or no prediction server is connected.

Use the GLACIER starting profile in `models.simulator.glacier`: batch size 128, 4 CPU workers per process, 1 GPU worker and 1 prediction process per GPU, 2 HDF5 writers, 4 HDF5 chunks, feature cache 256 and 100 sparse peaks. Reuse a matching measured host profile; batch 128 may already provide high utilization. Increase it only when measured throughput improves within memory limits. For GLACIER, check that the selected checkout’s native `predict_smis_joint.py` exposes `--num-writers`, `--num-h5-chunks` and `--feat-cache-size`; report an incompatible runtime during setup rather than silently dropping settings. Do not modify a supplied checkout to bypass that check. Keep GLACIER settings separate from ICEBERG’s tuned profile and save machine-specific overrides in the local config. Keep shared raw prediction reuse enabled on the model host.

Read [saved pipeline answers](../../../docs/pipeline-preferences.md) before asking setup questions. Reuse confirmed hosts, backends, model selections and exact weight identities while their context matches; ask only for gaps or changes and save new answers. Installation needs no spectrum and no collision-energy information. Never write a host name, partition, GPU type, account or path into code, skills or `configs/default.yaml`. Every such value goes into the uncommitted `configs/local.yaml` on the host it describes.
For an analysis without a supplied formula, follow [formula search](../../../docs/formula-search.md). Recommend BUDDY’s top three and reuse a matching user choice or ask once between that and mass filtering. With MCP, use `infer_formulas` after BUDDY is selected; with GLACIER, pass the selected hypotheses to `search_pubchem_formulas`. Use `search_pubchem_mass` only for the selected mass-filtering route. Keep the choice and its input context in the local preferences.


1. **Get the checkout.** Clone the repository if it is not already present, or reuse an existing checkout. Codex reads `.agents/skills` and Claude Code reads `.claude/skills`, which links to the same folders. For use from another project, follow the skill-installation paragraph in [docs/technical-guide.md](../../../docs/technical-guide.md).
   Install the [workflow completion hooks](../../../docs/workflow-hooks.md) in the **client project**, even when the model host is remote. Install this package in the client environment, ensure `msms-workflow-guard` is available on the client's PATH, and run `msms-workflow-guard install --project-dir <client-project>`. Preserve unrelated client settings. Tell the user to review/trust the hooks in Codex (`/hooks`) or Claude Code and reload if needed; writing the definitions does not activate them in the current session. Keep `AGENTS.md` as the shared entry point. Setup alone does not register a scientific analysis.
2. **Find or ask for the prediction server.** First look for one that already exists:
   - an `msms-structure-elucidation` entry in `.mcp.json` or in the client's MCP list. If it is connected, call `server_info` and show the user its host, backend and model status.
   - an `execution` block in `configs/local.yaml`.

   If one is found and its host, intended prediction model and exact weights match a saved user confirmation (or the user confirms them now), skip to step 6 and fill only the gaps `server_info` reports. Apply the model-specific atlas policy in step 3. Otherwise ask once where the models should run:
   - **This machine.**
   - **Several servers or Kubernetes.** Follow [prediction server pools](../../../docs/prediction-servers.md). Tune each endpoint independently, verify matching native source and exact weight hashes, then register the named endpoints with `setup-pool`. Kubernetes needs an explicit context and namespace, a compatible pinned runtime image, persistent storage, and a bounded CPU gateway for file transfers. Keep credentials on the coordinator. Inventory is advisory; cluster schedulers allocate resources. Save private endpoint settings in ignored local configuration.
   - **A GPU host over ssh.** Collect the ssh destination (`user@host` or a `~/.ssh/config` alias) and the path of this repository there. If the checkout is not there yet, ask before cloning or installing anything remotely. Login must work without a prompt, because setup uses `BatchMode=yes`. If the host needs a password or a second factor, ask the user to open a persistent connection first (`ControlMaster`/`ControlPersist`), for example by running `ssh <host>` in another terminal.
   - **A Slurm cluster.** Collect the login-node ssh destination and the checkout path, as for an ssh host. Prefer a project or scratch file system when home quota is small. Jobs run on compute nodes, and the login node only submits and polls them.
3. **Set up the environment** on that host.
   - Look for:
     - an [ms-pred checkout](https://github.com/coleygroup/ms-pred), and an interpreter that imports `ms_pred`, `msbuddy` and `rdkit`;
     - either a complete ICEBERG generator and intensity checkpoint pair, or a GLACIER checkpoint; either model is sufficient;
     - for de novo work, a FRIGID checkout, its interpreter, and MIST and DLM checkpoints;
     - a GPU (`nvidia-smi`, or `sinfo` on a cluster).
   - Show the user the available model(s), exact checkpoint paths, and weight provenance when known. Reuse a saved model selection when its host and weight identities match. If no matching selection exists and only one model has complete weights, propose using it; if both do, ask which to use. Before benchmarking or prediction, reuse a matching `model_selection` confirmation, or ask once and save it with the host, model and exact checkpoint identities in its context. Use the confirmed selection; do not require or download the other model's weights.
   - Use the confirmed model throughout prediction and scoring. For **ICEBERG**, prefer public atlas spectra with matching adduct and collision energies unless the user requests fresh predictions throughout. The atlas uses ICEBERG 2.1 trained on NIST'23. If local weights differ, or matching weights have not been verified, tell the user that mixing their predictions may affect ranking comparability; prefer atlas spectra and identify the checkpoint for local predictions. Predict missing spectra with the confirmed local ICEBERG weights. For **GLACIER**, no open GLACIER atlas is currently available: predict every candidate spectrum with the confirmed GLACIER weights. Do not use public ICEBERG atlas spectra or atlas retrieval scores as GLACIER predictions.
   - If no ms-pred checkout exists, clone it on the model host with `git clone https://github.com/coleygroup/ms-pred.git` (for remote hosts, obtain permission as described in step 2). For anything missing, follow the checkout's `README.md` **Install & setup** section and `msms-denovo/scripts/setup_env.sh`. `bash setup_envs.sh` is only a CPU venv helper. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym). Never download or distribute NIST-trained weights; use them only if the user supplies them.
   - Create the server venv from any Python 3.10 or newer, without modifying the user's model environments:

     ```bash
     python3 -m venv .cache/mcp-venv && .cache/mcp-venv/bin/pip install -e '.[mcp]'
     ```
4. **Probe and tune.** Pass the interpreters, checkouts and checkpoints confirmed in step 3, so setup saves them. The example below uses ICEBERG. For GLACIER, use `--model glacier --checkpoint <confirmed-checkpoint>` and omit the ICEBERG checkpoint flags. Setup tunes GLACIER batch size independently and keeps one GPU process per GPU. Pass `--benchmark-smiles <cohort.json>` for a representative fixed SMILES cohort large enough for many batches. Setup saves the selected model and checkpoint paths in the host's `configs/local.yaml`. Pass the confirmed model explicitly to prediction and scoring tools.

   This machine:

   ```bash
   msms-structure-elucidation setup --ms-pred-python /path/to/python --ms-pred-dir /path/to/ms-pred \
     --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt \
     --frigid-python /path/to/frigid/python --frigid-dir /path/to/FRIGID \
     --mist-checkpoint /path/to/mist.ckpt --dlm-checkpoint /path/to/dlm.ckpt
   ```

   An ssh host. Paths are remote paths, and `--remote-python` is the server venv there:

   ```bash
   msms-structure-elucidation setup --remote <ssh-destination> --remote-repo <remote-repo> \
     --remote-python .cache/mcp-venv/bin/python --ms-pred-python <ms-pred-python> --ms-pred-dir <ms-pred> ...
   ```

   A Slurm cluster runs in two passes through the login node:
   1. `--scheduler slurm` without a partition lists the partitions, their GPU gres, time limits and node sizes. It saves nothing.
   2. Show these to the user and agree on the partition, the GPU request (`--slurm-gpus`), CPUs and memory, the time limit, the account or QoS if required, `--slurm-requeue` for preemptible partitions, and the shell lines that prepare the job environment (`--slurm-setup`, repeatable, such as module loads or environment activation).
   3. Rerun with those values. Setup saves them under `mcp.slurm`, sets `mcp.backend: slurm`, and runs the probe and selected model's benchmark inside a submitted job, so the compute node is measured rather than the login node.

   Before a large run, use a bounded allocation to verify that the selected CPU and GPU nodes can read the checkout/checkpoints and read, write and rename files in the configured job root. A responsive login-node path does not establish compute-node accessibility. Prefer a verified shared path and node-local scratch for native intermediate writes. Confirm scheduler completion and exit code zero, rather than treating an allocated node or runner result as sufficient.

   Follow the production-route canary and admission checks in [MCP execution](../../../docs/mcp-execution.md). Validate the actual PubChem route, CPU trust store, fresh inference when required, collection and cache reuse before bulk admission. Keep TLS verification enabled. Check concurrent handoffs on shared storage. Use one supported dispatcher and current configuration per job root, and keep operational history separate from current agent instructions. Stop old dispatchers before upgrading; preserve scientific requests, caches, checkpoints and partial results.

   ```bash
   msms-structure-elucidation setup --remote <login-destination> --remote-repo <remote-repo> \
     --remote-python .cache/mcp-venv/bin/python --scheduler slurm
   msms-structure-elucidation setup --remote <login-destination> --remote-repo <remote-repo> \
     --remote-python .cache/mcp-venv/bin/python --scheduler slurm --slurm-partition <partition> \
     --slurm-gpus <gpu-request> --slurm-cpus <n> --slurm-mem <mem> --slurm-time <hh:mm:ss> \
     --slurm-setup '<environment activation>' --ms-pred-python <ms-pred-python> --ms-pred-dir <ms-pred> ...
   ```

   Setup reads CPU threads, RAM, and the GPUs and their free memory. It picks model-specific starting settings from `msms-predict` (**Inference configuration anchors**). With a GPU, compatible ms-pred and confirmed weights, it warms up and times the selected model on a fixed cohort. It halves `batch_size` until the run succeeds with peak GPU memory at most 80%, then doubles while throughput improves by at least 5%. ICEBERG also compares 2, 3 and 4 parallel GPU workers per GPU at the selected batch size. GLACIER keeps one worker per GPU, starts at 128, and tests larger batches such as 256 for measured gains. A larger GPU does not guarantee that a larger batch is faster. `--no-benchmark` saves heuristic settings only.
5. **Where settings are saved.** Setup writes `configs/local.yaml` on the model host. Keys the user edited by hand are kept.
   - `models.simulator`: the tuned settings and the paths given.
   - `models.denovo`: the FRIGID paths.
   - `models.batch.max_workers`.
   - `mcp`: the backend, plus the Slurm settings when that backend is chosen.
   - `host_profile`.

   Every CLI command and the MCP server apply this file on top of `configs/default.yaml` unless `--config` or `MSMS_CONFIG` names another config. Remote setup also writes an `execution` block to the local `configs/local.yaml`, holding the host, repo, server python, prefix and the tuned settings. If the remote checkout is not installed yet, only a probe is recorded (`status: probed_not_installed`); rerun after installing.
6. **Register the MCP server** from the client checkout:

   ```bash
   msms-structure-elucidation mcp-config --write   # merges the entry into ./.mcp.json
   ```

   For a local server, run it with the server venv's interpreter (`.cache/mcp-venv/bin/python -m msms_structure_elucidation.cli mcp-config --write`), or pass `--python`. For a remote host, the entry runs the server through `ssh` on the model host. Ask the user to reload MCP servers in their client. Then call `server_info` and check that the expected models report `ready`.
7. **Report to the user:**
   - where models run and which backend is used;
   - the confirmed prediction model and checkpoint paths, and whether suitable public ICEBERG atlas spectra will be reused or every candidate will be predicted directly;
   - what is ready and what is missing (environment, checkpoints, GPU);
   - the saved settings, and whether they are `benchmarked` or `heuristic`;
   - what to add to get a benchmark or to enable a missing model.

## Using the prediction server

With the MCP server connected, call its tools: `predict_spectra`, `score_candidates`, `retrieve_atlas`, `search_pubchem_mass`, `generate_structures_frigid` and `predict_fingerprint_mist`. Pass spectra as `.ms` text, so no files need copying between machines. For raw/mzML inputs, first resolve energy values, NCE/eV units and stepped acquisition from their acquisition metadata using [collision-energy handling](../../../docs/collision-energy.md); reuse verified saved settings, and ask only when unresolved or conflicting.

Follow the confirmed model selection from step 3. With ICEBERG, use `retrieve_atlas` where suitable and predict missing spectra with `model="iceberg"`. For mass-based atlas retrieval, the WebUI offers **Mass ± ppm** search over stored spectra. For a [direct mass-only PubChem prediction](../../../docs/technical-guide.md#mass-only-pubchem-option), use `search_pubchem_mass` with the confirmed model, ppm tolerance and collision unit; without MCP use `run --mass-only` or `batch --mass-only`. It skips formula prediction and atlas retrieval even for ICEBERG. For other requests for fresh predictions throughout, score every candidate directly. With GLACIER, skip `retrieve_atlas` and predict or score every candidate using `predict_spectra` or `score_candidates` with `model="glacier"`; do not substitute ICEBERG atlas spectra.

Long jobs, and every job on Slurm, return a `job_id`. Keep polling with `get_job(job_id, wait_seconds=30)` while they are queued or running. Review stable per-feature outputs as they arrive while monitoring the remaining jobs; do not wait for a final batch report or stop supervision after launch. A job can outlive the client connection; reconnect and inspect its actual state rather than assuming completion. Follow [job monitoring and early review](../../../docs/job-monitoring.md).

Without MCP, run CLI commands on the host instead: `ssh <host> '<prefix> cd <repo> && <python> -m msms_structure_elucidation.cli …'`. Copy inputs over and results back with `scp` or `rsync`, then open `msms-visualize` locally.

Rerun setup and retune the selected model after changing GPUs, drivers, checkpoints, the ms-pred version, or scheduler resources. Setup benchmarks the selected model. Small calibration cohorts do not establish sustained utilization; supply a sufficiently large representative cohort and inspect native timings before drawing that conclusion.

When a workload is slow, use [phase-level performance diagnosis](../../../docs/mcp-execution.md) to check CPU preparation, cache waits and scoring before retuning GPU inference. A CPU scoring replay does not measure sustained GPU throughput; validate any optimization on fixed outputs and retain compatible raw predictions.

Tune independent CPU feature concurrency as part of host setup. Inspect `server_info.cpu_workload`, CPU utilization, queue phases and the complete CPU-job memory footprint. Keep this separate from the model's native `num_cpu_workers`. Use MCP `plan_cpu_concurrency`, or `msms-structure-elucidation plan-cpu --cpu-budget 24 --memory-budget-gb 64 --cpus-per-job 2 --memory-per-job-gb 4`, with explicit budgets after shared-host and GPU-worker headroom. For Slurm, derive budgets from the selected compute resources and account policy; for Kubernetes, inspect requests, limits and remaining quota. The planner does not probe a login host or change configuration.

Benchmark representative feature completion at increasing concurrency before saving measured `mcp.max_cpu_jobs`, `mcp.max_live_pipelines` and CPU task resources in `configs/local.yaml`. Label a capacity-only plan unmeasured. Include prediction waiters in resident memory budgets; Slurm parents retain CPU/RAM allocations while waiting. Use [CPU sizing and controlled reload](../../../docs/mcp-execution.md#size-independent-cpu-feature-jobs) when applying changes to an existing queue, preserving native jobs and request identities.

For ICEBERG, start with two parallel GPU workers per GPU and compare two to four, tuning batch size within their combined memory budget. Save the total worker count across configured GPUs in `models.simulator.num_gpu_workers`. Setup tunes batch size, then compares worker counts on the same fixed cohort. Keep the smaller configuration unless throughput improves within the memory budget.

Tune GLACIER independently on each machine based on its native `pred_smis` inference script. Measure the configured prediction path, using MCP tools when connected. Inspect the installed script's options and defaults. Start from the GLACIER profile above, keep one prediction process per GPU, and increase batch size until throughput plateaus or memory limits are reached. Keep CPU workers and writers fixed initially; adjust them when native timing shows feature or writer waits. After warm-up, use a fixed, representative candidate set large enough for many batches, keeping confirmed weights and scientific settings unchanged. Report sustained inference throughput, sampled utilization and peak GPU/host memory separately from whole-workload elapsed time and utilization. Verify all expected spectra and save measured settings in `models.simulator.glacier` in `configs/local.yaml`. Label unmeasured settings heuristic.
