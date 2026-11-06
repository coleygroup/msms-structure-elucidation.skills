# Keep a batch review queue moving

Use this procedure whenever more than one feature needs evidence review. The coordinating agent drives the queue while monitoring prediction jobs; MCP prediction workers do not dispatch LLM reviewers. Keep the queue in coordinator-owned records under the analysis output directory, such as `review_queue.json`. Reuse an existing analysis ledger when it can represent these records instead of creating competing workflow state.

## Discover and track work

On each monitoring pass, discover newly published results and changed review inputs. Admit a feature when its result can be read as a stable snapshot. Record its analysis/feature ID, task ID, task kind, source path and SHA-256, relevant input/context identity, enqueue time, state, assignment/worker ID, draft path and accepted review path. Context identity includes the experimental input, verified acquisition settings and relevant saved user notes; a new user comment can require reassessment even when the ranking bytes are unchanged.

Use task kinds `initial`, `refresh` and `followup`, and states `ready`, `running`, `draft_ready`, `accepted`, `blocked` or `superseded`. Keep required fixes and their dependencies on blocked records. This is an operational dispatch record, not a replacement for the canonical evidence review or workflow completion state.

Deduplicate work by feature, task purpose and evidence version. Do not enqueue unchanged inputs on every poll or dispatch duplicate initial/refresh assessments of the same version. An intentional independent second opinion is a distinct follow-up task. Preserve old assignments when evidence changes, mark their tasks superseded and enqueue one current version; a superseded running assignment still occupies a slot until it returns or is stopped. Job heartbeats and viewer refresh timestamps alone are not scientific input changes. On resume, reconcile saved assignments with actual agent/task state and current artifact hashes before admitting replacements.

## Fill the available pool continuously

1. Reconcile prediction progress, reviewer handoffs and newly ready results on every monitoring pass. Collect each returned draft promptly and check it under the [delegation procedure](delegation.md). Publish accepted reviews and update workflow state without waiting for the other reviewers or the whole batch.
2. Fill each free reviewer slot immediately from the oldest ready `initial` or required `refresh` task. Preserve arrival order across datasets and include strong matches, weak matches and no-candidate results. Do not repeatedly select the same interesting feature while other ready features have no assessment. Honor an explicit user priority for an identified subset.
3. Reuse or replace available reviewers as the client supports. Supply a fresh, disjoint assignment with its full feature context; retain all energies together. Respect `max_reviewers`, `features_per_task` and the client's actual capacity, reserving the coordinator for monitoring and acceptance. Queue size may exceed worker capacity; do not try to spawn one agent per queued feature. Count follow-up reviewers against the same pool limit.
4. Return to job monitoring while reviewers work. Refill slots after individual handoffs or failures rather than dispatching a wave and waiting for every member. With `mode: main` or unavailable delegation, take the next ready feature directly and alternate its assessment with job monitoring.

Each initial assessment still follows the full evidence-review checklist: inspect the supplied spectra, leading candidates, matched/unexplained peaks and all energies. Coverage-first scheduling does not authorize a score-only summary or a weaker acceptance standard. `features_per_task` groups remain small; each feature has its own draft and completion record.

## Keep deeper follow-ups separate

Record additional raw-data QC, acquisition/adduct investigations, second opinions and selected de novo/proposal work as `followup` tasks linked to the feature and required inputs. When ready initial or required refresh reviews exist, use available review slots for that coverage before optional deeper investigations, unless the user explicitly prioritizes a follow-up. Launch authorized model jobs through their normal workflow when capacity permits; their execution need not block reviews of other features.

If a missing input prevents the required assessment, preserve the concrete observations as a provisional draft and mark the task blocked with the missing dependency. Release the reviewer to the next ready feature. Requeue that assessment only when the needed evidence arrives or a scoped user decision resolves the blocker; do not repeatedly retry unchanged missing inputs. Provisional work remains incomplete and visible in workflow state. Weak structural evidence or unresolved isomers alone do not require endless re-review: a complete assessment can document those uncertainties and request the user's next-route choice.

Optional proposals and FRIGID still require the applicable user selection. A follow-up queue entry is not authorization to run them. Reassess the feature when a selected route produces new scored candidates. Keep its baseline assessment and provenance traceable.

## Report and finish

Checkpoint assignments and outcomes after dispatch, handoff and publication so another turn can resume the queue. Report distinct features with current accepted reviews separately from pending initial assessments, required refreshes, running assignments, blocked/provisional work and follow-ups. Repeated handoffs for one feature do not increase reviewed-feature coverage.

An empty ready queue or idle reviewers do not establish completion. Reconcile every requested feature, outstanding prediction, required refresh and user-selected follow-up, then apply the workflow's completion checks. Preserve scoped holds and explicit pauses honestly; never mark provisional or stale evidence accepted to drain the queue.
