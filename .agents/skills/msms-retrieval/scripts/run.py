"""
Retrieve candidate structures from a spectral database for an experimental spectrum.

Usage:
    python run.py --spectrum sample.mzML --db_path /data/spectral_db --top_k 10 --output out.json

Requirements:
    - Env: retrieval
    - Database path set in configs/default.yaml or via --db_path
"""

import argparse
import json
from pathlib import Path

import yaml


def load_db(db_path: str):
    # TODO: load spectral database (e.g. from HDF5, FAISS index, etc.)
    raise NotImplementedError("Spectral database not yet implemented.")


def load_spectrum(spectrum_path: str) -> dict:
    """Load and parse a spectrum file (mzML or MGF)."""
    # TODO: parse spectrum using pyteomics or pymzml
    raise NotImplementedError


def search(db, spectrum: dict, top_k: int) -> list[dict]:
    """
    Search database for top_k candidates matching the spectrum.

    Returns:
        List of {smiles, score, db_id} dicts, sorted by score descending.
    """
    # TODO: compute spectral similarity and rank candidates
    raise NotImplementedError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve structure candidates from spectral database."
    )
    parser.add_argument(
        "--spectrum", required=True, help="Path to query spectrum (mzML or MGF)"
    )
    parser.add_argument(
        "--db_path", default="", help="Path to spectral database (overrides config)"
    )
    parser.add_argument(
        "--top_k", type=int, default=10, help="Number of candidates to return"
    )
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    db = load_db(args.db_path)
    spectrum = load_spectrum(args.spectrum)
    candidates = search(db, spectrum, args.top_k)

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
