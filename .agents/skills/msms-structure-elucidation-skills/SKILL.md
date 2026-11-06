---
name: msms-structure-elucidation-skills
description: Coordinate end-to-end MS/MS molecular structure elucidation from spectra, feature exports, raw data or public dataset links through candidate search, spectral prediction, evidence review and an interactive report. Use when asked to elucidate unknown structures or run this skill collection.
---
# MS/MS structure elucidation

Use this skill as the entrypoint for one or many unknowns. Read the collection's [AGENTS.md](../../../AGENTS.md) and [scientific workflow](../../workflows/msms-elucidation.md) before starting. The workflow is the canonical scientific procedure; follow its input handling, model/search selection, energy handling, review and completion requirements.

Keep the full checkout, documentation and companion skills available together. If this skill was copied outside the checkout, locate the retained collection from the project's installation context and resolve repository references there. Use [msms-setup](../msms-setup/SKILL.md) for missing installation or configuration. With MCP connected, call `server_info` first and use its model tools.

## Establish scope

Accept experimental `.ms` spectra, feature-table/MGF exports, raw/mzML data, or public links with the requested files/features identified. Reuse supplied formulas, structures and acquisition context. Read [saved preferences](../../../docs/pipeline-preferences.md) and the applicable `configs/local.yaml`; reuse matching confirmations and ask only for missing or conflicting information. For a follow-up, inspect existing jobs, results, evidence reviews and saved viewer notes before launching more work. Route a narrowly scoped request directly to its relevant skill.

## Coordinate the workflow

Load specialized instructions as their work becomes relevant:

- Input preparation: [msms-feature-detect](../msms-feature-detect/SKILL.md), [msms-inspect](../msms-inspect/SKILL.md) or [msms-preprocess](../msms-preprocess/SKILL.md), under the workflow's conditions.
- Candidate search and prediction: [msms-atlas-retrieval](../msms-atlas-retrieval/SKILL.md) and [msms-predict](../msms-predict/SKILL.md), using the confirmed model and the workflow's formula/search policy.
- Every stable result: [msms-evidence-review](../msms-evidence-review/SKILL.md), including strong matches and no-candidate outcomes. Continuously refill available review slots from its ready-feature queue while monitoring remaining jobs; keep deeper follow-ups separate from initial review coverage.
- Optional routes selected by the user: [msms-structure-proposal](../msms-structure-proposal/SKILL.md), or [msms-subformulae](../msms-subformulae/SKILL.md) followed by [msms-denovo](../msms-denovo/SKILL.md). Follow the workflow's selection requirements, score generated candidates and review the new evidence.
- Presentation: [msms-visualize](../msms-visualize/SKILL.md), combining multiple unknowns in one report and preserving saved annotations.

Register and maintain the analysis under [workflow completion hooks](../../../docs/workflow-hooks.md). Keep supervising queued/running jobs under [job monitoring](../../../docs/job-monitoring.md), using the supported dispatcher. Apply the evidence-review skill's bounded delegation rules when relevant; retain responsibility for monitoring, accepted reviews and shared state.

## Deliver

Return ranked candidate records (`retrieval.json`), a current `evidence_review.json` for each analyzed feature, and the rich interactive viewer as the primary result. Provide a verified clickable viewer URL or a shareable HTML export, labelling partial coverage. For an existing result:

```bash
msms-structure-elucidation visualize --result results/sample/retrieval.json
```

Report supported hypotheses, ambiguity, exclusions and unresolved failures. Verify feature accounting and required artifacts, then run `msms-workflow-guard finish` before claiming completion. Follow the workflow's pause/hold handling for unavailable evidence, resources or scoped user decisions.
