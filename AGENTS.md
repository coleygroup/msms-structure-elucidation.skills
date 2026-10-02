# Agent instructions

This repository contains MS/MS structure elucidation skills and a Python CLI, `msms-structure-elucidation`. The skills are in `.agents/skills`, and `.claude/skills` links to the same folders.

- **Install or set up:** when asked to install these skills, or when `configs/local.yaml` is missing, follow the `msms-setup` skill. It checks the environment, asks whether models run on this machine or a remote host, probes that host, and saves the tuned ms-pred inference settings to `configs/local.yaml`. Do not commit that file.
- **Remote execution:** if `configs/local.yaml` has `execution.mode: remote`, run model-backed commands on that host as described in `msms-setup`.
- **Model tools:** when the `msms-structure-elucidation` MCP server is connected, call `server_info` first, then use its tools for predictions instead of running the model scripts directly.
- **Analysis:** follow [.agents/workflows/msms-elucidation.md](.agents/workflows/msms-elucidation.md). Before scoring, confirm with the user whether the collision energies are NCE or absolute eV.
- **Standards:** code, skills, workflows and research conduct follow `.agents/rules/`. For batch sizes and worker counts, use `configs/local.yaml`, or the anchors in `msms-sim-iceberg` if that file is missing.
