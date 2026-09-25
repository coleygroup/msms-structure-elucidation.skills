"""
Run MZmine 4 headless batch processing on an mzML file to detect LC-MS features
and export per-feature averaged MS2 spectra in SIRIUS/.mgf format.

Usage:
    python run.py --input sample.mzML --output-dir results/

Requirements:
    - Env: preprocess (Python side only; MZmine itself is a separate Java binary)
    - MZmine 4 binary, path set via --mzmine-bin or MZMINE_BIN env var
    - MZmine user login file (.mzuser), path set via --mzmine-user or MZMINE_USER env var.
      Generate with: <mzmine-bin> -login-console
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

BATCH_TEMPLATE = Path(__file__).parent.parent / "resources" / "batch_template.xml"


def _find_mzmine(cli_path: str | None) -> str:
    """Resolve the MZmine binary from --mzmine-bin, MZMINE_BIN env var, or PATH."""
    for candidate in (cli_path, os.environ.get("MZMINE_BIN")):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit(
        "MZmine binary not found. Pass --mzmine-bin or set MZMINE_BIN in .env "
        "to the MZmine 4 'bin/mzmine' executable."
    )


def _find_mzmine_user(cli_path: str | None) -> str:
    """Resolve the MZmine .mzuser login file from --mzmine-user or MZMINE_USER env var."""
    for candidate in (cli_path, os.environ.get("MZMINE_USER")):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit(
        "MZmine user file not found. Pass --mzmine-user or set MZMINE_USER in .env "
        "to a .mzuser file generated via '<mzmine-bin> -login-console'."
    )


def load_sample_config(config_path: Path) -> dict:
    """Load a per-sample YAML config: input path, free-text notes, optional manual rt_range."""
    import yaml

    return yaml.safe_load(config_path.read_text())


def detect_rt_range(input_path: Path, margin: float) -> tuple[float, float]:
    """Read the file's own first/last MS1 scan RT (minutes), padded by margin."""
    suffix = input_path.suffix.lower()
    rts: list[float] = []
    if suffix == ".mzml":
        from pyteomics import mzml

        with mzml.MzML(str(input_path)) as reader:
            for spectrum in reader:
                scan = spectrum.get("scanList", {}).get("scan", [{}])[0]
                rt = scan.get("scan start time")
                if rt is not None:
                    rts.append(float(rt))
    elif suffix == ".mgf":
        from pyteomics import mgf

        with mgf.MGF(str(input_path)) as reader:
            for spectrum in reader:
                rt = spectrum.get("params", {}).get("rtinseconds")
                if rt is not None:
                    rts.append(float(rt) / 60.0)
    if not rts:
        sys.exit(f"Could not read any retention times from {input_path}")
    return max(0.0, min(rts) - margin), max(rts) + margin


