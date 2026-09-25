#!/usr/bin/env python3
"""
Convert atlas-lookup output (HDF5 from msms-atlas-lookup) into ms-pred's
native .ms format, one file per molecule with all its collision-energy blocks.

Usage:
    # Env: retrieval
    python .agents/skills/msms-atlas-to-ms/scripts/run.py \\
        --input results/atlas_lookup_1k/spectra.hdf5 \\
        --adduct "[M+H]+" \\
        --instrument "Orbitrap (LCMS)" \\
        --output-dir results/atlas_lookup_1k/ms_files \\
        --failure-log results/atlas_lookup_1k/ms_conversion_failures.log

Requirements:
    - Env: retrieval
    - Input HDF5 must have datasets: smiles, inchikey14, formula, collision_energy, mz, intensity
"""

import argparse
from collections import defaultdict
from pathlib import Path

import h5py
from rdkit import Chem
from rdkit import RDLogger
from rdkit.Chem import rdMolDescriptors

RDLogger.DisableLog("rdApp.*")

ION_MASS = {"[M+H]+": 1.00728, "[M-H]-": -1.00728}


def decode(x) -> str:
    """Decode HDF5 bytes/str to a plain str."""
    return x.decode() if isinstance(x, bytes) else x


def write_ms_file(
    path: Path,
    compound: str,
    parent_mass: float,
    adduct: str,
    instrument: str,
    ce_blocks: dict,
) -> None:
    """Write one ms-pred .ms file with a metadata header and one block per CE."""
    lines = [
        f">compound {compound}",
        f">parentmass {parent_mass:.4f}",
        f">ionization {adduct}",
        f">instrumentation {instrument}",
        "",
    ]
    for ce, peaks in sorted(
        ce_blocks.items(), key=lambda kv: float(kv[0]) if kv[0] else 0.0
    ):
        ce_label = f"{float(ce):.1f} eV" if ce else "0.0 eV"
        lines.append(f">collision {ce_label}")
        for mz, inten in peaks:
            lines.append(f"{mz:.4f}\t{inten:.4f}")
        lines.append("")
    path.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert atlas-lookup HDF5 to ms-pred .ms files"
    )
    p.add_argument(
        "--input", required=True, type=Path, help="HDF5 from msms-atlas-lookup"
    )
    p.add_argument("--adduct", default="[M+H]+", choices=["[M+H]+", "[M-H]-"])
    p.add_argument("--instrument", default="Orbitrap (LCMS)")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--failure-log", required=True, type=Path)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)

    with h5py.File(args.input) as h5:
        smiles = [decode(s) for s in h5["smiles"][:]]
        inchikey14 = [decode(s) for s in h5["inchikey14"][:]]
        formula = [decode(s) for s in h5["formula"][:]]
        ce = [decode(s) for s in h5["collision_energy"][:]]
        mz = h5["mz"][:]
        inten = h5["intensity"][:]

    by_molecule: dict[str, dict] = defaultdict(lambda: {"ce_blocks": {}})
    for i in range(len(smiles)):
        rec = by_molecule[inchikey14[i]]
        rec["smiles"] = smiles[i]
        rec["formula"] = formula[i]
        rec["ce_blocks"][ce[i]] = list(zip(mz[i], inten[i]))

    n_ok, n_failed = 0, 0
    with open(args.failure_log, "w") as fail_log:
        for ikey, rec in by_molecule.items():
            try:
                mol = Chem.MolFromSmiles(rec["smiles"])
                if mol is None:
                    raise ValueError(f"Invalid SMILES: {rec['smiles']}")
                mw = rdMolDescriptors.CalcExactMolWt(mol)
                p_mass = mw + ION_MASS[args.adduct]

                out_path = args.output_dir / f"{ikey}.ms"
                write_ms_file(
                    path=out_path,
                    compound=ikey,
                    parent_mass=p_mass,
                    adduct=args.adduct,
                    instrument=args.instrument,
                    ce_blocks=rec["ce_blocks"],
                )
                n_ok += 1
            except Exception as e:
                n_failed += 1
                fail_log.write(f"{ikey}\t{rec.get('smiles', '')}\t{e}\n")

    print(f"Done. {n_ok} .ms files written, {n_failed} failed.")
    print(f"Output dir: {args.output_dir}")
    print(f"Failures: {args.failure_log}")


if __name__ == "__main__":
    main()
