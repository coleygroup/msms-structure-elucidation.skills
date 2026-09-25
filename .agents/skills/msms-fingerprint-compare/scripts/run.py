#!/usr/bin/env python3
"""
Compute deterministic Morgan fingerprints from SMILES and compare them
(Tanimoto similarity) against MIST-predicted fingerprints for the same
molecules (inchikey14-matched).

Usage:
    # Env: retrieval
    python .agents/skills/msms-fingerprint-compare/scripts/run.py \\
        --smiles-csv .agents/test/uspto_sample1k.csv \\
        --mist-fingerprints results/atlas_lookup_1k/mist_fingerprints.hdf5 \\
        --output results/atlas_lookup_1k/fingerprint_comparison.csv \\
        --failure-log results/atlas_lookup_1k/fingerprint_compare_failures.log

Requirements:
    - Env: retrieval
"""

import argparse
import csv
from pathlib import Path

import h5py
import numpy as np
from rdkit import Chem
from rdkit import RDLogger
from rdkit.Chem import rdMolDescriptors

RDLogger.DisableLog("rdApp.*")

FP_BITS = 4096
FP_RADIUS = 2


def morgan_fp(smiles: str) -> np.ndarray | None:
    """Deterministic 4096-bit radius-2 Morgan fingerprint as a uint8 bit array."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    bv = rdMolDescriptors.GetMorganFingerprintAsBitVect(mol, FP_RADIUS, nBits=FP_BITS)
    arr = np.zeros((FP_BITS,), dtype=np.uint8)
    Chem.DataStructs.ConvertToNumpyArray(bv, arr)
    return arr


def tanimoto(a: np.ndarray, b: np.ndarray) -> float:
    """Tanimoto similarity between two binary fingerprint arrays."""
    a_bool, b_bool = a.astype(bool), b.astype(bool)
    intersection = np.logical_and(a_bool, b_bool).sum()
    union = np.logical_or(a_bool, b_bool).sum()
    return float(intersection / union) if union > 0 else 0.0


def decode(x) -> str:
    return x.decode() if isinstance(x, bytes) else x


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare MIST-predicted vs Morgan fingerprints"
    )
    p.add_argument(
        "--smiles-csv", required=True, type=Path, help="CSV with a smiles column"
    )
    p.add_argument(
        "--mist-fingerprints",
        required=True,
        type=Path,
        help="HDF5 from msms-mist-fingerprint",
    )
    p.add_argument("--output", required=True, type=Path, help="Output CSV path")
    p.add_argument("--failure-log", required=True, type=Path)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)

    smiles_by_inchikey14 = {}
    with open(args.smiles_csv) as f:
        for row in csv.DictReader(f):
            smi = row["smiles"].strip()
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                continue
            smiles_by_inchikey14[Chem.MolToInchiKey(mol)[:14]] = smi

    with h5py.File(args.mist_fingerprints) as h5:
        mist_inchikey14 = [decode(s) for s in h5["inchikey14"][:]]
        mist_fp_binary = h5["fp_binary"][:]

    n_ok, n_failed = 0, 0
    rows = []
    with open(args.failure_log, "w") as fail_log:
        for ikey, mist_fp in zip(mist_inchikey14, mist_fp_binary):
            smi = smiles_by_inchikey14.get(ikey)
            if smi is None:
                n_failed += 1
                fail_log.write(f"{ikey}\tNot found in --smiles-csv\n")
                continue
            morgan = morgan_fp(smi)
            if morgan is None:
                n_failed += 1
                fail_log.write(f"{ikey}\t{smi}\tInvalid SMILES for Morgan FP\n")
                continue
            sim = tanimoto(mist_fp, morgan)
            rows.append(
                {"inchikey14": ikey, "smiles": smi, "tanimoto_mist_vs_morgan": sim}
            )
            n_ok += 1

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["inchikey14", "smiles", "tanimoto_mist_vs_morgan"]
        )
        writer.writeheader()
        writer.writerows(rows)

    sims = [r["tanimoto_mist_vs_morgan"] for r in rows]
    print(f"Done. {n_ok} compared, {n_failed} failed.")
    if sims:
        print(
            f"Tanimoto: mean={np.mean(sims):.4f} median={np.median(sims):.4f} "
            f"min={np.min(sims):.4f} max={np.max(sims):.4f}"
        )
    print(f"Output: {args.output}")
    print(f"Failures: {args.failure_log}")


if __name__ == "__main__":
    main()
