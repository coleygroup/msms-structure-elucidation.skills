"""Consistency tests for a completed msms-cross-model-elucidation run.

These check the invariants that span the three stages -- the places where two
independently-written code paths have to agree, and where a silent mismatch
would produce plausible-looking but wrong numbers.

Usage:
    # Env: preprocess
    pixi run --environment preprocess python \
        .agents/skills/msms-cross-model-elucidation/test_cascade_outputs.py \
        results/<timestamp>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_stage_outputs(results_dir: Path) -> tuple[dict, dict, list, dict]:
    """Load the manifest, JAM metrics, FRIGID results and FRIGID summary."""
    manifest = json.loads((results_dir / "01_glacier" / "manifest.json").read_text())
    jam_metrics = json.loads((results_dir / "02_jam" / "metrics.json").read_text())
    frigid_results = json.loads(
        (results_dir / "03_frigid" / "results.json").read_text()
    )
    summary = json.loads((results_dir / "03_frigid" / "summary.json").read_text())
    return manifest, jam_metrics, frigid_results, summary


def test_spectra_are_wellformed(results_dir: Path, manifest: dict) -> None:
    """Every stage-1 spectrum has aligned arrays and the [M+H]+ adduct."""
    spectra_dir = results_dir / "01_glacier" / "spectra"
    expected = {
        f"{molecule['name']}_ce{ce}"
        for molecule in manifest["molecules"]
        for ce in manifest["collision_energies"]
    }
    found = {path.stem for path in spectra_dir.glob("*.json")}
    assert found == expected, f"spectrum set mismatch: missing {expected - found}"

    for path in sorted(spectra_dir.glob("*.json")):
        spectrum = json.loads(path.read_text())
        lengths = {
            len(spectrum["masses"]),
            len(spectrum["intensities"]),
            len(spectrum["frag_formulas"]),
        }
        assert len(lengths) == 1, f"{path.name}: ragged arrays {lengths}"
        assert spectrum["adduct"] == "[M+H]+", f"{path.name}: {spectrum['adduct']}"
        assert spectrum["collision_energy"] in manifest["collision_energies"]
    print(f"OK  stage 1: {len(found)} spectra well-formed, all [M+H]+")


def test_fingerprints_match_records(results_dir: Path, jam_metrics: dict) -> None:
    """The stored fingerprint matrix lines up with the scored records."""
    import numpy as np

    data = np.load(
        results_dir / "02_jam" / "predicted_fingerprints.npz", allow_pickle=True
    )
    spec_names = [str(name) for name in data["spec_names"]]
    bits = data["pred_fp_bits"]
    record_names = [record["spec_name"] for record in jam_metrics["records"]]

    assert spec_names == record_names, "npz spec order differs from metrics.json"
    assert bits.shape == (len(record_names), jam_metrics["n_bits"]), bits.shape
    assert set(np.unique(bits)) <= {0.0, 1.0}, "pred_fp_bits is not binary"
    for record, row in zip(jam_metrics["records"], bits):
        assert record["num_predicted_bits"] == int(row.sum()), record["spec_name"]
        assert 0.0 <= record["fingerprint_tanimoto"] <= 1.0, record["spec_name"]
    print(f"OK  stage 2: {bits.shape[0]} binary fingerprints of width {bits.shape[1]}")


def test_candidate_metrics_are_consistent(frigid_results: list) -> None:
    """Per-candidate ground-truth Tanimotos agree with the aggregate metrics.

    tanimoto_top1 is the first-generated candidate (FRIGID's own definition) and
    tanimoto_top10 is the best of the ten; the per-candidate values are computed
    by a different code path, so agreement is the real check.
    """
    for record in frigid_results:
        candidates = record["candidates"]
        assert record["exact_match_top1"] in (0.0, 1.0), record["spec_name"]
        assert record["exact_match_top10"] in (0.0, 1.0), record["spec_name"]
        assert len(candidates) <= 10, record["spec_name"]
        if not candidates:
            assert record["num_formula_matched_candidates"] == 0, record["spec_name"]
            continue
        assert [c["rank"] for c in candidates] == list(range(1, len(candidates) + 1))
        rank1 = candidates[0]["tanimoto_vs_ground_truth"]
        assert abs(rank1 - record["tanimoto_top1"]) < 1e-6, (
            f"{record['spec_name']}: rank-1 {rank1} != tanimoto_top1 "
            f"{record['tanimoto_top1']}"
        )
        best = max(c["tanimoto_vs_ground_truth"] for c in candidates)
        assert abs(best - record["tanimoto_top10"]) < 1e-6, (
            f"{record['spec_name']}: best-of-ten {best} != tanimoto_top10 "
            f"{record['tanimoto_top10']}"
        )
        assert bool(candidates[0]["exact_match"]) == bool(record["exact_match_top1"])
    print(f"OK  stage 3: {len(frigid_results)} records, candidate/aggregate agreement")


def test_summary_matches_records(frigid_results: list, summary: dict) -> None:
    """The summary averages are the means of the per-record metrics."""
    import numpy as np

    assert summary["n_records"] == len(frigid_results)
    for key in (
        "exact_match_top1",
        "exact_match_top10",
        "tanimoto_top1",
        "tanimoto_top10",
        "fingerprint_tanimoto",
    ):
        expected = float(np.mean([record[key] for record in frigid_results]))
        assert abs(expected - summary[key]) < 1e-6, (
            f"{key}: {expected} vs {summary[key]}"
        )
    print("OK  summary averages match per-record metrics")


def test_every_molecule_reached_stage_three(
    manifest: dict, frigid_results: list
) -> None:
    """Each input molecule has at least one scored collision energy."""
    scored = {record["name"] for record in frigid_results}
    names = {molecule["name"] for molecule in manifest["molecules"]}
    missing = names - scored
    assert not missing, f"molecules absent from stage 3: {sorted(missing)}"
    print(f"OK  all {len(names)} molecules reached stage 3")


def main() -> None:
    """Run every cross-stage consistency test against one results directory."""
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    results_dir = Path(sys.argv[1])
    manifest, jam_metrics, frigid_results, summary = load_stage_outputs(results_dir)

    test_spectra_are_wellformed(results_dir, manifest)
    test_fingerprints_match_records(results_dir, jam_metrics)
    test_candidate_metrics_are_consistent(frigid_results)
    test_summary_matches_records(frigid_results, summary)
    test_every_molecule_reached_stage_three(manifest, frigid_results)
    print(f"\nAll cross-stage checks passed for {results_dir}")


if __name__ == "__main__":
    main()
