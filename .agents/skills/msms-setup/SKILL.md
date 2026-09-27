---
name: msms-setup
description: Install these skills, check model environments and assets, then probe this or a remote host and save tuned ms-pred inference settings to configs/local.yaml. Use when asked to install or set up the skills, or when the GPU host changes.
---
# Install and tune

Use this skill when the user asks to install the skills (for example "Install the skill for me: https://github.com/coleygroup/msms-structure-elucidation.skills"), or when `configs/local.yaml` is missing or was tuned on another host. Installation needs no spectrum and no collision-energy information.

1. **Get the checkout.** Clone the repository if it is not already present, or reuse an existing checkout. Codex reads `.agents/skills` and Claude Code reads `.claude/skills`, which links to the same folders. For use from another project, follow the skill-installation paragraph in [docs/technical-guide.md](../../../docs/technical-guide.md).
2. **Ask where models should run.** Ask once: *this machine*, or *a remote machine*. For a remote machine, collect:
   - the ssh destination, such as `user@host` or a `~/.ssh/config` alias. Key-based login is required, because setup uses `BatchMode=yes` and cannot answer password prompts.
   - the path of this repository on that machine. If it is not there yet, ask before cloning or installing anything remotely.
   - the interpreter or environment activation. Use `--remote-python` for an interpreter path, or `--remote-prefix` for shell text such as `source ~/miniforge3/bin/activate ms-pred &&`.
   - any scheduler or container prefix, also passed through `--remote-prefix`: for example `srun --gres=gpu:1`, or `apptainer exec --nv image.sif`. Probe and benchmark must run on the node that will run the jobs, not on a login node.
3. **Check the environment** on the machine that runs the models. Look for an ms-pred checkout and an interpreter that imports `ms_pred`, `msbuddy` and `rdkit`, with this package installed (`pip install -e <repo>`). Also look for ICEBERG generator and intensity checkpoints and a GPU (`nvidia-smi`). For anything missing, follow ms-pred's README **Install & setup** section. `bash setup_envs.sh` is only a CPU venv helper. Public MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym). Never download or distribute NIST-trained weights; use them only if the user supplies them.
4. **Probe and tune.** Run setup on this machine:

   ```bash
   msms-structure-elucidation setup --ms-pred-python /path/to/python --ms-pred-dir /path/to/ms-pred \
     --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt
   ```

   Or run it on a remote machine. Paths are the remote paths:

   ```bash
   msms-structure-elucidation setup --remote gpu-box --remote-repo ~/msms-structure-elucidation.skills \
     --remote-prefix 'source ~/miniforge3/bin/activate ms-pred &&' \
     --ms-pred-dir ~/ms-pred --gen-checkpoint ~/ckpt/gen.ckpt --inten-checkpoint ~/ckpt/inten.ckpt
   ```

   Setup reads CPU threads, RAM, and the GPUs and their free memory. It picks starting settings from the measured anchors in `msms-sim-iceberg` (**Inference configuration anchors**). If a GPU, ms-pred and ICEBERG checkpoints are all available, it runs an untimed warm-up and then times ICEBERG on a fixed set of benchmark molecules. It halves batch_size until peak GPU memory is at most 80% and the run succeeds. It then doubles while throughput improves by at least 5%. Each trial takes one to a few minutes. `--no-benchmark` saves the heuristic settings only.
5. **Where settings are saved.** Local setup merges `models.simulator` (`cuda_devices`, `batch_size`, `num_cpu_workers`, `num_gpu_workers`, `shard_size`, and any paths given) and a `host_profile` into `configs/local.yaml`. Keys the user edited by hand are kept. Every CLI command applies this file on top of `configs/default.yaml` unless `--config` or `MSMS_CONFIG` names another config, and warns if the hostname has changed. Remote setup writes the tuned file in the remote checkout. The local `configs/local.yaml` gets an `execution` block with host, repo, python, prefix and the tuned settings. If the remote checkout is not installed yet, only a probe and a recommendation are recorded (`status: probed_not_installed`); rerun setup with `--remote-repo` after installing.
6. **Report to the user:** what is ready and what is missing (environment, checkpoints, GPU), the saved settings, and whether they are `benchmarked` or `heuristic`. Say what to add to get a benchmark.

## Running jobs on a remote machine

When `configs/local.yaml` has `execution.mode: remote`, run model-backed commands there: `run` with an energy fallback, `review`, `batch` and `denovo`. Copy inputs to the remote checkout with `scp` or `rsync`. Run `ssh <host> '<prefix> cd <repo> && <python> -m msms_structure_elucidation.cli …'`, then copy the result directory back and open `msms-visualize` locally. Atlas-only retrieval can run on either machine.

Rerun setup after changing GPUs, drivers, checkpoints or the ms-pred version. The benchmark measures ICEBERG. GLACIER uses the same batch_size and CPU workers, and always one GPU worker.
