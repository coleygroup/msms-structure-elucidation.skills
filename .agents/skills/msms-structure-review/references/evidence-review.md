# Agent review of spectral evidence

Apply this review after initial retrieval/scoring and after each refinement round. Review every result, including high-scoring candidates. This assessment precedes the user's choice of optional structure-generation routes; presenting a viewer or reading the top score does not complete it.

## Review checklist

- Compare leading candidates with the full experimental spectra at every supplied collision energy. Use `msms-visualize` to inspect mirror spectra and available fragment assignments. Check that all intended energy comparisons are present.
- Build a candidate-specific table of the peaks relevant to the decision: experimental m/z, intensity, collision energy, matched/unmatched status and proposed fragment explanation when available. Separate observed peaks from predicted assignments.
- Assess entropy similarity, explained intensity, strong unexplained peaks, matches that distinguish the leading structures and candidate differences the spectra cannot resolve. A high score does not by itself establish a unique structure; the 0.5 weak-match flag is a review heuristic.
- Check formula/adduct consistency and candidate-search completeness. Missing comparisons or failed predictions require an operational fix, and should not be treated as evidence of poor structural agreement.
- Treat `review_evidence` as a starting summary. Verify its named `best_candidate` against the current ranking; the strongest-unexplained list includes only 20 peaks. Reconstruct unmatched peaks from the original spectra when the summary is stale or incomplete.

## Record the decision

Write `evidence_review.json` alongside the result. Identify each assessed feature/entry, the candidates considered, supporting and conflicting peak evidence, unresolved ambiguities, and the agent's recommended next action. Identify which entries have weak support or important unexplained peaks and explain why.

For those entries, summarize the assessment and ask whether the user wants agent proposals plus ICEBERG/GLACIER scoring, FRIGID de novo plus scoring, both, or skip. Keep the recommendation distinct from the user's selected route. Record the selection and affected entries, and save the answer with its analysis/entry context through the pipeline-preferences procedure. Reuse an earlier explicit choice only when it covers the same analysis and entries. Neither weak evidence nor a failed proposal round automatically selects a route.

When no further route is selected, report the current hypotheses and their unresolved evidence. When a selected route finishes, refresh the assessment using its scored candidates.
