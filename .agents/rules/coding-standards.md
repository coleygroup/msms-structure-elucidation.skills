---
trigger: always_on
---

# Coding Standards

These rules apply across all code in this project. Follow them without exception.

## 1. Global Guidelines
1. **Language**: All code and comments must be in **English**.
2. **No separator comments**: Never write decorative separator lines like `# ---`, `# ===`, `# -- Section name --`, or any comment whose sole purpose is visual dividing. Use a blank line or a section header instead.
3. **Temporary Files**: All temporary log, validation, and testing files go under `<project_root>/.agents/test/`.
4. **Error Handling**: Avoid `try/except` unless the failure mode is genuinely recoverable.
5. **Cleanup**: Remove temporary test code and deprecated functions after implementation changes.
6. **Reusability**: Search existing dependencies before writing a new function.
7. **Imports**: Use **absolute imports**.
8. **Secrets**: All credentials and paths go in `.env` or `configs/`. Never hardcode.

## 2. Environment and Dependency Management
- Use the appropriate **pixi environment** for each model (see `mcp-environments.md`).
- The `default` environment is the fallback for orchestration and shared utilities.
- Never globally pin PyTorch or similar packages across environments — each feature set in `pyproject.toml` manages its own deps.
- If a tool requires an old Python version or conda-only packages, add it as a `conda-forge` dependency in the relevant pixi feature block.
- Do **not** implement import fallbacks when a package is missing — fix the environment.

## 3. Documentation and Testing
- Each major module gets a `README.md`.
- Skills follow `.agents/rules/skill-standards.md` strictly.
- Non-trivial scripts include a `# ponytail: self-check` block or a small `test_*.py`.
- Functions need type hints and a one-line docstring if their behavior isn't obvious from the name.

## 4. Performance and Safety
- Use streaming for large spectrum files.
- Monitor memory when loading model checkpoints.
- Input validation at trust boundaries only (CLI args, external file paths).
