"""Stage 3 of the cross-model cascade: decode JAM fingerprints into structures with FRIGID.

Takes the JAM-predicted (binarized) fingerprints from stage 2 and decodes each
one into candidate structures with the FRIGID diffusion language model, filtering
generated candidates to the ground-truth molecular formula (treated here as known
from the precursor mass). Scoring reuses FRIGID's own
`dlm.utils.benchmark_utils` helpers so the metrics match
`scripts/eval_dlm_pred_fp.py` exactly, the only difference being that the
conditioning fingerprint comes from JAM rather than from a precomputed CSV.

FRIGID lives in a sibling repo with a conda env, so this script runs under that
env's interpreter and inserts `<frigid_root>/src` onto sys.path the same way
FRIGID's own eval script does.

Usage:
    # Env: external FRIGID conda env (not a snowmageddon pixi env)
    /mnt/home/magled/miniconda3/envs/masskit_ai/envs/frigid/bin/python \\
        .agents/skills/msms-cross-model-elucidation/scripts/03_decode_frigid.py \\
        --jam-metrics results/<timestamp>/02_jam/metrics.json \\
        --jam-fingerprints results/<timestamp>/02_jam/predicted_fingerprints.npz \\
        --output-dir results/<timestamp>/03_frigid
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import yaml

FRIGID_ROOT = Path("/mnt/home/magled/FRIGID")
sys.path.insert(0, str(FRIGID_ROOT / "src"))

from dlm.sampler import Sampler  # noqa: E402
from dlm.utils.benchmark_utils import (  # noqa: E402
    build_prediction_entry,
    compute_morgan_fingerprint,
    compute_tanimoto_similarity,
    evaluate_predictions,
    generate_with_formula_filter,
    get_inchikey_first_block,
    normalize_formula,
)
from rdkit import Chem, RDLogger  # noqa: E402
from rdkit.Chem import rdMolDescriptors  # noqa: E402

RDLogger.DisableLog("rdApp.*")

DEFAULT_CHECKPOINT = FRIGID_ROOT / "30000.ckpt"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the FRIGID decoding stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jam-metrics",
        required=True,
        help="Stage-2 metrics.json (one record per (molecule, collision energy)).",
    )
    parser.add_argument(
        "--jam-fingerprints",
        required=True,
        help="Stage-2 predicted_fingerprints.npz holding pred_fp_bits.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for per-record results and metrics.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(DEFAULT_CHECKPOINT),
        help="FRIGID DLM checkpoint (.ckpt).",
    )
    parser.add_argument(
        "--n-required",
        type=int,
        default=10,
        help="Number of unique formula-matching candidates to collect per record.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=300,
        help="Maximum generation attempts per record before giving up.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=16, help="FRIGID generation batch size."
    )
    parser.add_argument(
        "--softmax-temp", type=float, default=1.0, help="Sampling softmax temperature."
    )
    parser.add_argument(
        "--randomness", type=float, default=0.1, help="Sampling randomness factor."
    )
    parser.add_argument(
        "--fp-bits", type=int, default=4096, help="Fingerprint bit width."
    )
    parser.add_argument(
        "--fp-radius", type=int, default=2, help="Morgan fingerprint radius."
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Force CPU generation by hiding CUDA devices from FRIGID.",
    )
    return parser.parse_args()


def decode_record(
    sampler: "Sampler",
    fingerprint: np.ndarray,
    ground_truth_smiles: str,
    args: argparse.Namespace,
) -> dict:
    """Decode one JAM fingerprint into formula-filtered candidates and score them."""
    target_mol = Chem.MolFromSmiles(ground_truth_smiles)
    ground_truth_fp = compute_morgan_fingerprint(
        ground_truth_smiles, args.fp_bits, args.fp_radius
    )
    target_formula = normalize_formula(rdMolDescriptors.CalcMolFormula(target_mol))
    target_inchi_key = get_inchikey_first_block(Chem.MolToInchiKey(target_mol))

    (
        matched_smiles,
        matched_sims,
        total_gen,
        total_valid,
        total_matched,
        _,
        _,
        gen_time,
    ) = generate_with_formula_filter(
        sampler=sampler,
        fingerprint_array=fingerprint.astype(np.float32),
        target_formula=target_formula,
        target_smiles=ground_truth_smiles,
        n_required=args.n_required,
        max_attempts=args.max_attempts,
        batch_size=args.batch_size,
        softmax_temp=args.softmax_temp,
        randomness=args.randomness,
        fp_bits=args.fp_bits,
        fp_radius=args.fp_radius,
    )

    # generate_with_formula_filter pads matched_smiles with non-matching molecules
    # once it runs out of real formula matches within max_attempts (see its own
    # docstring: "padded with non-matched if needed"). The returned list gives no
    # per-entry match flag, so the formula is recomputed here per candidate and
    # only genuine matches are kept -- padding is discarded rather than reported
    # as if it were a ranked candidate.
    formula_matched = []
    for smi, sim in zip(matched_smiles, matched_sims):
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        gen_formula = normalize_formula(rdMolDescriptors.CalcMolFormula(mol))
        if gen_formula == target_formula:
            formula_matched.append((smi, sim))

    predictions = [
        build_prediction_entry(smi, sim, 1, "formula", args.fp_bits, args.fp_radius)
        for smi, sim in formula_matched
    ]
    predictions = [entry for entry in predictions if entry]

    result = evaluate_predictions(
        predictions,
        ground_truth_smiles,
        target_inchi_key,
        ground_truth_fp,
        args.fp_bits,
        args.fp_radius,
    )
    result.pop("predictions", None)
    # evaluate_predictions defines top-1 as predictions[0] in generation order and
    # scores each candidate against the ground truth itself. The `similarity`
    # carried on a prediction entry is instead its similarity to the *conditioning*
    # fingerprint, so candidate Tanimotos are recomputed against the ground truth
    # here to stay consistent with the reported tanimoto_top1/top10.
    candidate_tanimotos = [
        float(compute_tanimoto_similarity(ground_truth_fp, entry["fingerprint"]))
        for entry in predictions[: args.n_required]
    ]
    result.pop("top_predictions", None)
    result.update(
        {
            "target_formula": target_formula,
            "candidates": [
                {
                    "rank": rank,
                    "smiles": entry["smiles"],
                    "tanimoto_vs_ground_truth": tanimoto_value,
                    "similarity_to_conditioning_fp": float(entry["similarity"]),
                    "exact_match": bool(entry.get("inchi_key") == target_inchi_key),
                }
                for rank, (entry, tanimoto_value) in enumerate(
                    zip(predictions[: args.n_required], candidate_tanimotos), start=1
                )
            ],
            "num_formula_matched_candidates": len(predictions),
            "total_generated": int(total_gen),
            "total_valid": int(total_valid),
            "total_formula_matched": int(total_matched),
            "generation_time": gen_time,
        }
    )
    return result


def main() -> None:
    """Run the FRIGID decoding stage end to end."""
    args = parse_args()
    if args.cpu:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metrics = json.loads(Path(args.jam_metrics).read_text())
    fingerprints = np.load(args.jam_fingerprints, allow_pickle=True)
    spec_names = [str(name) for name in fingerprints["spec_names"]]
    bits = fingerprints["pred_fp_bits"]
    name_to_index = {name: index for index, name in enumerate(spec_names)}

    with (output_dir / "input_configs.yaml").open("w") as handle:
        yaml.safe_dump(
            {
                **vars(args),
                "frigid_root": str(FRIGID_ROOT),
                "fingerprint_source": "jam_predicted_binarized",
                "jam_threshold": metrics["threshold"],
                "formula_source": "ground_truth_assumed_known_from_precursor_mass",
                "num_records": len(metrics["records"]),
            },
            handle,
            sort_keys=True,
        )

    print(f"Loading FRIGID checkpoint: {args.checkpoint}", flush=True)
    sampler = Sampler(args.checkpoint)

    results_path = output_dir / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    done = {entry["spec_name"] for entry in results}

    start = time.time()
    pending = [r for r in metrics["records"] if r["spec_name"] not in done]
    print(f"{len(pending)} records to decode ({len(done)} already done)", flush=True)

    for position, record in enumerate(pending):
        fingerprint = bits[name_to_index[record["spec_name"]]]
        result = decode_record(
            sampler, fingerprint, record["ground_truth_smiles"], args
        )
        result.update(
            {
                "spec_name": record["spec_name"],
                "name": record["name"],
                "collision_energy": record["collision_energy"],
                "fingerprint_tanimoto": record["fingerprint_tanimoto"],
                "num_predicted_bits": record["num_predicted_bits"],
            }
        )
        results.append(result)
        # Written after every record so a long run can be resumed after an interrupt.
        results_path.write_text(json.dumps(results, indent=1))
        elapsed = time.time() - start
        print(
            f"[{position + 1}/{len(pending)}] {record['spec_name']} "
            f"top1_match={result['exact_match_top1']:.0f} "
            f"tanimoto_top1={result['tanimoto_top1']:.3f} "
            f"n_cand={result['num_formula_matched_candidates']} "
            f"({elapsed:.0f}s elapsed)",
            flush=True,
        )

    summary = {
        "n_records": len(results),
        "exact_match_top1": float(np.mean([r["exact_match_top1"] for r in results])),
        "exact_match_top10": float(np.mean([r["exact_match_top10"] for r in results])),
        "tanimoto_top1": float(np.mean([r["tanimoto_top1"] for r in results])),
        "tanimoto_top10": float(np.mean([r["tanimoto_top10"] for r in results])),
        "tanimoto_mean": float(np.mean([r["tanimoto_mean"] for r in results])),
        "fingerprint_tanimoto": float(
            np.mean([r["fingerprint_tanimoto"] for r in results])
        ),
        "total_seconds": time.time() - start,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1), flush=True)


# ponytail: self-check
# results.json must hold one entry per stage-2 record, each with
# exact_match_top1/top10 in {0.0, 1.0}, tanimoto_top1 in [0, 1], and
# len(candidates) <= n_required. A record whose num_formula_matched_candidates is
# 0 means the sampler never produced a candidate matching target_formula within
# max_attempts -- a legitimate outcome that must be reported, not retried away.
# The load-bearing invariant is that the per-candidate ground-truth Tanimotos
# agree with the aggregate metrics evaluate_predictions reported, since the two
# are computed by different code paths over two different notions of similarity:
#   python -c "
#   import json,sys
#   rs=json.loads(open(sys.argv[1]).read())
#   for r in rs:
#       if not r['candidates']: continue
#       cs=r['candidates']
#       assert abs(cs[0]['tanimoto_vs_ground_truth']-r['tanimoto_top1'])<1e-6, r['spec_name']
#       assert abs(max(c['tanimoto_vs_ground_truth'] for c in cs)-r['tanimoto_top10'])<1e-6, r['spec_name']
#   print('candidate/aggregate Tanimoto consistency OK', len(rs))
#   " results/<timestamp>/03_frigid/results.json
# Confirm the API surface before a long run:
#   python -c "import sys; sys.path.insert(0,'/mnt/home/magled/FRIGID/src'); \
#       from dlm.utils.benchmark_utils import generate_with_formula_filter as g; \
#       import inspect; print(inspect.signature(g))"

if __name__ == "__main__":
    sys.exit(main())
