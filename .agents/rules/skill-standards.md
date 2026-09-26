---
trigger: model_decision
description: Rules for skills in `.agents/skills/`
---
# Skill standards

Keep each `SKILL.md` self-contained with frontmatter `name` and `description`, a practical command, input/output expectations, and relevant method references. Scripts should use `argparse` and accept paths to their interpreter, model checkout, and checkpoints where relevant. Set up missing model environments through `setup_envs.sh` or a skill-specific setup script; do not rely on a repository-wide environment manager. Large checkpoints stay outside Git. Do not put tool-specific agent assumptions in the scientific workflow. `.claude/skills` points to these same skill folders.
