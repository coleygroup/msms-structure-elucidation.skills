---
trigger: model_decision
description: Rules for skills in `.agents/skills/`
---
# Skill standards

Write skills and guides as instructions for a new user following the supported workflow. Describe behavior directly; omit conversation history, before/after narratives, experiment reports and one-off compatibility probes. Keep practical setup requirements, tuning guidance and operational limitations. Put detailed demo results and experiment provenance in their dedicated artifacts.

Keep each `SKILL.md` self-contained with frontmatter `name` and `description`, a practical command, input/output expectations, and relevant method references. Scripts should use `argparse` and accept paths to their interpreter, model checkout, and checkpoints where relevant. Set up missing model environments through `setup_envs.sh` or a skill-specific setup script; do not rely on a repository-wide environment manager. Large checkpoints stay outside Git. Do not put tool-specific agent assumptions in the scientific workflow. `.claude/skills` points to these same skill folders.

Use user-configurable native executable paths for external converters. Do not recommend Docker, require privileged installation, or bind workflows to named servers. If a required converter is unavailable, explain which open-format input to provide from a user-selected conversion host.
