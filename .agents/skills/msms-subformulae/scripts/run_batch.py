#!/usr/bin/env python3
"""
Batch-assign per-peak subformulae for every .ms file in a directory.

Parallelized over molecules (multiprocessing.Pool) and naturally resumable:
assign_subformulae already writes one output JSON per molecule, so a
re-invocation with the same --output-dir skips any <inchikey14>.json that
already exists rather than recomputing it.

Usage:
    # Env: denovo
    python .agents/skills/msms-subformulae/scripts/run_batch.py \\
        --ms-dir results/atlas_lookup_1k/ms_files \\
        --formulae-csv results/atlas_lookup_1k/uspto_sample1k.csv \\
        --adduct "[M+H]+" \\
        --output-dir results/atlas_lookup_1k/subformulae \\
        --failure-log results/atlas_lookup_1k/subformulae_failures.log \\
        --workers 16

Requirements:
    - Env: denovo
    - ms-pred-dev installed (via msms-denovo setup_env.sh)
    - .ms files named <inchikey14>.ms (as produced by msms-atlas-to-ms)
    - A CSV mapping inchikey14 -> smiles, formula (for known-structure assignment)
"""

# Env: denovo

import argparse
import csv
import multiprocessing as mp
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import _setup_ms_pred, assign_subformulae

# Set once per worker process via the Pool initializer.
_WORKER_MS_PRED_ROOT: Path | None = None
_WORKER_OUTPUT_DIR: Path | None = None
_WORKER_ADDUCT: str = "[M+H]+"
_WORKER_SMILES_FORMULA_MAP: dict = {}


def load_smiles_formula_map(csv_path: Path) -> dict:
    """Map inchikey14 -> {smiles, formula} from a lookup CSV (must include a formula column)."""
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors
    from rdkit import RDLogger

    RDLogger.DisableLog("rdApp.*")

    out = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            smiles = row["smiles"].strip()
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            inchikey14 = Chem.MolToInchiKey(mol)[:14]
            out[inchikey14] = {
                "smiles": smiles,
                "formula": rdMolDescriptors.CalcMolFormula(mol),
            }
    return out


def _init_worker(
    project_root: str, output_dir: str, adduct: str, smiles_formula_map: dict
) -> None:
    global \
        _WORKER_MS_PRED_ROOT, \
        _WORKER_OUTPUT_DIR, \
        _WORKER_ADDUCT, \
        _WORKER_SMILES_FORMULA_MAP
    _WORKER_MS_PRED_ROOT = _setup_ms_pred(Path(project_root))
    _WORKER_OUTPUT_DIR = Path(output_dir)
    _WORKER_ADDUCT = adduct
    _WORKER_SMILES_FORMULA_MAP = smiles_formula_map


def _assign_one(task: tuple[str, str]) -> tuple[str, str | None]:
    """Run in a worker process: assign subformulae for one molecule. Returns (inchikey14, error_or_None)."""
    inchikey14, ms_path_str = task
    ms_path = Path(ms_path_str)
    try:
        rec = _WORKER_SMILES_FORMULA_MAP.get(inchikey14)
        if rec is None:
            raise KeyError(f"{inchikey14} not found in formulae CSV")
        assign_subformulae(
            spectrum_path=ms_path,
            formula=rec["formula"],
            adduct=_WORKER_ADDUCT,
            output_dir=_WORKER_OUTPUT_DIR,
            ms_pred_root=_WORKER_MS_PRED_ROOT,
            smiles=rec["smiles"],
            use_all=False,
        )
        return inchikey14, None
    except Exception as e:
        return inchikey14, str(e)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch subformula assignment for a dir of .ms files"
    )
    p.add_argument(
        "--ms-dir", required=True, type=Path, help="Directory of <inchikey14>.ms files"
    )
    p.add_argument(
        "--formulae-csv",
        required=True,
        type=Path,
        help="CSV with smiles column, to derive formula per inchikey14",
    )
    p.add_argument("--adduct", default="[M+H]+")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--failure-log", required=True, type=Path)
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    p.add_argument("--config", default="configs/default.yaml")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).parent.parent.parent.parent.parent

    smiles_formula_map = load_smiles_formula_map(args.formulae_csv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)
    subform_out_dir = args.output_dir / "default_subformulae"
    subform_out_dir.mkdir(parents=True, exist_ok=True)

    ms_files = sorted(args.ms_dir.glob("*.ms"))
    pending = [p for p in ms_files if not (subform_out_dir / f"{p.stem}.json").exists()]
    n_already_done = len(ms_files) - len(pending)
    if n_already_done:
        print(
            f"Resuming: {n_already_done}/{len(ms_files)} already assigned, skipping.",
            flush=True,
        )

    tasks = [(p.stem, str(p)) for p in pending]

    n_ok, n_failed = n_already_done, 0
    with (
        open(args.failure_log, "w") as fail_log,
        mp.Pool(
            args.workers,
            initializer=_init_worker,
            initargs=(
                str(project_root),
                str(args.output_dir),
                args.adduct,
                smiles_formula_map,
            ),
        ) as pool,
    ):
        for i, (inchikey14, error) in enumerate(
            pool.imap_unordered(_assign_one, tasks, chunksize=4)
        ):
            if error is None:
                n_ok += 1
            else:
                n_failed += 1
                fail_log.write(f"{inchikey14}\t{error}\n")

            if (i + 1) % 50 == 0:
                print(
                    f"{n_already_done + i + 1}/{len(ms_files)} processed (ok={n_ok}, failed={n_failed})",
                    flush=True,
                )

    print(f"\nDone. {n_ok} subformulae assigned, {n_failed} failed.")
    print(f"Output dir: {args.output_dir}")
    print(f"Failures: {args.failure_log}")


if __name__ == "__main__":
    main()
