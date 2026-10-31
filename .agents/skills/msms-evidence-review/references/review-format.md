# Per-feature review format

Write one `evidence_review.json` per feature. Delegated reviewers write it under their assigned task directory; the coordinator owns the canonical file beside the feature's result. All relative paths resolve from the analysis project root, not from the task directory. Absolute paths are suitable for an explicit cross-directory assignment.

The following illustrates the required fields; replace all example values with the actual assessment:

```json
{
  "feature_id": "feature-001",
  "source_result": "results/feature-001/retrieval.json",
  "source_sha256": "<SHA-256 of the exact assigned result bytes>",
  "reviewed_at": "2026-01-01T12:00:00+00:00",
  "coverage": "complete",
  "energies_reviewed": ["20", "40"],
  "assessment": "Explain the support for the leading hypotheses and the distinctions left unresolved.",
  "candidates_considered": ["<candidate SMILES or stable IDs>"],
  "peak_evidence": [
    {
      "candidate": "<candidate ID>",
      "spectrum_key": "20",
      "mz": 100.0,
      "intensity": 1.0,
      "energy": "<verified acquisition setting and unit>",
      "matched": true,
      "explanation": "<observed support, predicted assignment if available, and limitations>"
    }
  ],
  "supporting_evidence": ["<observed support>"],
  "conflicting_evidence": [],
  "ambiguities": ["<unresolved distinction>"],
  "weak_evidence": false,
  "important_unexplained_peaks": false,
  "recommended_next_action": "<continue reporting, resolve an operational issue, or ask about optional routes>",
  "reviewer": {"assignment_id": "task-001", "role": "evidence-review"}
}
```

`source_result` names the current result being assessed, even if the reviewer reads an immutable snapshot. `source_sha256` identifies that snapshot's bytes; never substitute a later result hash for evidence that was not reviewed. The coordinator rejects the draft if the current source has changed. Record the reviewed snapshot path as an additional `source_snapshot` when used. Retain the experimental input identity and acquisition/model provenance in the assessment or additional structured fields.

`energies_reviewed` lists the exact keys in the source's observed `spectra` mapping. They may be converted energy keys, not the original acquisition values; use `energy_mapping` and each candidate's `energy_alignment` to label observations correctly. For a stepped composite, use `["stepped"]`, record the component values/units and merge provenance, and assess only the observed composite.

For `ranked`, the candidates and peak evidence lists must be nonempty and must cover the relevant evidence across supplied energies. For `no_candidates`, they may be empty; set `weak_evidence: true` and describe the search outcome and uncertainty. Supporting/conflicting/ambiguity lists can be empty when justified, but do not fill them with invented evidence. Formula/adduct or incomplete prediction issues belong in an explicit operational limitation, not an unsupported structural conclusion.

Use `coverage: provisional` when input, candidate or energy coverage is incomplete, and add `missing_evidence` describing what is needed. Write down known observations and the next operational action. A provisional review cannot pass final publication validation or complete the analysis.

The coordinator stores the user's de novo choice separately in workflow state and scoped pipeline preferences. Do not label a recommendation as an approved `agent`, `frigid`, `both` or `skip` route. A second reviewer writes a separate draft; the coordinator records the reconciliation and preserves unresolved differences when publishing the assessment.
