"""
Convert proprietary MS instrument files to mzML or MGF.

Usage:
    python run.py --input sample.raw --output sample.mzML --format mzML

Requirements:
    - Env: preprocess
    - msconvert (ProteoWizard) in PATH, or set --converter
"""

import argparse
import subprocess
import sys
from pathlib import Path

SUPPORTED_INPUT = {".raw", ".d"}
SUPPORTED_OUTPUT = {"mzml", "mgf"}


def already_converted(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in SUPPORTED_OUTPUT


def convert(input_path: Path, output_path: Path, fmt: str, converter: str) -> None:
    """Run the external converter tool."""
    # TODO: implement conversion for each supported converter
    # Example for msconvert:
    #   subprocess.run([converter, str(input_path), "--outfile", str(output_path), f"--{fmt}"], check=True)
    raise NotImplementedError(f"Conversion via {converter!r} not yet implemented.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert raw MS files to mzML or MGF.")
    parser.add_argument(
        "--input", required=True, help="Path to input file (.raw, .d, mzML, MGF, ...)"
    )
    parser.add_argument("--output", required=True, help="Path to output file")
    parser.add_argument(
        "--format", default="mzML", choices=["mzML", "MGF"], help="Output format"
    )
    parser.add_argument(
        "--converter", default="msconvert", help="Converter binary (default: msconvert)"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    if already_converted(input_path):
        print(f"Already converted ({input_path.suffix}), passing through: {input_path}")
        print(input_path)  # downstream can capture this
        return

    if input_path.suffix.lower() not in SUPPORTED_INPUT:
        sys.exit(
            f"Unsupported input format: {input_path.suffix}. Supported: {SUPPORTED_INPUT}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert(input_path, output_path, args.format.lower(), args.converter)
    print(output_path)


if __name__ == "__main__":
    main()
