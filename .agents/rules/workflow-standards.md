---
trigger: model_decision
description: Rules to implement a workflow under `.agents/workflows/`
---

# Workflow Standards

Workflows in `.agents/workflows/` describe high-level elucidation objectives that orchestrate multiple skills.

## What is a Workflow?

1. **High-Level Objective**: e.g. "Elucidate the structure of an unknown compound from a raw MS/MS spectrum."
2. **Scientific Scope**: Comparable in scope to a methods section of a paper — defines the problem and the sequence of steps to solve it.
3. **Flexibility**: Can reference skills by name, specify decision logic (e.g. cascade vs parallel), and document expected outputs.

## Directory Structure

```
.agents/workflows/
├── msms-elucidation.md
└── ...
```

## Workflow File Format

### 1. YAML Frontmatter
```yaml
---
description: Concise one-sentence summary of the workflow's objective.
---
```

### 2. Title and Problem Definition
```markdown
# Workflow Name

This workflow guides you through [high-level objective].

**Scientific Problem:** [Context and motivation — what question does this workflow answer?]
```

### 3. Step-by-Step Methodology
Numbered steps. Each step should:
- Reference existing skills by name (e.g. `msms-preprocess`, `msms-simulator`)
- Specify the pixi environment where relevant
- Include decision logic (e.g. "if retrieval score > threshold, skip de novo")

### 4. References
Cite any literature or databases the workflow is based on.
