# Dispatch and accept evidence reviews

## Coordinator

Dispatch stable features as they become ready. Use the effective `pipeline.evidence_review` settings: `mode` (`auto`, `main`, `delegate`), `max_reviewers` and `features_per_task`. `auto` uses delegation for independent batch work when the client supports it; a single ordinary feature can be reviewed directly. `delegate` requests delegation but falls back to direct review if unavailable. Keep worker counts within available slots and preserve the main agent's capacity to monitor jobs. Never leave a feature unreviewed because delegation is unavailable.

Create a disjoint assignment with a task directory for its outputs. Give the reviewer:

- This skill's path and its references, explicitly; do not assume the reviewer inherits them.
- The analysis/feature IDs, task ID and project root.
- The original experimental input, current source result path, a stable snapshot with the same bytes and its SHA-256, and available fragment/assignment data. Pass full spectra and leading candidates rather than just the top score or unexplained-peak summary.
- Verified energy settings, formula/adduct and acquisition/model provenance, search/coverage status, relevant user notes and the current scoped decisions as read-only context.
- The private output directory and the required draft format. For multiple features, use distinct per-feature subdirectories.

Prefer immutable published results. To snapshot a source that might be updated, check its hash before and after copying, and verify the copy has that hash; retry after it is stable if they differ. Keep the snapshot read-only to the reviewer. Work on the original source path only through the coordinator; reviewers write in their assigned output directories.

An assignment can be phrased as:

> Use `msms-evidence-review` at `<skill-path>/SKILL.md` to assess feature `<feature-id>`. Read the full experimental input at `<input>`, result snapshot at `<snapshot>`, acquisition/provenance context at `<context>`, and fragment data at `<fragment-path-if-available>`. The current source result is `<source>` and the snapshot SHA-256 is `<hash>`. The analysis project root is `<project>` and your assignment ID is `<task-id>`. Write `<task-dir>/<feature-id>/evidence_review.json` using the skill's review format, and return its path, coverage, hash, principal ambiguities and recommendation. Write only under `<task-dir>`; return any missing evidence or questions to me. Do not launch models, propose new structures, change shared state, ask the user or delegate further.

Adapt available inputs honestly; do not invent a fragment-data file or a user decision. Reviewers need no model environment for reasoning over already supplied artifacts. If additional scientific calculations are needed, the coordinator uses the configured environment within the authorized scope and supplies the resulting evidence.

Track task assignment, status and outcome in coordinator-owned records. Continue checking real model jobs and available results while reviewers work. Retrieve failures, partial drafts and terminal outcomes; retry or review directly when needed, without losing the requested feature set. A dispatched task, an idle reviewer or a returned draft alone does not complete the analysis.

## Reviewer

Assess each assigned feature across all supplied energies and competing leading candidates. Read the skill and checklist, verify snapshot identity, and preserve the assigned source hash. Use only the input evidence that is actually available. Record operational gaps as provisional with a concrete requested fix. Write per-feature drafts in the assigned directory and return a short handoff; keep detailed observations in the JSON rather than only in the chat response.

## Accept and publish

The coordinator checks the reasoning and named peaks, verifies the draft against the current source with `msms-workflow-guard review-check`, and publishes only accepted final reviews using its `--publish` option. Stale or provisional drafts remain pending; request a refreshed review for changed evidence. Keep old drafts and their provenance so conclusions can be traced to the data reviewed.

Use a separate reviewer for a specific difficult or consequential ambiguity when useful. Give that reviewer the raw artifacts and task, without supplying the first review's proposed conclusion. Compare the resulting evidence, not votes or numerical confidence invented by the reviewers. Record meaningful disagreement and unresolved alternatives in the canonical assessment. The main agent retains responsibility for user questions, selected routes, monitoring, final workflow state and completion.
