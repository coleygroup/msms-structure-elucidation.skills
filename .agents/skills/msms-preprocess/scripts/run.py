"""
Convert proprietary MS instrument files to mzML or MGF via msconvert (ProteoWizard).

Usage:
    python run.py --input sample.raw --output sample.mzML --format mzML

Requirements:
    - Env: preprocess
    - msconvert in PATH, or Docker with chambm/pwiz-skyline-i-agree-to-the-vendor-licenses
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SUPPORTED_INPUT = {".raw", ".d"}
SUPPORTED_OUTPUT = {"mzml", "mgf"}
DOCKER_IMAGE = "proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses"


def already_converted(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in SUPPORTED_OUTPUT


FORMAT_FLAG = {"mzml": "--mzML", "mgf": "--mgf"}


def _msconvert_args(
    input_path: Path, output_dir: Path, output_name: str, fmt: str
) -> list[str]:
    return [
        str(input_path),
        FORMAT_FLAG[fmt],
        "-o",
        str(output_dir),
        "--outfile",
        output_name,
        "--filter",
        "peakPicking true 1-",
    ]


def convert_system(input_path: Path, output_path: Path, fmt: str) -> None:
    """Run msconvert from PATH."""
    subprocess.run(
        ["msconvert"]
        + _msconvert_args(input_path, output_path.parent, output_path.name, fmt),
        check=True,
    )


def convert_docker(input_path: Path, output_path: Path, fmt: str) -> None:
    """Run msconvert via Docker, mounting input and output directories."""
    input_abs = input_path.resolve()
    output_abs = output_path.resolve()
    cmd = [
        _find_binary("docker"),
        "run",
        "--rm",
        "-v",
        f"{input_abs.parent}:/data/in:ro",
        "-v",
        f"{output_abs.parent}:/data/out",
        DOCKER_IMAGE,
        "wine",
        "msconvert",
    ] + _msconvert_args(
        Path("/data/in") / input_abs.name,
        Path("/data/out"),
        output_abs.name,
        fmt,
    )
    subprocess.run(cmd, check=True)


def _find_binary(name: str) -> str | None:
    """Find a binary via shutil.which or common fallback paths."""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/usr/bin", "/usr/local/bin", "/snap/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return None


def convert(input_path: Path, output_path: Path, fmt: str) -> None:
    """Try system msconvert first, fall back to Docker."""
    if _find_binary("msconvert"):
        convert_system(input_path, output_path, fmt)
    elif _find_binary("docker"):
        convert_docker(input_path, output_path, fmt)
    else:
        sys.exit(
            "Neither msconvert nor docker found. "
            "Install ProteoWizard or Docker to run conversion."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert raw MS files to mzML or MGF.")
    parser.add_argument(
        "--input", required=True, help="Path to input file (.raw, .d, mzML, MGF)"
    )
    parser.add_argument("--output", required=True, help="Path to output file")
    parser.add_argument(
        "--format", default="mzML", choices=["mzML", "MGF"], help="Output format"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    if already_converted(input_path):
        print(f"Already converted ({input_path.suffix}), passing through: {input_path}")
        print(input_path)
        return

    if input_path.suffix.lower() not in SUPPORTED_INPUT:
        sys.exit(
            f"Unsupported input format: {input_path.suffix}. Supported: {SUPPORTED_INPUT}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert(input_path, output_path, args.format.lower())
    print(output_path)


if __name__ == "__main__":
    main()
