# Simulated user runs

Each scenario file holds a fixed user request and the answers a simulated user gives. The developer session launches a fresh subagent with `harness.md`, filled in with one scenario, so the run sees only what a new user's agent would see: the repository, `AGENTS.md`, and the skills.

Placeholders in `{braces}` are filled from `configs/local.yaml` at launch, so no host paths are written here. Runs write to `results/agent-runs/`, which git ignores.

The `## Expected` section of a scenario is for the developer session to grade the run. Do not pass it to the subagent.
