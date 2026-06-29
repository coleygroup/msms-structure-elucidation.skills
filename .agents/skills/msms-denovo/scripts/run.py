"""
Generate candidate structures de novo from an experimental MS/MS spectrum.

Usage:
    python run.py --spectrum sample.mzML --top_k 10 --output out.json

Requirements:
    - Env: denovo
    - Checkpoint path set in configs/default.yaml or via --checkpoint
"""

import argparse
import json
from pathlib import Path

import yaml


def load_model(checkpoint: str):
    # TODO: load in-house de novo model from checkpoint
    raise NotImplementedError("De novo model not yet implemented.")


def load_spectrum(spectrum_path: str) -> dict:
    """Load and parse a spectrum file (mzML or MGF)."""
    # TODO: parse spectrum using pyteomics or pymzml
    raise NotImplementedError


def predict(model, spectrum: dict, top_k: int) -> list[dict]:
    """
    Generate top_k candidate structures for a spectrum.

    Returns:
        List of {smiles, score} dicts, sorted by score descending.
    """
    # TODO: run model inference
    raise NotImplementedError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="De novo structure prediction from MS/MS spectrum."
    )
    parser.add_argument(
        "--spectrum", required=True, help="Path to query spectrum (mzML or MGF)"
    )
    parser.add_argument(
        "--top_k", type=int, default=10, help="Number of candidates to return"
    )
    parser.add_argument(
        "--checkpoint", default="", help="Model checkpoint path (overrides config)"
    )
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    model = load_model(args.checkpoint)
    spectrum = load_spectrum(args.spectrum)
    candidates = predict(model, spectrum, args.top_k)

    result = {
        "query_spectrum": args.spectrum,
        "top_k": args.top_k,
        "candidates": candidates,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2))
    (output_path.parent / "input_configs.yaml").write_text(yaml.dump(vars(args)))
    print(output_path)


if __name__ == "__main__":
    main()
