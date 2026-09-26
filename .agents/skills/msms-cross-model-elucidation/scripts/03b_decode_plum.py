"""Stage 3b of the cross-model cascade: decode JAM fingerprints with PLUM, seeded
with the full reagent pool from the real reaction (reaction_id "2_1" in
`/home/datashare/impurities/data/oprd/dataset_v7_0729.json`).

Unlike `03_decode_frigid.py`, which conditions PLUM's/FRIGID's sibling sampler
purely on formula + fingerprint from a fully masked (de-novo) input, this script
seeds the masked input with the SAFE encoding of the full real reagent pool
(reactant + reagents, excluding catalyst/solvent), then still passes
`formula=`/`fingerprint=` into `sampler.generate` on top of that seeded input --
mirroring `Sampler.unified_conditioned_generation`'s own input-construction
pattern (SAFE-encode -> tokenize -> `_insert_mask` -> `generate`), just seeding
the context with the reagent pool instead of an empty bos/eos pair.

Reaction 2_1 (OPRD 10.1021/acs.oprd.9b00553), a Friedel-Crafts acylation:
    reactant: O=C(Cl)c1ccccc1Cl        (2-chlorobenzoyl chloride)
    reagents: C1=CCCC1 (cyclopentene), [Cl][Al]([Cl])[Cl] (AlCl3, catalyst),
              C1CCCC1 (cyclopentane)
    solvent:  ClCCl (DCM)
AlCl3 and DCM are excluded from the seed pool -- they are catalyst/solvent and do
not contribute atoms to the product or impurity skeletons (confirmed by
inspecting the product/impurity SMILES in the dataset entry, all of which are
built from the chlorobenzoyl + cyclopentyl/cyclopentenyl fragments only).

PLUM lives in a sibling repo with the same conda env as FRIGID (verified
sampler.py is md5sum-identical), so this script runs under that env's
interpreter and inserts `<plum_root>/src` onto sys.path.

Seeding strategy: the WHOLE reagent pool is SAFE-encoded and used as fixed
context for every generation attempt -- no `fragment_completion`, no
`list_individual_attach_points`, no per-attempt random core selection. This
mirrors `unified_conditioned_generation`'s own input-construction pattern
(SAFE-encode -> tokenize -> `_insert_mask` -> `generate`), just seeding with
the full pool string instead of an empty bos/eos pair.

Usage:
    # Env: external FRIGID/PLUM conda env (not a msms-structure-elucidation Python env)
    $FRIGID_PYTHON \\
        .agents/skills/msms-cross-model-elucidation/scripts/03b_decode_plum.py \\
        --jam-metrics results/<timestamp>/02b_jam_reactant_aug/metrics.json \\
        --jam-fingerprints results/<timestamp>/02b_jam_reactant_aug/predicted_fingerprints.npz \\
        --output-dir results/<timestamp>/03_plum_reagent_seeded
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import safe as sf
import torch
import yaml

PLUM_ROOT = Path(os.environ.get("PLUM_DIR", "../PLUM")).resolve()
FRIGID_ROOT = Path(os.environ.get("FRIGID_DIR", "../FRIGID")).resolve()
sys.path.insert(0, str(PLUM_ROOT / "src"))
sys.path.insert(0, str(FRIGID_ROOT / "src"))

from dlm.sampler import Sampler  # noqa: E402
from dlm.utils.benchmark_utils import (  # noqa: E402
    build_prediction_entry,
    compute_morgan_fingerprint,
    compute_tanimoto_similarity,
    evaluate_predictions,
    get_inchikey_first_block,
    normalize_formula,
)
from rdkit import Chem, DataStructs, RDLogger  # noqa: E402
from rdkit.Chem import AllChem, rdMolDescriptors  # noqa: E402

RDLogger.DisableLog("rdApp.*")

DEFAULT_CHECKPOINT = (
    "/home/datashare/impurities/checkpoints/PLUM_ckpt/"
    "fp2mol_abs_230M_v2_finetune/checkpoints/43640.ckpt"
)

REACTANT_SMILES = "O=C(Cl)c1ccccc1Cl"
REAGENT_POOL_SMILES = [
    REACTANT_SMILES,
    "C1=CCCC1",
    "C1CCCC1",
]
REAGENT_POOL_MULTI_SMILES = ".".join(REAGENT_POOL_SMILES)
SEED_MIN_ADD_LEN = 40


def load_sampler_with_vocab_fix(checkpoint_path: str, scratch_dir: Path) -> "Sampler":
    """Load a PLUM Sampler, working around a checkpoint/config vocab-size drift.

    The 230M finetune checkpoint stores a `token_atom_counts` buffer of shape
    [1882, 30], but the current tokenizer config declares vocab_size=1880, so
    plain `torch.nn.Module.load_state_dict` raises a size-mismatch error before
    `DLM.load_state_dict`'s own missing-buffer handling (which only covers
    missing keys, not wrong-shaped ones) can help. `token_atom_counts` is a
    computed (not learned) buffer -- `DLM.load_state_dict` unconditionally
    recomputes it via `_setup_token_atom_counts()` after loading regardless of
    whether it was present in the checkpoint -- so it is safe to drop from the
    incoming state_dict before handing it to Lightning's loader. This mirrors
    what `DLM.load_state_dict` already does for buffers that are absent
    entirely; here the buffer is present but wrong-shaped, so it has to be
    stripped one level up, before `Sampler`/`DLM.load_from_checkpoint` runs.
    """
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if "token_atom_counts" in checkpoint.get("state_dict", {}):
        del checkpoint["state_dict"]["token_atom_counts"]
    scratch_dir.mkdir(parents=True, exist_ok=True)
    fixed_path = scratch_dir / f"{Path(checkpoint_path).stem}_vocab_fixed.ckpt"
    torch.save(checkpoint, fixed_path)
    return Sampler(str(fixed_path))


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the PLUM reagent-seeded decoding stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jam-metrics", required=True)
    parser.add_argument("--jam-fingerprints", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("--n-required", type=int, default=10)
    parser.add_argument("--max-attempts", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--softmax-temp", type=float, default=1.0)
    parser.add_argument("--randomness", type=float, default=0.1)
    parser.add_argument("--fp-bits", type=int, default=4096)
    parser.add_argument("--fp-radius", type=int, default=2)
    parser.add_argument("--cpu", action="store_true")
    return parser.parse_args()


def build_seed_tokens(sampler: "Sampler", pool_smiles: str) -> torch.Tensor:
    """Tokenize a SAFE-encoded multi-component reagent pool string.

    Mirrors the SAFE-encode step in `fragment_completion`/`unified_conditioned_generation`,
    but encodes the full reagent pool (all components joined with '.') rather than a
    single attach-point core, so the whole pool is present as fixed context.
    """
    encoded_pool = (
        sf.SAFEConverter(ignore_stereo=True).encoder(pool_smiles, allow_empty=True)
        + "."
    )
    x = sampler.model.tokenizer(
        [encoded_pool],
        return_tensors="pt",
        truncation=True,
        max_length=sampler.model.config.model.max_position_embeddings,
    )["input_ids"]
    return x


def reagent_seeded_conditioned_generation(
    sampler: "Sampler",
    pool_smiles: str,
    formula: str,
    fingerprint: np.ndarray,
    num_samples: int,
    softmax_temp: float,
    randomness: float,
) -> list[str]:
    """Seed generation with the full reagent pool, mirroring unified_conditioned_generation's
    own input-construction pattern (SAFE-encode -> tokenize -> _insert_mask -> generate),
    but seeding the context with the reagent pool instead of an empty bos/eos pair, and
    passing formula/fingerprint straight into `generate` on top of that seeded input.

    No `fragment_completion`, no `list_individual_attach_points`, no per-attempt random
    core selection: the whole pool string goes in as context on every attempt.
    """
    original_training = sampler.model.training
    sampler.model.eval()

    x = build_seed_tokens(sampler, pool_smiles)
    x = sampler._insert_mask(x, num_samples, min_add_len=SEED_MIN_ADD_LEN)
    x = x.to(sampler.model.device)

    samples = sampler.generate(
        x,
        softmax_temp=softmax_temp,
        randomness=randomness,
        formula=formula,
        fingerprint=fingerprint,
    )

    if original_training:
        sampler.model.train()
    return samples


def generate_with_formula_filter_seeded(
    sampler: "Sampler",
    pool_smiles: str,
    fingerprint_array: np.ndarray,
    target_formula: str,
    n_required: int,
    max_attempts: int,
    batch_size: int,
    softmax_temp: float,
    randomness: float,
    fp_bits: int,
    fp_radius: int,
) -> tuple:
    """Mirror generate_with_formula_filter's loop structure, but call the reagent-seeded
    composed generation function each attempt instead of unified_conditioned_generation.
    """
    matched_smiles: list[str] = []
    matched_similarities: list[float] = []
    matched_inchi_keys: set[str] = set()
    non_matched_smiles: list[str] = []
    non_matched_similarities: list[float] = []
    non_matched_inchi_keys: set[str] = set()
    total_generated = 0
    total_valid = 0
    total_formula_matched = 0
    generation_time = 0.0

    pred_fp_bitvect = DataStructs.ExplicitBitVect(fp_bits)
    for i in range(fp_bits):
        if fingerprint_array[i] > 0.5:
            pred_fp_bitvect.SetBit(i)

    stop_generation = False
    while total_generated < max_attempts and not stop_generation:
        current_batch = min(batch_size, max_attempts - total_generated)
        if current_batch <= 0:
            break

        gen_start = time.time()
        samples = reagent_seeded_conditioned_generation(
            sampler,
            pool_smiles,
            formula=target_formula,
            fingerprint=fingerprint_array,
            num_samples=current_batch,
            softmax_temp=softmax_temp,
            randomness=randomness,
        )
        generation_time += time.time() - gen_start
        total_generated += current_batch

        for smiles in samples:
            if not smiles:
                continue
            mol = Chem.MolFromSmiles(str(smiles))
            if mol is None:
                continue

            total_valid += 1
            canonical = Chem.MolToSmiles(mol)
            gen_fp = AllChem.GetMorganFingerprintAsBitVect(
                mol, fp_radius, nBits=fp_bits
            )
            similarity = float(DataStructs.TanimotoSimilarity(pred_fp_bitvect, gen_fp))

            try:
                inchi_key = Chem.MolToInchiKey(mol)
                inchi_key_first_block = (
                    get_inchikey_first_block(inchi_key) if inchi_key else None
                )
            except Exception:
                inchi_key_first_block = None

            gen_formula = normalize_formula(rdMolDescriptors.CalcMolFormula(mol))
            if gen_formula == target_formula:
                total_formula_matched += 1
                if (
                    inchi_key_first_block is None
                    or inchi_key_first_block not in matched_inchi_keys
                ):
                    if inchi_key_first_block is not None:
                        matched_inchi_keys.add(inchi_key_first_block)
                    matched_smiles.append(canonical)
                    matched_similarities.append(similarity)
                    if len(matched_smiles) >= n_required:
                        stop_generation = True
                        break
            else:
                if (
                    inchi_key_first_block is None
                    or inchi_key_first_block not in non_matched_inchi_keys
                ):
                    if inchi_key_first_block is not None:
                        non_matched_inchi_keys.add(inchi_key_first_block)
                    non_matched_smiles.append(canonical)
                    non_matched_similarities.append(similarity)

    if matched_smiles:
        pairs = sorted(
            zip(matched_similarities, matched_smiles), key=lambda t: t[0], reverse=True
        )
        matched_similarities, matched_smiles = (
            [p[0] for p in pairs],
            [p[1] for p in pairs],
        )

    if len(matched_smiles) < n_required and non_matched_smiles:
        pairs = sorted(
            zip(non_matched_similarities, non_matched_smiles),
            key=lambda t: t[0],
            reverse=True,
        )
        num_to_pad = n_required - len(matched_smiles)
        for sim, smi in pairs[:num_to_pad]:
            matched_smiles.append(smi)
            matched_similarities.append(sim)

    return (
        matched_smiles,
        matched_similarities,
        total_generated,
        total_valid,
        total_formula_matched,
        generation_time,
    )


def decode_record(
    sampler: "Sampler",
    fingerprint: np.ndarray,
    ground_truth_smiles: str,
    args: argparse.Namespace,
) -> dict:
    """Decode one JAM fingerprint into reagent-seeded, formula-filtered candidates."""
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
        gen_time,
    ) = generate_with_formula_filter_seeded(
        sampler=sampler,
        pool_smiles=REAGENT_POOL_MULTI_SMILES,
        fingerprint_array=fingerprint.astype(np.float32),
        target_formula=target_formula,
        n_required=args.n_required,
        max_attempts=args.max_attempts,
        batch_size=args.batch_size,
        softmax_temp=args.softmax_temp,
        randomness=args.randomness,
        fp_bits=args.fp_bits,
        fp_radius=args.fp_radius,
    )

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
    candidate_tanimotos = [
        float(compute_tanimoto_similarity(ground_truth_fp, entry["fingerprint"]))
        for entry in predictions[: args.n_required]
    ]
    result.pop("top_predictions", None)
    result.update(
        {
            "target_formula": target_formula,
            "seed_pool_smiles": REAGENT_POOL_MULTI_SMILES,
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
    """Run the PLUM reagent-seeded decoding stage end to end."""
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
                "plum_root": str(PLUM_ROOT),
                "fingerprint_source": "jam_predicted_binarized",
                "jam_threshold": metrics["threshold"],
                "formula_source": "ground_truth_assumed_known_from_precursor_mass",
                "num_records": len(metrics["records"]),
                "reagent_pool_smiles": REAGENT_POOL_SMILES,
                "seed_strategy": "full_reagent_pool_safe_encoded_as_fixed_context",
                "excluded_from_pool": [
                    "[Cl][Al]([Cl])[Cl] (catalyst)",
                    "ClCCl (solvent, DCM)",
                ],
            },
            handle,
            sort_keys=True,
        )

    print(f"Loading PLUM checkpoint: {args.checkpoint}", flush=True)
    sampler = load_sampler_with_vocab_fix(args.checkpoint, output_dir / ".ckpt_scratch")

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


if __name__ == "__main__":
    sys.exit(main())
