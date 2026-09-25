"""Stage 4 of the cross-model cascade: assemble the results notebook and markdown report.

Reads the three stages' outputs and writes two deliverables:

  * a Jupyter notebook that reloads those outputs and, per compound and per
    collision energy, plots the GLACIER-simulated spectrum and tabulates the JAM
    fingerprint Tanimoto plus FRIGID's top-10 candidates;
  * a markdown report with one section per compound, naming the top candidate,
    its confidence, which model produced it, and the run's caveats.

The notebook is emitted as executed-on-open source (it carries no stored
outputs); it is executed here with nbconvert so the committed copy renders with
real figures and tables.

Runs in the `preprocess` pixi env, which is this project's env carrying
matplotlib, pandas, rdkit and nbconvert; the `default` agent env has none of
those.

Usage:
    # Env: preprocess
    pixi run --environment preprocess python \\
        .agents/skills/msms-cross-model-elucidation/scripts/04_build_report.py \\
        --results-dir results/<timestamp> \\
        --notebook notebooks/msms_cross_model_cascade.ipynb
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

import yaml

NOTEBOOK_KERNEL = {
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the report-building stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        required=True,
        help="Timestamped results directory holding 01_glacier, 02_jam and 03_frigid.",
    )
    parser.add_argument(
        "--notebook",
        default="notebooks/msms_cross_model_cascade.ipynb",
        help="Output path for the results notebook.",
    )
    parser.add_argument(
        "--report-name",
        default="report.md",
        help="Filename for the markdown report inside --results-dir.",
    )
    parser.add_argument(
        "--execute-notebook",
        dest="execute_notebook",
        action="store_true",
        default=True,
        help="Execute the notebook with nbconvert so it renders with real outputs.",
    )
    parser.add_argument(
        "--no-execute-notebook",
        dest="execute_notebook",
        action="store_false",
        help="Write the notebook source without executing it.",
    )
    return parser.parse_args()


def markdown_cell(source: str) -> dict:
    """Build a notebook markdown cell from a source string."""
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(True)}


def code_cell(source: str) -> dict:
    """Build an unexecuted notebook code cell from a source string."""
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(True),
    }


def build_notebook(results_dir: Path) -> dict:
    """Build the results notebook that reloads and visualizes all three stages."""
    cells = [
        markdown_cell(
            "# MS/MS cross-model cascade: GLACIER to JAM to FRIGID\n"
            "\n"
            "This notebook documents one run of the `msms-cross-model-elucidation`\n"
            "skill: a three-stage cascade over the 11 molecules of reaction 7,\n"
            "restricted to the `[M+H]+` adduct at collision energies 20, 40 and 60 eV.\n"
            "\n"
            "The cascade is strictly sequential, not a parallel fusion of three\n"
            "independent predictors:\n"
            "\n"
            "1. **GLACIER** simulates a sparse MS/MS spectrum (fragment m/z,\n"
            "   intensities and per-fragment formulas) from each SMILES.\n"
            "2. **JAM** encodes that simulated spectrum into a 4096-bit Morgan\n"
            "   fingerprint prediction.\n"
            "3. **FRIGID** decodes JAM's predicted fingerprint into candidate\n"
            "   structures, filtered to the ground-truth molecular formula.\n"
            "\n"
            "Only stage 3 yields structures, so the cascade's final answer is\n"
            "FRIGID's top-1 candidate. Stages 1 and 2 produce intermediate\n"
            "representations and are scored here only to localize where accuracy is\n"
            "lost."
        ),
        code_cell(
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "import matplotlib.pyplot as plt\n"
            "import pandas as pd\n"
            "\n"
            f"RESULTS_DIR = Path({str(results_dir.resolve())!r})\n"
            "SPECTRA_DIR = RESULTS_DIR / '01_glacier' / 'spectra'\n"
            "\n"
            "manifest = json.loads((RESULTS_DIR / '01_glacier' / 'manifest.json').read_text())\n"
            "jam_metrics = json.loads((RESULTS_DIR / '02_jam' / 'metrics.json').read_text())\n"
            "frigid_results = json.loads((RESULTS_DIR / '03_frigid' / 'results.json').read_text())\n"
            "frigid_summary = json.loads((RESULTS_DIR / '03_frigid' / 'summary.json').read_text())\n"
            "\n"
            "smiles_by_name = {m['name']: m['smiles'] for m in manifest['molecules']}\n"
            "frigid_by_spec = {r['spec_name']: r for r in frigid_results}\n"
            "jam_by_spec = {r['spec_name']: r for r in jam_metrics['records']}\n"
            "collision_energies = manifest['collision_energies']\n"
            "print(f\"{len(smiles_by_name)} molecules, adduct {manifest['adduct']}, \"\n"
            '      f"collision energies {collision_energies}")\n'
            "print(f\"JAM binarization threshold: {jam_metrics['threshold']}\")"
        ),
        markdown_cell(
            "## Run-level summary\n"
            "\n"
            "Cascade-level metrics, averaged over every (molecule, collision energy)\n"
            "pair that completed all three stages."
        ),
        code_cell(
            "summary_rows = [\n"
            "    ('records scored', frigid_summary['n_records']),\n"
            "    ('JAM fingerprint Tanimoto (mean)', round(frigid_summary['fingerprint_tanimoto'], 4)),\n"
            "    ('FRIGID exact match top-1', round(frigid_summary['exact_match_top1'], 4)),\n"
            "    ('FRIGID exact match top-10', round(frigid_summary['exact_match_top10'], 4)),\n"
            "    ('FRIGID Tanimoto top-1 (mean)', round(frigid_summary['tanimoto_top1'], 4)),\n"
            "    ('FRIGID Tanimoto top-10 (mean)', round(frigid_summary['tanimoto_top10'], 4)),\n"
            "]\n"
            "pd.DataFrame(summary_rows, columns=['metric', 'value'])"
        ),
        code_cell(
            "per_ce = pd.DataFrame([\n"
            "    {\n"
            "        'collision_energy': r['collision_energy'],\n"
            "        'fingerprint_tanimoto': r['fingerprint_tanimoto'],\n"
            "        'tanimoto_top1': r['tanimoto_top1'],\n"
            "        'tanimoto_top10': r['tanimoto_top10'],\n"
            "        'exact_match_top1': r['exact_match_top1'],\n"
            "        'n_candidates': r['num_formula_matched_candidates'],\n"
            "    }\n"
            "    for r in frigid_results\n"
            "])\n"
            "per_ce.groupby('collision_energy').mean().round(4)"
        ),
        markdown_cell(
            "## Per compound and per collision energy\n"
            "\n"
            "For each molecule: the GLACIER-simulated spectrum at each collision\n"
            "energy, then a table of JAM's fingerprint Tanimoto and FRIGID's decoding\n"
            "metrics, then FRIGID's top-10 candidates at the collision energy whose\n"
            "top-1 candidate is closest to the ground truth."
        ),
        code_cell(
            "def plot_spectra(name):\n"
            '    """Plot the GLACIER-simulated spectrum of one molecule at every collision energy."""\n'
            "    fig, axes = plt.subplots(\n"
            "        1, len(collision_energies), figsize=(4.4 * len(collision_energies), 3.0), sharey=True\n"
            "    )\n"
            "    for ax, ce in zip(axes, collision_energies):\n"
            "        spec = json.loads((SPECTRA_DIR / f'{name}_ce{ce}.json').read_text())\n"
            "        peaks = [(m, i) for m, i in zip(spec['masses'], spec['intensities']) if i > 0]\n"
            "        if peaks:\n"
            "            masses, intens = zip(*peaks)\n"
            "            ax.vlines(masses, 0, intens, linewidth=0.9, color='#1f4e79')\n"
            "        ax.set_title(f'{name} @ {ce} eV ({len(peaks)} peaks)', fontsize=10)\n"
            "        ax.set_xlabel('m/z')\n"
            "        ax.set_ylim(bottom=0)\n"
            "    axes[0].set_ylabel('relative intensity')\n"
            "    fig.suptitle(smiles_by_name[name], fontsize=11, y=1.04)\n"
            "    fig.tight_layout()\n"
            "    plt.show()\n"
            "\n"
            "\n"
            "def metrics_table(name):\n"
            '    """Per-collision-energy cascade metrics for one molecule."""\n'
            "    rows = []\n"
            "    for ce in collision_energies:\n"
            "        spec_name = f'{name}_ce{ce}'\n"
            "        frigid = frigid_by_spec.get(spec_name)\n"
            "        jam = jam_by_spec.get(spec_name)\n"
            "        if frigid is None:\n"
            "            rows.append({'collision_energy': ce, 'status': 'not scored'})\n"
            "            continue\n"
            "        rows.append({\n"
            "            'collision_energy': ce,\n"
            "            'fp_tanimoto': round(frigid['fingerprint_tanimoto'], 4),\n"
            "            'fp_bits_pred': jam['num_predicted_bits'] if jam else None,\n"
            "            'fp_bits_true': jam['num_true_bits'] if jam else None,\n"
            "            'frigid_tanimoto_top1': round(frigid['tanimoto_top1'], 4),\n"
            "            'frigid_tanimoto_top10': round(frigid['tanimoto_top10'], 4),\n"
            "            'exact_top1': bool(frigid['exact_match_top1']),\n"
            "            'exact_top10': bool(frigid['exact_match_top10']),\n"
            "            'n_formula_matched': frigid['num_formula_matched_candidates'],\n"
            "        })\n"
            "    return pd.DataFrame(rows)\n"
            "\n"
            "\n"
            "def candidate_table(name):\n"
            '    """FRIGID\'s top-10 candidates at this molecule\'s best collision energy."""\n'
            "    scored = [frigid_by_spec[f'{name}_ce{ce}'] for ce in collision_energies\n"
            "              if f'{name}_ce{ce}' in frigid_by_spec]\n"
            "    if not scored:\n"
            "        return None, None\n"
            "    best = max(scored, key=lambda r: (bool(r['candidates']), r['tanimoto_top1']))\n"
            "    table = pd.DataFrame([\n"
            "        {\n"
            "            'rank': c['rank'],\n"
            "            'smiles': c['smiles'],\n"
            "            'tanimoto_vs_ground_truth': round(c['tanimoto_vs_ground_truth'], 4),\n"
            "            'exact_match': c['exact_match'],\n"
            "        }\n"
            "        for c in best['candidates']\n"
            "    ])\n"
            "    return best, table"
        ),
    ]

    manifest = json.loads((results_dir / "01_glacier" / "manifest.json").read_text())
    for molecule in manifest["molecules"]:
        name = molecule["name"]
        cells.append(markdown_cell(f"### {name} — `{molecule['smiles']}`"))
        cells.append(
            code_cell(
                f"plot_spectra({name!r})\n"
                f"display(metrics_table({name!r}))\n"
                f"best, candidates = candidate_table({name!r})\n"
                "if best is None:\n"
                "    print('no FRIGID result for this molecule')\n"
                "else:\n"
                f'    print(f"ground truth: {{smiles_by_name[{name!r}]}}  "\n'
                "          f\"formula: {best['target_formula']}\")\n"
                "    print(f\"best collision energy: {best['collision_energy']} eV \"\n"
                "          f\"(top-1 Tanimoto {best['tanimoto_top1']:.4f})\")\n"
                "    display(candidates)"
            )
        )

    cells.append(
        markdown_cell(
            "## Caveats\n"
            "\n"
            "* The input spectra are **simulated**, not measured, so stage 2 and 3\n"
            "  accuracy is bounded by GLACIER's fragment prediction quality as well as\n"
            "  by JAM's and FRIGID's own error.\n"
            "* The molecular formula handed to FRIGID's formula filter is the\n"
            "  ground-truth formula, assumed known from the precursor mass. Without\n"
            "  that assumption the candidate set would be far larger.\n"
            "* JAM's binarization threshold is a fixed documented default, not tuned\n"
            "  on a validation split for this molecule set.\n"
            "* Several of these molecules are small and closely related structural\n"
            "  isomers, so exact-match top-1 is a harsh metric while Tanimoto is\n"
            "  correspondingly generous.\n"
            "* FRIGID generation is stochastic; re-running yields slightly different\n"
            "  candidate sets and metrics.\n"
            "* `tanimoto_top1` is the Tanimoto of the candidate FRIGID generated\n"
            "  first, not the best of the ten -- candidates are never re-ranked against\n"
            "  the ground truth. The sampler's own ranking uses similarity to the\n"
            "  *conditioning* fingerprint, a different quantity stored alongside each\n"
            "  candidate as `similarity_to_conditioning_fp`."
        )
    )

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": NOTEBOOK_KERNEL,
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def build_report(results_dir: Path) -> str:
    """Build the markdown report body from the three stages' outputs."""
    manifest = json.loads((results_dir / "01_glacier" / "manifest.json").read_text())
    jam_metrics = json.loads((results_dir / "02_jam" / "metrics.json").read_text())
    frigid_results = json.loads(
        (results_dir / "03_frigid" / "results.json").read_text()
    )
    summary = json.loads((results_dir / "03_frigid" / "summary.json").read_text())

    frigid_by_spec = {r["spec_name"]: r for r in frigid_results}
    jam_by_spec = {r["spec_name"]: r for r in jam_metrics["records"]}
    collision_energies = manifest["collision_energies"]

    lines = [
        "# MS/MS cross-model cascade report: reaction 7",
        "",
        f"Results directory: `{results_dir.resolve()}`",
        "",
        "## Pipeline",
        "",
        "Three models run in a strict cascade, restricted to the `[M+H]+` adduct at "
        f"collision energies {', '.join(f'{ce} eV' for ce in collision_energies)}:",
        "",
        "1. **GLACIER** (`ms-pred`) simulates a sparse MS/MS spectrum from each SMILES: "
        "fragment m/z, intensities and per-fragment element-count formulas.",
        "2. **JAM** (`MistNet`) encodes that simulated spectrum into a predicted "
        f"{jam_metrics['n_bits']}-bit Morgan fingerprint (radius {jam_metrics['radius']}), "
        f"binarized at threshold {jam_metrics['threshold']}.",
        "3. **FRIGID** (DLM decoder) decodes JAM's predicted fingerprint into candidate "
        "structures, filtered to the ground-truth molecular formula.",
        "",
        "Stages 1 and 2 emit intermediate representations, not candidate structures, so "
        "the cascade's final answer for every compound is **FRIGID's top-1 candidate**. "
        "The stage-1 and stage-2 numbers below localize where accuracy is lost; they are "
        "not independent votes to be fused.",
        "",
        "## Run-level results",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| (molecule, collision energy) pairs scored | {summary['n_records']} |",
        f"| JAM fingerprint Tanimoto (mean) | {summary['fingerprint_tanimoto']:.4f} |",
        f"| FRIGID exact match top-1 | {summary['exact_match_top1']:.4f} |",
        f"| FRIGID exact match top-10 | {summary['exact_match_top10']:.4f} |",
        f"| FRIGID Tanimoto top-1 (mean) | {summary['tanimoto_top1']:.4f} |",
        f"| FRIGID Tanimoto top-10 (mean) | {summary['tanimoto_top10']:.4f} |",
        "",
        "### By collision energy",
        "",
        "| Collision energy | JAM fp Tanimoto | FRIGID Tanimoto top-1 | FRIGID Tanimoto top-10 | Exact match top-1 |",
        "| --- | --- | --- | --- | --- |",
    ]

    for ce in collision_energies:
        subset = [r for r in frigid_results if r["collision_energy"] == ce]
        if not subset:
            continue
        lines.append(
            f"| {ce} eV | "
            f"{statistics.fmean(r['fingerprint_tanimoto'] for r in subset):.4f} | "
            f"{statistics.fmean(r['tanimoto_top1'] for r in subset):.4f} | "
            f"{statistics.fmean(r['tanimoto_top10'] for r in subset):.4f} | "
            f"{statistics.fmean(r['exact_match_top1'] for r in subset):.4f} |"
        )

    lines += ["", "## Per-compound results", ""]

    for molecule in manifest["molecules"]:
        name = molecule["name"]
        scored = [
            frigid_by_spec[f"{name}_ce{ce}"]
            for ce in collision_energies
            if f"{name}_ce{ce}" in frigid_by_spec
        ]
        lines += [f"### {name}", "", f"- Ground truth SMILES: `{molecule['smiles']}`"]
        if not scored:
            lines += [
                "- **No FRIGID result**: this molecule did not complete stage 3.",
                "",
            ]
            continue

        lines += [
            f"- Molecular formula: `{scored[0]['target_formula']}`",
            "",
            "| Collision energy | JAM fp Tanimoto | predicted / true bits | FRIGID Tanimoto top-1 | top-10 | Exact top-1 | Exact top-10 | Formula-matched candidates |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for ce in collision_energies:
            spec_name = f"{name}_ce{ce}"
            frigid = frigid_by_spec.get(spec_name)
            if frigid is None:
                lines.append(f"| {ce} eV | not scored | | | | | | |")
                continue
            jam = jam_by_spec.get(spec_name, {})
            lines.append(
                f"| {ce} eV | {frigid['fingerprint_tanimoto']:.4f} | "
                f"{jam.get('num_predicted_bits', '?')} / {jam.get('num_true_bits', '?')} | "
                f"{frigid['tanimoto_top1']:.4f} | {frigid['tanimoto_top10']:.4f} | "
                f"{bool(frigid['exact_match_top1'])} | {bool(frigid['exact_match_top10'])} | "
                f"{frigid['num_formula_matched_candidates']} |"
            )

        # Prefer a collision energy that actually produced candidates, so a
        # zero-candidate record cannot win the comparison on a 0.0 tie and hide
        # another energy's real result.
        best = max(scored, key=lambda r: (bool(r["candidates"]), r["tanimoto_top1"]))
        top_candidate = best["candidates"][0] if best["candidates"] else None
        lines += [
            "",
            f"**Top candidate** (model source: FRIGID, decoding JAM's predicted "
            f"fingerprint from GLACIER's {best['collision_energy']} eV simulated spectrum):",
            "",
        ]
        if top_candidate is None:
            lines += [
                "- No formula-matching candidate was generated at any collision energy.",
                "",
            ]
        else:
            best_of_ten = max(
                best["candidates"], key=lambda c: c["tanimoto_vs_ground_truth"]
            )
            lines += [
                f"- SMILES (rank 1): `{top_candidate['smiles']}`",
                f"- Tanimoto vs ground truth: "
                f"{top_candidate['tanimoto_vs_ground_truth']:.4f}",
                f"- Exact match (InChIKey connectivity block): {bool(best['exact_match_top1'])}",
                f"- Confidence proxy: JAM fingerprint Tanimoto "
                f"{best['fingerprint_tanimoto']:.4f} at this collision energy; "
                f"{best['num_formula_matched_candidates']} of "
                f"{best['total_generated']} generated molecules passed the formula filter.",
                f"- Best of the top-10 (not the reported top-1): "
                f"`{best_of_ten['smiles']}` at Tanimoto "
                f"{best_of_ten['tanimoto_vs_ground_truth']:.4f}.",
                "",
            ]
            if len(best["candidates"]) > 1:
                lines += [
                    "Full candidate list (generation order, rank 1 is top-1):",
                    "",
                ]
                for candidate in best["candidates"]:
                    marker = " **exact match**" if candidate["exact_match"] else ""
                    lines.append(
                        f"{candidate['rank']}. `{candidate['smiles']}` "
                        f"(Tanimoto {candidate['tanimoto_vs_ground_truth']:.4f})"
                        f"{marker}"
                    )
                lines.append("")

    skipped = jam_metrics.get("skipped", [])
    lines += [
        "## Caveats",
        "",
        "- The spectra driving this run are **simulated by GLACIER**, not measured. "
        "Stage 2 and 3 accuracy is therefore bounded by GLACIER's fragment prediction "
        "quality on top of JAM's and FRIGID's own error.",
        "- The formula passed to FRIGID's filter is the **ground-truth molecular "
        "formula**, assumed known from the precursor mass. A real unknown would need "
        "formula annotation first, and the candidate space would be larger.",
        f"- JAM's binarization threshold ({jam_metrics['threshold']}) is a fixed "
        "documented default. `jam.evaluate` normally selects it by sweeping on a "
        "validation split; no validation split exists for these 11 molecules, so the "
        "threshold is not fitted here.",
        "- Several inputs are small molecules or close structural isomers "
        "(two pairs share a molecular formula), so exact-match top-1 is a harsh metric "
        "while Tanimoto is correspondingly generous.",
        "- FRIGID sampling is stochastic; a re-run gives slightly different candidates "
        "and metrics.",
        "- Fingerprint Tanimoto is computed between JAM's **binarized** prediction and "
        "an RDKit Morgan fingerprint of the ground truth, both "
        f"{jam_metrics['n_bits']} bits at radius {jam_metrics['radius']}.",
        "- FRIGID's candidates are **not re-ranked against the ground truth**: "
        "`tanimoto_top1` is the Tanimoto of the candidate FRIGID generated first, "
        "matching `evaluate_predictions`' own definition, while `tanimoto_top10` is "
        "the best of the ten. Candidate Tanimotos in the per-compound lists are "
        "recomputed against the ground truth, which is a different quantity from the "
        "similarity-to-conditioning-fingerprint that the sampler ranks on; both are "
        "stored per candidate in `03_frigid/results.json`.",
    ]
    if skipped:
        lines.append(
            f"- {len(skipped)} (molecule, collision energy) pairs were skipped before "
            f"stage 2: {', '.join(s['spec_name'] + ' (' + s['reason'] + ')' for s in skipped)}."
        )
    else:
        lines.append(
            "- No (molecule, collision energy) pair was skipped: all "
            f"{summary['n_records']} pairs completed all three stages."
        )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    """Write the results notebook and the markdown report."""
    args = parse_args()
    results_dir = Path(args.results_dir)

    with (results_dir / "04_report_input_configs.yaml").open("w") as handle:
        yaml.safe_dump({**vars(args)}, handle, sort_keys=True)

    notebook_path = Path(args.notebook)
    notebook_path.parent.mkdir(parents=True, exist_ok=True)
    notebook_path.write_text(json.dumps(build_notebook(results_dir), indent=1))
    print(f"Wrote notebook to {notebook_path}", flush=True)

    if args.execute_notebook:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "jupyter",
                "nbconvert",
                "--to",
                "notebook",
                "--execute",
                "--inplace",
                "--ExecutePreprocessor.timeout=600",
                str(notebook_path),
            ],
            check=True,
        )
        print(f"Executed notebook in place: {notebook_path}", flush=True)

    report_path = results_dir / args.report_name
    report_path.write_text(build_report(results_dir))
    print(f"Wrote report to {report_path}", flush=True)


# ponytail: self-check
# The report must contain one "### <name>" section per molecule in the stage-1
# manifest and no placeholder numbers. After a run:
#   grep -c '^### rxn7_' results/<timestamp>/report.md   # expect 11
#   grep -c 'not scored\|No FRIGID result' results/<timestamp>/report.md
# Any non-zero second count is a legitimately skipped pair and must also appear
# in the report's Caveats section. The notebook must carry stored outputs after
# execution: `jq '[.cells[].outputs | length] | add' <notebook>` must exceed 0.

if __name__ == "__main__":
    sys.exit(main())
