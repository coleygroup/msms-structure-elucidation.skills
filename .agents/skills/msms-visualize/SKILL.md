---
name: msms-visualize
description: Open a local interactive webpage for ranked MS/MS spectra, fragment structures, and saved candidate review notes after retrieval or review.
---

# Interactive fragment review

After `msms-retrieval` or `msms-structure-review` produces `retrieval.json`, launch the viewer on localhost:

```bash
msms-structure-elucidation visualize --result results/sample/retrieval.json
```

When there is more than one unknown, always serve every result in one viewer instead of one server per spectrum:

```bash
msms-structure-elucidation visualize --result results/*/retrieval.json
```

The page opens on an overview of every unknown: summary counts, the rank-1 similarity distribution, outcome (ICEBERG Atlas, model prediction labelled with the model and version used such as ICEBERG 2.1 prediction, missing exact Atlas energy, not in ICEBERG Atlas, Atlas retrieval failed, no formula, no PubChem formula), with the next step shown for unknowns without candidates, review progress, and a filterable, sortable table with rank-1 structures. Each result keeps its own `review_notes.json` beside its `retrieval.json`; results load on demand, so large batches stay responsive. `--atlas-mgf` applies only to a single result.

The command prints a `http://127.0.0.1:<port>/` URL and serves until Ctrl+C. It selects a Python with `ms_pred`; that interpreter also needs Flask (`python -m pip install -e '.[visualize]'`). Use `--ms-pred-python` to select another interpreter. If ms-pred must be installed, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred), read the checkout's `README.md` **Install & setup** section, and follow its environment instructions before installing this skill's visualization extra. For an older result moved away from its cached atlas, pass `--atlas-mgf /path/to/formula.mgf` to restore fragment IDs.

Opening an unknown shows the review workspace: ranked candidates on the left, the mirror plot (experimental up, predicted down) in the middle, and an inspector with the candidate structure on the right. Hover a peak to light up its predicted fragment's atoms and bonds on the structure; click to pin it and see the fragment formula, predicted and experimental m/z, and ppm error. Drag across the plot to zoom and double-click to reset. **Compare** stacks a second candidate's prediction against the same experimental spectrum. The evidence panel lists the energy pairing, provenance (ICEBERG Atlas, or the model, version, weights and ms-pred version of a prediction, and whose structures), the other MSBuddy formula hypotheses with a copyable retrieval command, and the strongest unexplained peaks. Fragment IDs are available for annotated ICEBERG/GLACIER spectra; without them the mirror plot still works and the page says so.

Reviews autosave to `review_notes.json` as you set a decision (keep, uncertain, reject), type a candidate comment, or write a note on the selected fragment; computed rankings remain unchanged. Keyboard: `/` search, `j`/`k` next/previous unknown, `↑`/`↓` candidates, `←`/`→` annotated peaks, `1`/`2`/`3`/`0` decisions, `c` compare, `z` reset zoom, `?` help.

For a shareable record, **Export report** (or `msms-structure-elucidation visualize --result results/*/retrieval.json --export review_report.html`) writes one self-contained, read-only HTML page with the same overview and workspace for the top five candidates of every unknown, including saved notes. It opens offline from disk.

The public plasma demo uses the same rich static export with 20 candidates and `--demo-reviews`: visitors can try the review controls, but those trial notes stay in their own browser and do not change the published result. Do not publish a result containing local paths or licensed checkpoints.

When the visualization server is running, share its URL and tell the user they can leave reviews in the viewer (they autosave), then ask you in chat to review their notes and refine the predictions. The viewer does not notify you automatically when notes are saved.

Keep the viewer bound to `127.0.0.1`. It makes no prediction or atlas request and requires no external service. Similarity and fragment matches remain structural evidence, not identification confidence.
