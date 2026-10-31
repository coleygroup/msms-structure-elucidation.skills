# MS/MS structure elucidation

Give an AI coding agent an experimental tandem mass spectrum and ask it to investigate possible molecular structures. These skills combine formula evidence, the public ICEBERG PubChem spectral atlas, optional structure prediction, and an interactive fragment viewer. They run with Codex or Claude Code; you can also use the Python command line tools directly.

Use [msms-atlas-retrieval](.agents/skills/msms-atlas-retrieval/SKILL.md) to search the public ICEBERG PubChem atlas, and [msms-predict](.agents/skills/msms-predict/SKILL.md) to predict spectra and fragments from SMILES with GLACIER or ICEBERG.

## Quick start

Open Codex or Claude Code in your project and type:

> Install the skills from https://github.com/coleygroup/msms-structure-elucidation.skills. Check which required environments, repository checkouts, and model assets are already available, and tell me what is ready or missing. I am not providing a spectrum yet.

Agents: installation follows the [msms-setup](.agents/skills/msms-setup/SKILL.md) skill. It first finds or asks for the prediction server, which can be this machine, a GPU host reached over ssh, or a Slurm cluster. It confirms the intended prediction model and exact weights; either a complete ICEBERG checkpoint pair or a GLACIER checkpoint is sufficient. It then sets up that host's environment, probes and tunes it, saves the settings to `configs/local.yaml`, and registers the [MCP server](docs/technical-guide.md#mcp-server) that exposes ICEBERG, GLACIER, FRIGID, MIST and atlas retrieval as tools.

Installation does not require collision-energy information. When you are ready to analyze a spectrum, attach it or give its path and ask the agent to run the workflow. At that point, the agent should confirm the supplied collision energies and whether their values are **NCE or absolute eV** before scoring; a file header alone does not establish the unit.

## Explore a completed example

The [clinical unknowns demo](https://coley.mit.edu/msms-structure-elucidation.skills/demo/msms-structure-elucidation/) shows the three public unknowns in [ms-pred's clinical spectra directory](https://github.com/coleygroup/ms-pred/tree/main/data/exp_specs/clinical) on one interactive page. The GitHub Pages workflow rebuilds its HTML on every commit; you can also [build it locally](demo/README.md). The provider confirmed that the five energies, 10, 20, 30, 40, and 50, are **NCE** for all three unknowns, despite the files' eV labels.

In the demo, the mirror plot opens with all paired collision energies merged. Switch to an individual energy, click or hover over peaks to inspect predicted fragments on a candidate structure, and compare candidates. You can try the review controls; demo notes stay in your browser. Candidates are ranked by entropy similarity; explained intensity is shown as supporting evidence. Neither is a probability of identification.

## What to provide

| Input | What the agent needs |
| --- | --- |
| Spectrum | A `.ms` file, GNPS2 Everything Bagel/MZmine `.mgf`, or raw/mzML data from which to export MS/MS spectra. |
| Collision energies | Every supplied value and your confirmation that the values are NCE or absolute eV. |
| Precursor information | Precursor m/z and adduct, when they are not already in the file. |
| Helpful context | Instrument, sample context, and a molecular formula or formula list if you have one. |

A formula is optional: supply one or a list, ask for formula hypotheses from the spectrum, or request a [PubChem mass search within a ppm tolerance](docs/technical-guide.md#mass-only-pubchem-option) that skips formula prediction and predicts every matched candidate. With ICEBERG, prefer suitable public atlas spectra unless you request fresh predictions. The atlas uses ICEBERG 2.1 trained on NIST'23; when local weights differ or matching weights have not been verified, the agent explains that combining their predictions may affect ranking comparability. GLACIER requires predictions for every candidate because no open GLACIER atlas is currently available. Local model weights are optional for atlas retrieval and required for fresh predictions. Licensed NIST weights are never bundled with these skills.

## What the agent does

1. Reads or converts the spectrum and checks its precursor, adduct, instrument, and energy labels with you.
2. Uses supplied formulas, inferred hypotheses, or PubChem mass matches within your ppm tolerance. The mass-only route skips formula prediction and ranks candidates across every matched formula.
3. Uses your confirmed model and weights: ICEBERG can reuse suitable public atlas spectra; GLACIER and mass-only searches predict every candidate. It converts confirmed NCE to integer eV for prediction and compares **all** supplied collision energies.
4. Ranks candidate structures, checks explained intensity and unmatched peaks, and flags structures these spectra cannot distinguish. If you add candidate structures or review notes, it can refine the analysis.

The public atlas does not contain licensed NIST structures. A good spectral match supports a hypothesis; it does not establish a unique molecular identity.

## What you receive

- An interactive report with ranked structures, a merged mirror spectrum by default, individual energy views, fragment highlighting, candidate comparison, formula hypotheses, and the evidence behind each rank.
- `retrieval.json`, the machine-readable record of formulas, energy conversion, candidate scores, sources, and explicit outcomes such as no atlas coverage or missing model assets.
- A local review page where candidate decisions and fragment notes are saved to `review_notes.json`. A batch of unknowns can be reviewed together in one page.

Both the CLI atlas route and `run --mass-only`/`batch --mass-only` write these reports. For a ppm search that skips formula prediction, use `run --input sample.ms --collision-unit NCE --mass-only --ms1-ppm 5 --model glacier --output-dir results/sample` after confirming NCE and the model weights. The MCP `search_pubchem_mass` tool runs the same route on the prediction server; individual `score_candidates` calls return job results for explicit candidate lists.

## Try the example yourself

1. Ask your agent to install the skills using the quick-start prompt above.
2. Give it the [plasma spectrum](https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/plasma_unknown_583.ms) and say: **“Analyze this spectrum. Its collision energies are 10, 20, 30, 40, and 50 NCE. Search the public atlas first and show me the interactive fragment viewer.”**
3. Open the result URL the agent provides. Select a candidate and energy, click a peak, compare another candidate, and inspect the unexplained peaks.
4. Add a candidate or fragment note in the local viewer, then tell the agent: **“Please review my saved notes and refine the candidates.”**

The demo includes `csf_unknown.ms`, `plasma_unknown_198.ms`, and `plasma_unknown_583.ms`. See the [demo guide](demo/README.md) for the input spectra, energy mapping, saved results and build instructions.

For manual installation, batch processing, model checkpoints, and command line options, see the [technical guide](docs/technical-guide.md). The [scientific workflow](.agents/workflows/msms-elucidation.md) and [skills](.agents/skills) contain the agent procedures.
