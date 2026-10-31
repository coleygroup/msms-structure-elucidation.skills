---
trigger: model_decision
description: Rules for skills in `.agents/skills/`
---
# Skill standards

Write skills and guides as instructions for a new user following the supported workflow. Describe behavior directly; omit conversation history, before/after narratives, experiment reports and one-off compatibility probes. Keep practical setup requirements, tuning guidance and operational limitations. Put detailed demo results and experiment provenance in their dedicated artifacts.

Keep each `SKILL.md` self-contained with frontmatter `name` and `description`, a practical command, input/output expectations, and relevant method references. Scripts should use `argparse` and accept paths to their interpreter, model checkout, and checkpoints where relevant. Set up missing model environments through `setup_envs.sh` or a skill-specific setup script; do not rely on a repository-wide environment manager. Large checkpoints stay outside Git. Do not put tool-specific agent assumptions in the scientific workflow. `.claude/skills` points to these same skill folders.

Use user-configurable executable paths for external converters and keep workflows independent of named servers. If a converter is missing, search current official installation instructions for the conversion host and offer manual installation or an optional Docker route. Let the user install the converter and any dependencies or Docker setup; do not automatically install them or run privileged commands, configure services or change group membership on their behalf. Accept open-format files exported on another user-selected host when installation is unsuitable.
