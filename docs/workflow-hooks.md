# Workflow completion hooks

Use the shared `msms-workflow-guard` checker with Codex and Claude Code. It checks registered analyses when the agent tries to finish a turn, and returns specific unfinished work as a continuation instruction. `AGENTS.md` remains the shared instruction entry point.

## Install in the client project

Install this package in the **client's** Python environment, even when the models run over SSH. The hook only uses Python's standard library; it does not load models or connect to the prediction host.

```bash
python -m pip install -e /path/to/msms-structure-elucidation.skills
msms-workflow-guard install --project-dir /path/to/analysis-project
```

The installer merges SessionStart, UserPromptSubmit and Stop handlers into `.codex/hooks.json` and `.claude/settings.json`, preserving unrelated settings and hooks. This checkout includes those handlers. Ensure `msms-workflow-guard` is on the PATH inherited by the client: activate the package's environment before launching Codex or Claude Code. For an environment that cannot be activated, `install --command '<configured-python> -m msms_structure_elucidation.workflow_guard hook'` accepts an explicitly configured command. Quote paths for the client's shell and keep commands containing host-specific paths local to that host.

In Codex, trust the project and use `/hooks` to review and trust the hook definitions. Changed definitions require another review. In Claude Code, review project trust and hook settings, then reload/restart the client if needed. Creating these files does not grant trust, change managed policy or activate hooks in an already-running session. See the official [Codex hooks guide](https://learn.chatgpt.com/docs/hooks) and [Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

Verify the command is available from the client's environment:

```bash
msms-workflow-guard --help
```

The startup/prompt hook supplies `MS/MS workflow session_id: ...` as context. Confirm it appears in the client's hook diagnostics. An analysis is **not** registered merely by installing hooks. Register it before launching work as described below; ordinary questions, setup-only tasks and repository edits should not create analysis state.

For hooks installed in another project, that context points to the shared workflow and guide in the package's source checkout when they are not present locally. A packaged installation without local documentation points to the repository's online instructions instead. Keep the installed package and source instructions on matching versions.

## Register the requested analysis

Use the exact session ID supplied by the hook. Different sessions have independent state. Pass a stable run label and every requested feature ID, including entries that may later be excluded or fail. Qualify IDs by dataset when they repeat. For many features, `--feature-ids requested_features.json` accepts a JSON array of unique strings.

```bash
msms-workflow-guard start --session-id <session-id> --run-id sample-set \
  --feature csf_unknown --feature plasma_unknown_198 --feature plasma_unknown_583 \
  --output-dir results/sample-set
```

This prints the state path under the ignored `.msms-workflow/` directory and freezes the requested feature set in an adjacent `request.json`. Keep state on the client, where both the hook and agent can read it. Paths in state and reviews are absolute or relative to the registered project root. Preserve request/state on resume; to continue in another session, register a new session-owned run with the same full feature set and link its existing outputs. Do not delete a run or shrink its requested scope to pass the checker.

For raw input whose feature list is not yet available, use repeated `--input <input-id>` arguments instead of `--feature`. Register its feature-finding jobs immediately. Once feature finding settles, freeze the complete feature list exactly once:

```bash
msms-workflow-guard set-features --state <state-path> \
  --feature-ids requested_features.json --evidence converted/manifest.csv
```

The request retains the original input IDs and the feature manifest's hash. A run cannot finish before feature enumeration, and the frozen list cannot be replaced with a completed subset. Neither an unregistered job nor an omitted requested feature can be discovered automatically by this checker; register the complete authorized scope.

The main CLI also exposes these commands as `msms-structure-elucidation workflow ...`, or use `python -m msms_structure_elucidation.workflow_guard ...`.

## Maintain evidence while work runs

Update `state.json` as real artifacts are published, preferably by atomic replacement. Keep the state and source evidence consistent; a checklist assertion alone is not evidence. The checker verifies artifact presence, JSON structure, result hashes, terminal status and input accounting. The agent remains responsible for reading the scientific evidence and verifying remote process/scheduler state.

For each prerequisite (`inputs`, `feature_finding`, `collision_energy`, `model_selection`, `candidate_search`), replace its pending entry with a record such as:

```json
{
  "status": "done",
  "reason": "Verified acquisition settings for these inputs; the source records and scan IDs are saved.",
  "evidence": ["results/sample-set/acquisition_evidence.json"]
}
```

Use `not_applicable` with a reason and supporting artifact for conditional work, such as feature finding when selected spectra were supplied. Model-selection evidence identifies the confirmed model, host and exact weights. Acquisition evidence records values, NCE/eV, separate/stepped mode and source. Candidate-search evidence identifies the requested route, formulas/mass window and completeness. Reference the applicable saved preference records rather than asking again.

Register **every** launched job/controller in `jobs`, including feature finding, retrieval, prediction and selected de novo work:

```json
{"job_id": "controller-001", "status_path": "results/sample-set/controller-status.json"}
```

The receipt at `status_path` uses `state` (`queued`, `submitted`, `running`, `waiting_resources`, `done`, `failed` or `cancelled`). Terminal receipts need `finished` and an integer `exit_code`. For example:

```json
{"state": "done", "finished": "2026-01-01T12:00:00+00:00", "exit_code": 0}
```

Preserve native receipts when possible. For remote MCP jobs or scheduler/controller formats that differ, save a local polling receipt with those normalized fields **and** the source host, job ID, observation time and underlying status/log/exit evidence. Do not convert an active job into a terminal receipt based on a PID or progress count. Failed/cancelled job entries also need a `reason` and `evidence` paths documenting their disposition. Resource waits remain active work to monitor.

For each feature, record its `outcome`, `result` and `review` paths. Supported final outcomes are `ranked`, `no_candidates`, `excluded` and `failed`. Excluded/failed entries need `reason` and `evidence` paths; they do not require fabricated spectra or peak reviews. Unresolved formula, energy, coverage or model errors remain pending, or are reported explicitly as failures with evidence. A failed outcome produces `completed_with_failures`, not a fully successful analysis.

Use [msms-evidence-review](../.agents/skills/msms-evidence-review/SKILL.md) and its [checklist](../.agents/skills/msms-evidence-review/references/evidence-review.md) to assess every result, including strong matches and no-candidate outcomes. The canonical machine-readable fields and energy/provenance rules are in its [per-feature review format](../.agents/skills/msms-evidence-review/references/review-format.md). This format is the same for direct reviews and delegated drafts; provisional assessments do not satisfy final completion.

Delegated reviewers write under their assignment directories. The coordinator checks the reasoning, validates feature identity, current source hash and every observed spectrum key, and atomically publishes accepted final drafts with `msms-workflow-guard review-check --project-dir <project> --feature-id <id> --result <result.json> --review <draft.json> --publish <canonical-evidence_review.json>`. Failed validation preserves the existing canonical review. Keep reviewer tasks under supervision until their requested assessments are accepted or explicitly accounted for; dispatch alone is not completion. Workflow state points to the accepted canonical review, and the checker still verifies it at finalization.

If either weak-evidence flag is true, record the user's `denovo` choice on that feature, separately from the assessment:

```json
{
  "choice": "both",
  "source": "user",
  "reason": "The user selected both routes for this feature.",
  "evidence": ["results/sample-set/denovo-choice.json"],
  "routes": {
    "agent": {"status": "done", "proposals": "results/sample-set/agent-proposals.json", "scored_result": "results/sample-set/agent-scored.json"},
    "frigid": {"status": "done", "proposals": "results/sample-set/frigid-proposals.json", "scored_result": "results/sample-set/frigid-scored.json"}
  }
}
```

Choices are `agent`, `frigid`, `both` or `skip`. Only selected generation routes need proposal/scoring artifacts; `skip` still needs user evidence. Validate proposals and score them through the appropriate skills, incorporate their evidence into the final comparison, and refresh the feature's result/review. A readable scoring JSON does not prove scientific validity; inspect formulas, identities and complete energy comparisons.

## Finish, pause or wait

Inspect outstanding requirements at any time:

```bash
msms-workflow-guard check --state <state-path>
msms-workflow-guard finish --state <state-path>
```

`finish` succeeds only after the prerequisites, full feature accounting, current final reviews, selected routes, terminal job receipts, readable HTML report and final assessment exist. It writes a `completion_receipt.json` beside state with outcome counts and request/state hashes. The Stop hook still rechecks completed analyses; changing a result invalidates its review. For an active or completed run, Stop returns promptly on the first proven blocking requirement. Use `check` for the full outstanding list; `finish` always checks the entire scope and revalidates review content and hashes. Scoped awaiting/blocked holds still require all affected work to be checked. Shared evidence files are checked once within each validation pass and checked afresh on the next pass. Repeated checks cache validated reviews by artifact identity, size and modification time so large batches do not repeatedly load every spectrum. For an explicitly waived HTML export, set `report.required: false` and add the user decision's `reason` and `evidence`; the final written assessment is still required.

An explicit user pause uses `status: paused` plus a `hold` record with `source: user`, `reason` and `evidence` paths. This permits the conversation to stop without claiming completion and does not terminate running processes. Set `status: active` and remove the hold when the user resumes.

For a genuine question or external blocker, use `status: awaiting_user` or `blocked`, with a `hold` containing `reason`, `evidence`, affected `feature_ids` and affected `prerequisites` names. The hold permits stopping only when all unfinished feature/decision work is covered, no registered jobs remain active, and no published result remains unreviewed. Continue monitoring and reviewing unaffected work while an optional question is pending. The next user message reactivates awaiting/blocked runs so the agent must reassess the reply and any remaining hold; it does not automatically resume an explicit user pause or authorize a new route. Waiting for an idle GPU alone does not qualify as a blocker.

If access to a job's host/status is unavailable, retain its last observation and the access-error evidence, record the current observation as unavailable rather than inventing a terminal state, and include its ID in a `blocked` hold's `job_ids`. An explicitly scoped external-access blocker can permit stopping without a completion claim. Known running/resource-waiting jobs still require monitoring; an optional user question cannot hide them.

Hook checks do not call the agent recursively. On an unfinished run they return `decision: block` with the missing requirements, so the client continues the existing agent. Do useful work or perform a bounded monitoring wait before another stop; do not repeatedly call Stop or mark evidence complete to bypass it. Client continuation limits, disabled/untrusted hooks, user interrupts, API failures and closed/crashed sessions can still end supervision. Use a persistent job controller for durable execution; hooks supervise normal conversational completion and cannot prove the quality of chemical reasoning.
