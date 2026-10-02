---
name: msms-setup
description: Install these skills, find or ask for the prediction server (this machine, an ssh GPU host, or a Slurm cluster), set up its model environments, tune ms-pred inference into configs/local.yaml, and register the MCP server. Use when asked to install or set up the skills, or when the model host changes.
---
# Install, find a prediction server, tune

Use this skill when:
- the user asks to install the skills (for example "Install the skill for me: https://github.com/coleygroup/msms-structure-elucidation.skills"),
- `configs/local.yaml` is missing or was tuned on another host,
- or no prediction server is connected.

Installation needs no spectrum and no collision-energy information. Never write a host name, partition, GPU type, account or path into code, skills or `configs/default.yaml`. Every such value goes into the uncommitted `configs/local.yaml` on the host it describes.

1. **Get the checkout.** Clone the repository if it is not already present, or reuse an existing checkout. Codex reads `.agents/skills` and Claude Code reads `.claude/skills`, which links to the same folders. For use from another project, follow the skill-installation paragraph in [docs/technical-guide.md](../../../docs/technical-guide.md).
2. **Find or ask for the prediction server.** First look for one that already exists:
   - an `msms-structure-elucidation` entry in `.mcp.json` or in the client's MCP list. If it is connected, call `server_info` and show the user its host, backend and model status.
   - an `execution` block in `configs/local.yaml`.

   If one is found and the user confirms it, skip to step 6 and fill only the gaps `server_info` reports. Otherwise ask once where the models should run:
   - **This machine.**
   - **A GPU host over ssh.** Collect the ssh destination (`user@host` or a `~/.ssh/config` alias) and the path of this repository there. If the checkout is not there yet, ask before cloning or installing anything remotely. Login must work without a prompt, because setup uses `BatchMode=yes`. If the host needs a password or a second factor, ask the user to open a persistent connection first (`ControlMaster`/`ControlPersist`), for example by running `ssh <host>` in another terminal.
   - **A Slurm cluster.** Collect the login-node ssh destination and the checkout path, as for an ssh host. Prefer a project or scratch file system when home quota is small. Jobs run on compute nodes, and the login node only submits and polls them.
3. **Set up the environment** on that host.
   - Look for:
     - an ms-pred checkout, and an interpreter that imports `ms_pred`, `msbuddy` and `rdkit`;
     - ICEBERG generator and intensity checkpoints;
     - optionally a GLACIER checkpoint;
     - for de novo work, a FRIGID checkout, its interpreter, and MIST and DLM checkpoints;
     - a GPU (`nvidia-smi`, or `sinfo` on a cluster).
   - For anything missing, follow the ms-pred README **Install & setup** section and `msms-denovo/scripts/setup_env.sh`. `bash setup_envs.sh` is only a CPU venv helper. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym). Never download or distribute NIST-trained weights; use them only if the user supplies them.
   - Create the server venv from any Python 3.10 or newer, without modifying the user's model environments:

     ```bash
     python3 -m venv .cache/mcp-venv && .cache/mcp-venv/bin/pip install -e '.[mcp]'
     ```
4. **Probe and tune.** Pass every interpreter, checkout and checkpoint found in step 3, so that setup saves them.

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
   3. Rerun with those values. Setup saves them under `mcp.slurm`, sets `mcp.backend: slurm`, and runs the probe and ICEBERG benchmark inside a submitted job, so the compute node is measured rather than the login node.

   ```bash
   msms-structure-elucidation setup --remote <login-destination> --remote-repo <remote-repo> \
     --remote-python .cache/mcp-venv/bin/python --scheduler slurm
   msms-structure-elucidation setup --remote <login-destination> --remote-repo <remote-repo> \
     --remote-python .cache/mcp-venv/bin/python --scheduler slurm --slurm-partition <partition> \
     --slurm-gpus <gpu-request> --slurm-cpus <n> --slurm-mem <mem> --slurm-time <hh:mm:ss> \
     --slurm-setup '<environment activation>' --ms-pred-python <ms-pred-python> --ms-pred-dir <ms-pred> ...
   ```

   Setup reads CPU threads, RAM, and the GPUs and their free memory. It picks starting settings from the measured anchors in `msms-sim-iceberg` (**Inference configuration anchors**). If a GPU, ms-pred and ICEBERG checkpoints are all available, it runs an untimed warm-up and then times ICEBERG on a fixed set of benchmark molecules. It halves `batch_size` until peak GPU memory is at most 80% and the run succeeds, then doubles while throughput improves by at least 5%. Each trial takes one to a few minutes. `--no-benchmark` saves the heuristic settings only.
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
   - what is ready and what is missing (environment, checkpoints, GPU);
   - the saved settings, and whether they are `benchmarked` or `heuristic`;
   - what to add to get a benchmark or to enable a missing model.

## Using the prediction server

With the MCP server connected, call its tools: `predict_spectra`, `score_candidates`, `retrieve_atlas`, `generate_structures_frigid` and `predict_fingerprint_mist`. Pass spectra as `.ms` text, so no files need copying between machines. Confirm NCE or eV with the user before any call.

Long jobs, and every job on Slurm, return a `job_id`. Poll it with `get_job(job_id, wait_seconds=...)`. A job keeps running if the client disconnects.

Without MCP, run CLI commands on the host instead: `ssh <host> '<prefix> cd <repo> && <python> -m msms_structure_elucidation.cli …'`. Copy inputs over and results back with `scp` or `rsync`, then open `msms-visualize` locally.

Rerun setup after changing GPUs, drivers, checkpoints, the ms-pred version, or Slurm resources. The benchmark measures ICEBERG. GLACIER uses the same `batch_size` and CPU workers, and always one GPU worker.
