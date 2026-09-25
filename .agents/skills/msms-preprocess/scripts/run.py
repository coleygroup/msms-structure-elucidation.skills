"""
Convert proprietary MS instrument files to mzML, MGF, or ms-pred .ms format.

Usage:
    python run.py --input sample.raw --output sample.mzML
    python run.py --input sample.mzML --output sample_dir/ --format MS

Requirements:
    - Env: preprocess
    - mzML/MGF: msconvert in PATH or Docker (proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses)
    - .ms: pyteomics (pure Python, no external tool)
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SUPPORTED_RAW = {".raw", ".d"}
SUPPORTED_CONVERTED = {".mzml", ".mgf"}
DOCKER_IMAGE = "proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses"
FORMAT_FLAG = {"mzml": "--mzML", "mgf": "--mgf"}
POLARITY_FILTER = {"positive": "polarity positive", "negative": "polarity negative"}


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


def _msconvert_args(
    input_path: Path, output_dir: Path, output_name: str, fmt: str, polarity: str
) -> list[str]:
    args = [
        str(input_path),
        FORMAT_FLAG[fmt],
        "-o",
        str(output_dir),
        "--outfile",
        output_name,
        "--filter",
        "peakPicking true 1-",
    ]
    if polarity in POLARITY_FILTER:
        args += ["--filter", POLARITY_FILTER[polarity]]
    return args


def convert_system(
    input_path: Path, output_path: Path, fmt: str, polarity: str
) -> None:
    """Run msconvert from PATH."""
    subprocess.run(
        ["msconvert"]
        + _msconvert_args(
            input_path, output_path.parent, output_path.name, fmt, polarity
        ),
        check=True,
    )


def convert_docker(
    input_path: Path, output_path: Path, fmt: str, polarity: str
) -> None:
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
        polarity,
    )
    subprocess.run(cmd, check=True)


def convert_raw(input_path: Path, output_path: Path, fmt: str, polarity: str) -> None:
    """Convert .raw/.d to mzML or MGF via msconvert (system binary or Docker)."""
    if _find_binary("msconvert"):
        convert_system(input_path, output_path, fmt, polarity)
    elif _find_binary("docker"):
        convert_docker(input_path, output_path, fmt, polarity)
    else:
        sys.exit(
            "Neither msconvert nor docker found. "
            "Install ProteoWizard or Docker to run conversion."
        )


def _ionization(spectrum: dict) -> str:
    """Infer ionization mode from spectrum metadata."""
    if "positive scan" in spectrum:
        return "[M+H]+"
    if "negative scan" in spectrum:
        return "[M-H]-"
    return "[M+H]+"


def _write_ms_file(path: Path, meta: dict, spectra_by_ce: dict) -> None:
    lines = [f">{k} {v}" for k, v in meta.items()]
    for label, peaks in spectra_by_ce.items():
        lines.append("")
        lines.append(f">{label}")
        for mz, intensity in sorted(peaks):
            lines.append(f"{mz}\t{intensity}")
    path.write_text("\n".join(lines) + "\n")


def convert_to_ms(input_path: Path, output_dir: Path) -> None:
    """Convert mzML to ms-pred .ms format — one file per unique precursor m/z.

    Groups MS2 scans by precursor m/z (4 dp). Each group becomes one .ms file.
    """
    try:
        from pyteomics import mzml
    except ImportError:
        sys.exit("pyteomics not installed. Run: pixi install --environment preprocess")

    # best scan per precursor m/z: highest total fragment intensity
    best: dict[float, dict] = {}

    with mzml.MzML(str(input_path)) as reader:
        for spectrum in reader:
            if spectrum.get("ms level") != 2:
                continue

            prec = spectrum.get("precursorList", {}).get("precursor", [{}])[0]
            sel = prec.get("selectedIonList", {}).get("selectedIon", [{}])[0]
            mz = round(sel.get("selected ion m/z", 0), 4)

            int_array = spectrum.get("intensity array", [])
            total_intensity = float(int_array.sum()) if len(int_array) else 0.0

            if mz not in best or total_intensity > best[mz]["total_intensity"]:
                rt_min = (
                    spectrum.get("scanList", {})
                    .get("scan", [{}])[0]
                    .get("scan start time", 0)
                )
                act = prec.get("activation", {})
                ce = act.get("collision energy", "")
                best[mz] = {
                    "total_intensity": total_intensity,
                    "meta": {
                        "compound": f"{input_path.stem}_mz{mz}",
                        "parentmass": mz,
                        "ionization": _ionization(spectrum),
                        "rt": f"{round(float(rt_min) * 60, 1)}s",
                    },
                    "spectra_by_ce": {
                        f"collision {ce} eV" if ce else "collision": list(
                            zip(
                                spectrum.get("m/z array", []).tolist(),
                                int_array.tolist(),
                            )
                        )
                    },
                }

    if not best:
        sys.exit(f"No MS2 spectra found in {input_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    for mz, data in best.items():
        _write_ms_file(output_dir / f"mz{mz}.ms", data["meta"], data["spectra_by_ce"])

    print(f"Wrote {len(best)} .ms files to {output_dir}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert raw MS files to mzML, MGF, or .ms."
    )
    parser.add_argument(
        "--input", required=True, help="Path to input file (.raw, .d, .mzML)"
    )
    parser.add_argument(
        "--output", required=True, help="Output file path, or directory for --format MS"
    )
    parser.add_argument(
        "--format",
        default="mzML",
        choices=["mzML", "MGF", "MS"],
        help="Output format (default: mzML)",
    )
    parser.add_argument(
        "--polarity",
        default="any",
        choices=["any", "positive", "negative"],
        help="Filter scans to one polarity during conversion (default: any, no filtering). "
        "Required for mixed-mode .raw files — run twice (positive, negative) to get two "
        "single-polarity mzML files before msms-feature-detect.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    fmt = args.format.lower()

    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    if fmt == "ms":
        if input_path.suffix.lower() != ".mzml":
            sys.exit("--format MS requires a .mzML input. Convert from .raw first.")
        convert_to_ms(input_path, output_path)
    else:
        if input_path.suffix.lower() in SUPPORTED_CONVERTED:
            print(f"Already in {fmt} format, passing through: {input_path}")
            print(input_path)
            return
        if input_path.suffix.lower() not in SUPPORTED_RAW:
            sys.exit(f"Unsupported input format: {input_path.suffix}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        convert_raw(input_path, output_path, fmt, args.polarity.lower())

    print(output_path)


if __name__ == "__main__":
    main()
