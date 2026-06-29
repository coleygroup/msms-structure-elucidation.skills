---
trigger: model_decision
description: Rules to implement a skill under `.agents/skills/`
---

# Skill Standards

All modular capabilities live as "Skills" in `.agents/skills/`. This ensures consistency and discoverability.

## Directory Structure

```
.agents/skills/<skill-name>/
├── SKILL.md                  # Required
├── scripts/                  # Optional: helper scripts
│   ├── run.py
│   └── setup_env.sh          # Required if the env needs manual post-install steps
├── examples/                 # Optional: reference input/output
│   └── example-name/
│       ├── README.md
│       ├── example_input.*
│       └── example_output.json
└── resources/                # Optional: configs, reference data
    └── config_template.yaml
```

## Environment Setup Scripts

If a skill's pixi environment requires packages that cannot be declared in `pyproject.toml` (non-PyPI wheels, GitHub-only packages, custom find-links indexes), create `scripts/setup_env.sh`:

- Must be idempotent (safe to re-run — pip skips already-installed packages).
- Must use `pixi run --environment <env-name> pip install ...`.
- Must print a verify command at the end.
- Register it in the top-level `setup_envs.sh` with a `run_if_exists <skill-name>` line.

The SKILL.md Prerequisites section must reference `setup_env.sh` as the install step — do not put raw pip commands in the SKILL.md prose.

## SKILL.md Format

### 1. YAML Frontmatter
```yaml
---
name: skill-name-in-kebab-case
description: Concise one-sentence summary of the skill's purpose and outcome.
category: category-name
---
```

- `name`: kebab-case
- `description`: what the skill is used for, not how it works. No mid-sentence colons in unquoted values.
- `category`: one of `preprocessing`, `simulation`, `retrieval`, `denovo`, `general`

### 2. Title and Goal
```markdown
# Skill Name

## Goal
State what this skill achieves in precise technical language.
```

### 3. Instructions
Numbered steps. Each step must:
- State the objective
- Provide the exact command with `# Env: <pixi-env-name>` annotation
- Include all required parameters with inline comments

**Command block format:**
````markdown
```bash
# Env: <pixi-env-name>
python .agents/skills/<skill-name>/scripts/run.py [arguments]
```
````

### 4. Examples
Each example in its own `examples/<name>/` subdirectory with a `README.md` documenting goal, steps, and expected outputs.

> **Note**: Never commit large model checkpoints (`.pth`, `.pt`, `.ckpt`) or raw data files into example directories.

### 5. Constraints
Document environment requirements, input format constraints, known limitations.

```markdown
## Constraints
- **Environment**: Requires pixi env `<name>` (`pixi run --environment <name> python ...`)
- **Input format**: mzML or MGF
```

### 6. References
Cite the methods, models, or databases the skill relies on (with DOIs when available).

## Script Standards

All scripts must include:

1. **Module-level docstring** with usage and environment requirement.
2. **argparse** with help text for every argument.
3. **Type hints** and one-line docstrings for non-trivial functions.
4. **`input_configs.yaml`** saved alongside results — capture all input params including defaults.

## Environment Annotation

Every code block in `SKILL.md` **must** have `# Env: <pixi-env-name>`. Refer to `mcp-environments.md` for the env list.

## Skill Naming

- Category prefixes: `msms-` for spectrum skills, `general-` for utilities
- Kebab-case, descriptive of purpose not method
- Private skills: prefix with `private-` (gitignored automatically)

## Author Footer

End every `SKILL.md` with:
```markdown
---

**Author:** Name
**Contact:** [name@example.com](mailto:name@example.com)
```
