# Monitor jobs and review results as they arrive

Before launching the analysis, register its requested inputs/features and every launched job using [workflow completion hooks](workflow-hooks.md). Maintain the session-owned state and current evidence artifacts as work progresses; the Stop hook checks these records before normal turn completion.

## Stay with running work

Launching a job, receiving an MCP job ID or starting a detached controller is the beginning of execution. Keep monitoring queued and running jobs until the requested work and outputs are verified, or the user explicitly pauses/stops the work. A resource wait is still active work to supervise. If user input is required, state the specific blocker and continue monitoring or reviewing unaffected work.

With MCP, submit independent feature jobs with `wait_seconds=0` before waiting for their results. Queue independent formula-inference jobs first when their results are prerequisites, then submit each feature's retrieval or prediction as its required inputs become ready. The backend bounds active CPU preparation and GPU work; do not serialize an entire batch by waiting for one feature to finish before submitting the next.

Poll `get_job(job_id, wait_seconds=30)` across the parent jobs and inspect terminal results as they appear. A parent can be preparing on CPU, waiting for cached spectra or a GPU, or scoring on CPU. Its `prediction_job` identifies the current internal GPU stage and `prediction_jobs` lists all dispatched groups. With a [server pool](prediction-servers.md), inspect each child's server, scheduler state, transfer errors and cleanup/cancellation confirmations; that stage completing does not complete the feature. See [MCP execution stages](mcp-execution.md). Without MCP, inspect the actual process or scheduler state, recent logs, exit status, progress counters and output receipts on the execution host. Use bounded, adaptive waits rather than a tight polling loop, and keep the user informed during long waits. Check GPU occupancy when jobs are waiting for resources; a runner labelled `running` does not prove that inference is executing.

Each monitoring pass should:

1. Refresh job states and feature counts, distinguishing queued, waiting for resources, computing, completed, failed and excluded entries.
2. Identify newly published per-feature results, inspect failures or stalled progress, and verify the underlying process/log evidence when status is stale.
3. Review available spectral results using the procedure below while the remaining jobs continue.
4. Resume monitoring after review. Do not end active supervision merely because the launcher returned or a background process remains alive.

Repair recoverable failures within the authorized scope and resume only the affected work, preserving completed results, confirmed weights and scientific settings. Explain issues that need the user's input; do not silently skip failed features or overwrite their evidence.

## Start evidence review before the batch finishes

Apply the workflow's agent review as soon as a stable per-feature result is available. Do not wait for every feature, the final batch summary or the consolidated report. Alternate monitoring with review so neither activity prevents progress on the other.

Use [msms-evidence-review](../.agents/skills/msms-evidence-review/SKILL.md) for direct or delegated assessment. Prefer completed per-feature results with settled candidate and energy coverage. A readable partial result can also support a **provisional** review, but record the missing coverage and do not treat its current rank as final. Use published results or stable snapshots rather than files being written. Compare matched/unmatched peaks and leading candidates across the supplied energies using its [evidence-review checklist](../.agents/skills/msms-evidence-review/references/evidence-review.md).

When delegating, keep a bounded pool of reviewers on disjoint features/small groups and retain all energies of a feature in the same assignment. The main agent monitors model jobs and reviewer progress, supplies immutable input snapshots and provenance, checks returned reasoning, and publishes accepted drafts with `msms-workflow-guard review-check --publish`. Reviewers write only their assigned drafts and return gaps/questions to the coordinator. Do not stop supervising work after dispatch or finalize while a requested review remains pending; review directly if a task fails or delegation is unavailable. Operational defaults and overrides are under `pipeline.evidence_review`.

Write `evidence_review.json` beside each feature's result. Record its job/feature ID, source result path and hash, review time, completion/coverage state, supporting and conflicting evidence, ambiguities and recommended next action. Keep the user's selected optional route separate from that assessment. Revisit provisional reviews and refresh any review whose source ranking, candidates or energy coverage changes; do not mark the whole batch reviewed because the first few features were inspected.

For weakly supported entries, present the available evidence and ask about agent proposals, FRIGID, both or skip as described in the workflow. Ask for ready entries or identified subsets while the rest of the batch continues. Reuse applicable saved choices and keep monitoring unaffected jobs while waiting for a new answer. A provisional weak match does not by itself authorize a new model run.

When stable `retrieval.json` files are available, open or export a partial viewer for those results, clearly labelled as partial, and refresh it as more features finish. For example:

```bash
msms-structure-elucidation visualize \
  --result results/feature-001/retrieval.json results/feature-002/retrieval.json \
  --export results/review-progress.html --title 'Partial MS/MS review'
```

This is an agent-invoked progress export. The final batch exporter may still wait until the batch ends; that does not delay per-feature evidence review or require calling the progress page a final report.

## Verify completion

Before reporting the requested task as complete, verify terminal process/job states and exit statuses, reconcile every input feature with its outcome, and confirm that the required rankings, evidence reviews and final report actually exist and can be read. Distinguish successful analyses, no-candidate outcomes, exclusions and unresolved failures. Report incomplete or failed coverage explicitly. A PID, a queued job, a pilot result or a progress page is not a completion receipt.
