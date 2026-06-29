#!/usr/bin/env python3
"""
Example: predict LC-MS/MS spectrum for 2-aminoethyl benzoate (c1ccccc1C(=O)OCCN).

Run from project root:
    # Env: simulator-iceberg
    python .agents/skills/msms-sim-iceberg/examples/example-2-aminoethyl-benzoate/run_example.py \
        --gen_ckpt downloads/iceberg_dag_gen_msg_best.ckpt \
        --inten_ckpt downloads/iceberg_dag_inten_msg_best.ckpt
"""

import argparse
import subprocess
import sys
from pathlib import Path

EXAMPLE_SMILES = "c1ccccc1C(=O)OCCN"  # 2-aminoethyl benzoate


def main() -> None:
    p = argparse.ArgumentParser(description="ICEBERG example: 2-aminoethyl benzoate")
    p.add_argument("--gen_ckpt", required=True, type=Path)
    p.add_argument("--inten_ckpt", required=True, type=Path)
    p.add_argument(
        "--output_dir", type=Path, default=Path(".agents/test/iceberg_example")
    )
    p.add_argument("--cuda_devices", default=None)
    args = p.parse_args()

    script = Path(__file__).parent.parent.parent / "scripts" / "predict_msms.py"
    cmd = [
        sys.executable,
        str(script),
        "--smiles",
        EXAMPLE_SMILES,
        "--gen_ckpt",
        str(args.gen_ckpt),
        "--inten_ckpt",
        str(args.inten_ckpt),
        "--collision_energies",
        "20",
        "40",
        "--adduct",
        "[M+H]+",
        "--output_dir",
        str(args.output_dir),
        "--num_workers",
        "0",
    ]
    if args.cuda_devices:
        cmd += ["--cuda_devices", args.cuda_devices]

    print(f"Predicting spectrum for: {EXAMPLE_SMILES}")
    subprocess.run(cmd, check=True)
    print(f"\nResults in: {args.output_dir}")
    print("  spectrum.png       — predicted MS/MS spectrum plot")
    print("  fragments.json     — fragment SMILES per peak")
    print("  input_configs.yaml — run parameters")


if __name__ == "__main__":
    main()
