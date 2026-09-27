# MS/MS structure elucidation

Give an AI coding agent an experimental tandem mass spectrum and ask it to investigate possible molecular structures. These skills combine formula evidence, the public ICEBERG PubChem spectral atlas, optional structure prediction, and an interactive fragment viewer. They run with Codex or Claude Code; you can also use the Python command line tools directly.

## Quick start

Open Codex or Claude Code in your project and type:

> Install the skill for me: https://github.com/coleygroup/msms-structure-elucidation.skills

Then attach your spectrum or give its path and ask the agent to analyze it. The agent should ask you to confirm the collision energies and whether their numbers are **NCE or absolute eV** before scoring. The file header alone is not enough to establish the unit.

## Explore a completed example

The [plasma unknown 583 demo](demo/msms-structure-elucidation/index.html) is a self-contained interactive result. Download the HTML file and open it in a browser. It uses the public [plasma_unknown_583.ms](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms) spectrum from ms-pred. Its provider confirmed that the five energies, 10, 20, 30, 40, and 50, are **NCE**, despite the file's eV labels.

In the demo, switch between collision energies, click or hover over peaks to inspect predicted fragments on a candidate structure, and compare candidates. You can try the review controls; demo notes stay in your browser. Similarity ranks candidates but is not a probability of identification.

## What to provide

| Input | What the agent needs |
| --- | --- |
| Spectrum | A `.ms` file, GNPS/MZmine `.mgf`, or raw/mzML data from which to export MS/MS spectra. |
| Collision energies | Every supplied value and your confirmation that the values are NCE or absolute eV. |
| Precursor information | Precursor m/z and adduct, when they are not already in the file. |
| Helpful context | Instrument, sample context, and a molecular formula or formula list if you have one. |

A formula is optional: the workflow can propose formulas from the spectrum. Local model weights are optional for atlas retrieval, but may be needed when the atlas has no prediction at one of your energies or when you ask to score new structures. Licensed NIST weights are never bundled with these skills.

## What the agent does

1. Reads or converts the spectrum and checks its precursor, adduct, instrument, and energy labels with you.
2. Uses a supplied formula or examines multiple inferred formulas, then retrieves public ICEBERG predictions for matching PubChem structures.
3. Converts confirmed NCE to integer eV for prediction and compares **all** supplied collision energies. When exact atlas energy coverage is missing, it explains the required local model setup.
4. Ranks candidate structures, checks explained intensity and unmatched peaks, and flags structures these spectra cannot distinguish. If you add candidate structures or review notes, it can refine the analysis.

The public atlas does not contain licensed NIST structures. A good spectral match supports a hypothesis; it does not establish a unique molecular identity.

## What you receive

- An interactive report with ranked structures, energy-specific mirror spectra, fragment highlighting, candidate comparison, formula hypotheses, and the evidence behind each rank.
- `retrieval.json`, the machine-readable record of formulas, energy conversion, candidate scores, sources, and explicit outcomes such as no atlas coverage or missing model assets.
- A local review page where candidate decisions and fragment notes are saved to `review_notes.json`. A batch of unknowns can be reviewed together in one page.

## Try the example yourself

1. Ask your agent to install the skills using the quick-start prompt above.
2. Give it the [plasma spectrum](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms) and say: **“Analyze this spectrum. Its collision energies are 10, 20, 30, 40, and 50 NCE. Search the public atlas first and show me the interactive fragment viewer.”**
3. Open the result URL the agent provides. Select a candidate and energy, click a peak, compare another candidate, and inspect the unexplained peaks.
4. Add a candidate or fragment note in the local viewer, then tell the agent: **“Please review my saved notes and refine the candidates.”**

For this example, the workflow inferred three formulas and scored 158 public-atlas structures. The public demo shows the top 20 candidates with all five energy pairs; the leading candidate has 0.615 entropy similarity and 86.7% explained intensity. Those numbers describe the match to this spectrum, not identification confidence. See the [demo provenance](demo/msms-structure-elucidation/manifest.json) for the source spectrum and energy mapping.

For manual installation, batch processing, model checkpoints, and command line options, see the [technical guide](docs/technical-guide.md). The [scientific workflow](.agents/workflows/msms-elucidation.md) and [skills](.agents/skills) contain the agent procedures.
