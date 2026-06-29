"""
Predict MS/MS spectrum for a molecule using the in-house simulator model.

Usage:
    python run.py --smiles "CCO" --adduct "[M+H]+" --collision_energy 35 --output out.json

Requirements:
    - Env: simulator
    - Checkpoint path set in configs/default.yaml or via --checkpoint
"""

import argparse
import json
from pathlib import Path

import yaml


def load_model(checkpoint: str):
    # TODO: load in-house simulator model from checkpoint
    raise NotImplementedError("Simulator model not yet implemented.")


def predict(
    model, smiles: str, adduct: str, collision_energy: float
) -> list[list[float]]:
    """
    Predict MS/MS peaks for a molecule.

    Returns:
        List of [mz, intensity] pairs.
    """
    # TODO: run forward pass
    raise NotImplementedError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Predict MS/MS spectrum for a molecule."
    )
    parser.add_argument("--smiles", required=True, help="Input molecule as SMILES")
    parser.add_argument(
        "--adduct", default="[M+H]+", help="Ionization adduct (default: [M+H]+)"
    )
    parser.add_argument(
        "--collision_energy", type=float, default=35.0, help="Collision energy in eV"
    )
    parser.add_argument(
        "--checkpoint", default="", help="Model checkpoint path (overrides config)"
    )
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    model = load_model(args.checkpoint)
    peaks = predict(model, args.smiles, args.adduct, args.collision_energy)

    result = {
        "smiles": args.smiles,
        "adduct": args.adduct,
        "collision_energy": args.collision_energy,
        "peaks": peaks,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2))
    (output_path.parent / "input_configs.yaml").write_text(yaml.dump(vars(args)))
    print(output_path)


if __name__ == "__main__":
    main()
