---
name: msms-visualize
description: Open a local interactive webpage for ranked MS/MS spectra, fragment structures, and saved candidate review notes after retrieval or review.
---

# Interactive fragment review

After `msms-retrieval` or `msms-structure-review` produces `retrieval.json`, launch the viewer on localhost:

```bash
msms-structure-elucidation visualize --result results/sample/retrieval.json
```

The command prints a `http://127.0.0.1:<port>/` URL and serves until Ctrl+C. It selects a Python with `ms_pred`; that interpreter also needs Flask (`python -m pip install -e '.[visualize]'`). Use `--ms-pred-python` to select another interpreter. If ms-pred must be installed, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred), read the checkout's `README.md` **Install & setup** section, and follow its environment instructions before installing this skill's visualization extra. For an older result moved away from its cached atlas, pass `--atlas-mgf /path/to/formula.mgf` to restore fragment IDs.

Select a ranked structure and collision energy, inspect predicted and experimental peaks, click a predicted or matched experimental peak to see the highlighted fragment, and use the review controls on the webpage. Candidate decisions and comments on selected fragment peaks are saved only when **Save review** is clicked. They go to `review_notes.json` beside `retrieval.json`; computed rankings remain unchanged. Fragment IDs are available for annotated ICEBERG/GLACIER spectra. If the prediction has no fragment IDs, the mirror spectrum still works and the page says why fragment drawing is unavailable.

When the visualization server is running, share its URL and tell the user they can leave reviews in the viewer, click **Save review**, then ask you in chat to review their notes and refine the predictions. The viewer does not notify you automatically when notes are saved.

Keep the viewer bound to `127.0.0.1`. It makes no prediction or atlas request and requires no external service. Similarity and fragment matches remain structural evidence, not identification confidence.