def build_batch_xml(
    input_file: Path,
    output_mgf: Path,
    ms1_noise_level: float,
    ms2_noise_level: float,
    mz_tol_abs: float,
    mz_tol_ppm: float,
    min_consecutive_scans: int,
    min_consecutive_intensity: float,
    min_feature_height: float,
    isolation_window_width: float,
    rt_crop_min: float,
    rt_crop_max: float,
    polarity: str,
) -> str:
    """Fill the batch XML template with run parameters."""
    template = BATCH_TEMPLATE.read_text()
    return (
        template.replace("{INPUT_MZML}", str(input_file))
        .replace("{OUTPUT_MGF}", str(output_mgf))
        .replace("{POLARITY}", polarity)
        .replace("{MS1_NOISE_LEVEL}", str(ms1_noise_level))
        .replace("{MS2_NOISE_LEVEL}", str(ms2_noise_level))
        .replace("{MZ_TOL_ABS}", str(mz_tol_abs))
        .replace("{MZ_TOL_PPM}", str(mz_tol_ppm))
        .replace("{MIN_CONSECUTIVE_SCANS}", str(min_consecutive_scans))
        .replace("{MIN_CONSECUTIVE_INTENSITY}", str(min_consecutive_intensity))
        .replace("{MIN_FEATURE_HEIGHT}", str(min_feature_height))
        .replace("{ISOLATION_WINDOW_WIDTH}", str(isolation_window_width))
        .replace("{RT_CROP_MIN}", str(rt_crop_min))
        .replace("{RT_CROP_MAX}", str(rt_crop_max))
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect LC-MS features and export averaged MS2 spectra via MZmine."
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Path to input .mzML or .mgf file (or set via --config)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a per-sample YAML config with 'input' path, free-text 'notes', "
        "and optional manual 'rt_range: [min, max]' (minutes) overriding auto-detection",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for the batch XML and .mgf export",
    )
    parser.add_argument(
        "--mzmine-bin", default=None, help="Path to MZmine 4 'bin/mzmine' executable"
    )
    parser.add_argument(
        "--mzmine-user",
        default=None,
        help="Path to a .mzuser login file (generate via '<mzmine-bin> -login-console')",
    )
    parser.add_argument(
        "--ms1-noise-level", type=float, default=1000.0, help="MS1 centroid noise floor"
    )
    parser.add_argument(
        "--ms2-noise-level", type=float, default=100.0, help="MS2 centroid noise floor"
    )
    parser.add_argument(
        "--mz-tol-abs",
        type=float,
        default=0.02,
        help="Absolute m/z tolerance (Da) — widen for instruments with scan-to-scan mass drift",
    )
    parser.add_argument(
        "--mz-tol-ppm", type=float, default=10.0, help="Relative m/z tolerance (ppm)"
    )
    parser.add_argument(
        "--min-consecutive-scans",
        type=int,
        default=4,
        help="Minimum consecutive MS1 scans to call a chromatogram",
    )
    parser.add_argument(
        "--min-consecutive-intensity",
        type=float,
        default=5000.0,
        help="Minimum intensity for consecutive-scan detection",
    )
    parser.add_argument(
        "--min-feature-height",
        type=float,
        default=500000.0,
        help="Minimum absolute feature height to keep",
    )
    parser.add_argument(
        "--isolation-window-width",
        type=float,
        default=4.0,
        help="Precursor isolation window width (Da) used when merging MS2 across scans",
    )
    parser.add_argument(
        "--rt-crop-margin",
        type=float,
        default=0.1,
        help="Minutes of padding added around the file's own [min, max] scan RT for cropping",
    )
    parser.add_argument(
        "--polarity",
        default="Any",
        choices=["Any", "+", "-"],
        help="Restrict to one polarity (default: Any). Required for mixed-mode files — "
        "run this script twice ('+', '-') to get two correct feature sets. "
        "Must be the literal symbol '+'/'-' — the word forms 'Positive'/'Negative' and "
        "'POSITIVE'/'NEGATIVE' silently fall back to Any in this MZmine build.",
    )
    args = parser.parse_args()

    manual_rt_range = None
    input_arg = args.input
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            sys.exit(f"Config file not found: {config_path}")
        config = load_sample_config(config_path)
        input_arg = input_arg or config.get("input")
        manual_rt_range = config.get("rt_range")
        if config.get("notes"):
            print(f"Notes: {config['notes']}", file=sys.stderr)

    if not input_arg:
        sys.exit("Provide --input, or --config with an 'input' field.")

    input_path = Path(input_arg)
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")
    if input_path.suffix.lower() not in (".mzml", ".mgf"):
        sys.exit(
            "--input must be .mzML or .mgf. Run msms-preprocess first if starting from .raw."
        )

    mzmine_bin = _find_mzmine(args.mzmine_bin)
    mzmine_user = _find_mzmine_user(args.mzmine_user)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    polarity_names = {"Any": "", "+": "_pos", "-": "_neg"}
    polarity_suffix = polarity_names[args.polarity]
    output_mgf = output_dir / f"{input_path.stem}{polarity_suffix}_sirius.mgf"
    batch_xml_path = output_dir / f"{input_path.stem}{polarity_suffix}_batch.xml"

    if manual_rt_range:
        rt_crop_min, rt_crop_max = float(manual_rt_range[0]), float(manual_rt_range[1])
        print(
            f"Manual RT crop from config: [{rt_crop_min:.2f}, {rt_crop_max:.2f}] min",
            file=sys.stderr,
        )
    else:
        rt_crop_min, rt_crop_max = detect_rt_range(input_path, args.rt_crop_margin)
        print(
            f"Auto RT crop: [{rt_crop_min:.2f}, {rt_crop_max:.2f}] min "
            f"(margin {args.rt_crop_margin} min)",
            file=sys.stderr,
        )

    batch_xml_path.write_text(
        build_batch_xml(
            input_file=input_path.resolve(),
            output_mgf=output_mgf.resolve(),
            ms1_noise_level=args.ms1_noise_level,
            ms2_noise_level=args.ms2_noise_level,
            mz_tol_abs=args.mz_tol_abs,
            mz_tol_ppm=args.mz_tol_ppm,
            min_consecutive_scans=args.min_consecutive_scans,
            min_consecutive_intensity=args.min_consecutive_intensity,
            min_feature_height=args.min_feature_height,
            isolation_window_width=args.isolation_window_width,
            rt_crop_min=rt_crop_min,
            rt_crop_max=rt_crop_max,
            polarity=args.polarity,
        )
    )

    subprocess.run(
        [
            mzmine_bin,
            "-b",
            str(batch_xml_path),
            "-u",
            mzmine_user,
            "--ignore-parameter-warnings",
        ],
        check=True,
    )

    if not output_mgf.exists():
        sys.exit(f"MZmine finished but expected output not found: {output_mgf}")

    print(output_mgf)


if __name__ == "__main__":
    main()
