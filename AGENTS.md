# Repository guidance

Use `.agents/workflows/msms-elucidation.md` for the scientific workflow and `.agents/skills` for task-specific procedures. The same skills are linked from `.claude/skills` for Claude Code. Run `msms-structure-elucidation --help` for the portable CLI; set `MS_PRED_PYTHON` or `--ms-pred-python` to a Python with ms-pred and MSBuddy.

Always ask the spectrum provider which collision energies they supplied and whether those numbers are NCE or absolute eV; do not assume either unit from a header or value. Before feeding collision energy to a prediction model, convert confirmed NCE to eV using precursor m/z, round to integer eV, and pass the integer values with NCE mode disabled. Keep the CLI and report tool-neutral. Use public atlas retrieval before local model inference.

Use an existing verified model environment when present. When setup is needed, clone the target repository if absent, read its official README, and follow its installation instructions; `setup_envs.sh` is a CPU venv helper for ms-pred. Report formula provenance, matched and unexplained peaks, score, candidate source, and missing assets. Similarity is not calibrated confidence. Licensed NIST assets must be supplied by the user and must not be committed.
