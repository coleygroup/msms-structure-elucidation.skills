---
name: msms-evidence-review
description: Assess ranked MS/MS candidates against matched and unexplained peaks across all supplied collision energies, and write evidence_review.json. Use after retrieval or scoring, including strong matches, empty candidate results and delegated per-feature reviews; route new structure proposals to msms-structure-proposal.
---

# Review MS/MS candidate evidence

Apply this skill to each stable feature result as it becomes available, and after candidate rescoring. Review strong matches as well as weak ones. The main agent can perform the review directly or dispatch it to a reviewer; the scientific assessment and output requirements are the same.

This skill assesses existing candidates. It records evidence and recommends a next action. New candidate generation and model scoring follow their own skills and the user's selected route.

## Inputs

Read the original experimental spectrum, current retrieval/scoring JSON, its candidate-specific matches and predictions, and available fragment assignments. Include precursor/adduct, formula hypotheses and provenance, instrument, verified collision settings, search/prediction completeness, and model/atlas/checkpoint provenance. Include relevant user review notes and scoped decisions when available. A top score, screenshot or truncated `review_evidence` summary alone is insufficient.

For delegated work, the coordinator supplies a feature ID, an immutable result snapshot and its SHA-256, the current source result path, the exact experimental input and acquisition context, an assignment ID, and a private output directory. Read the [delegation procedure](references/delegation.md) before dispatching or accepting an assignment. If essential input is unavailable or inconsistent, describe the gap and return a provisional assessment; do not infer missing metadata or invent peak assignments.

## Assess the evidence

1. Verify input identity and acquisition context against the assigned feature. Preserve confirmed model/weight choices and verified NCE/eV settings. Review all energies of one feature together. For separate acquisitions, compare every supplied energy; for stepped acquisition, assess the single observed composite against the merged prediction and retain its component settings. Do not treat the components as separately measured spectra.
2. Follow the [evidence-review checklist](references/evidence-review.md). Compare leading candidates, build candidate-specific matched/unexplained peak evidence, and distinguish supported structural features from unresolved alternatives. Reconstruct unexplained peaks from the full experimental spectra when the saved summary is stale or truncated. Inspect available mirror spectra and fragments with `msms-visualize`, or use their underlying numeric/assignment data when an interactive viewer is unavailable; state any resulting limitation.
3. Check candidate-search, formula/adduct and energy coverage. Missing predictions or mismatched acquisitions need an operational fix; they are not evidence against a structure. High similarity does not resolve an isomeric ambiguity or establish unique identity. Separate measured peaks, model predictions, calculated assignments and chemical interpretation.
4. Record the assessment using the [review format](references/review-format.md). Set the weak-evidence and important-unexplained-peak flags explicitly. Recommend a next action, keeping it separate from any user-selected de novo route. A no-candidate outcome has weak evidence and needs a follow-up/skip decision from the user.

Write `evidence_review.json` for each assigned feature. A delegated reviewer writes only in its assigned output directory and returns the draft path, source hash, coverage, main uncertainties and recommended next action to the coordinator. Use `coverage: provisional` if required evidence or candidate/energy coverage is incomplete. Preserve concrete observations even when no defensible structural interpretation is possible.

## Coordinate batch reviews

Read `pipeline.evidence_review` from the effective configuration, including matching `configs/local.yaml` overrides. `mode: auto` delegates ready batch reviews when supported and useful; `main` reviews directly; `delegate` requests delegation when available, with direct review as the fallback. The defaults are at most two reviewers and one feature per task. Bound the pool by available agent slots and ready work, reserving the main agent to monitor jobs. Save operational overrides through `msms-structure-elucidation preferences`; do not ask again for an unchanged saved preference.

The coordinator assigns disjoint features or small groups, supplies this skill and its references explicitly, and keeps monitoring model jobs and reviewer tasks while reviews run. Each reviewer assesses all supplied energies and competing candidates for its feature. Reviewers do not spawn further agents, launch predictions/generators, change workflow/configuration state, select de novo routes, or ask the user questions. Return questions and missing evidence to the coordinator.

The coordinator checks each draft's scientific reasoning against the named peaks and alternatives, then validates the feature identity, current source hash, energy coverage and output fields. Publish a completed draft atomically with the shared checker:

```bash
msms-workflow-guard review-check --project-dir /path/to/project \
  --feature-id feature-001 --result results/feature-001/retrieval.json \
  --review results/review-tasks/task-001/feature-001/evidence_review.json \
  --publish results/feature-001/evidence_review.json
```

This command requires the installed project CLI in the client environment and uses no model dependencies. Validation does not establish the quality of the scientific reasoning. It rejects stale, provisional or incomplete reviews and leaves an existing published review untouched on failure. Keep provisional drafts visible as pending work; they cannot satisfy final completion. If a source changes during review, preserve the old draft and dispatch a fresh assessment for the new snapshot.

The coordinator alone publishes canonical reviews, updates workflow state, reconciles disagreements, asks the user about agent proposals/FRIGID/both/skip for affected entries, and checks final completion. Request a separate second opinion when a specific consequential ambiguity merits it; agreement between reviewers is not independent experimental confirmation. Preserve supported disagreements in the final assessment. Dispatching a task or receiving a draft is not completion of the requested analysis.

Use `msms-structure-proposal` only after the user chooses agent proposals; use `msms-denovo` for a selected FRIGID route and `msms-predict` for scoring. Reapply this evidence-review skill after the selected route's results are available.
