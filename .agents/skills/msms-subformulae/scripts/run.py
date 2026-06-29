"""
Assign per-peak subformulae to an MS/MS spectrum for use with FRIGID's MIST encoder.

Given a spectrum in ms-pred .ms format and the precursor molecular formula,
enumerate candidate fragment formulae for each MS2 peak using ms-pred's formula
assignment. Output is an HDF5 file readable by FRIGID's peakformula featurizer.

In de novo mode (unknown structure), omit --smiles to enumerate all formula subsets
from the precursor formula without requiring a known structure.

Usage:
    python run.py --spectrum sample.ms --formula C12H17NO3 --adduct "[M+H]+" \\
        --output-dir results/subformulae/

Requirements:
    - Env: denovo
    - ms-pred-dev installed (via msms-denovo setup_env.sh)
"""

# Env: denovo

import argparse
import json
import sys
from pathlib import Path

import yaml


def _setup_ms_pred(project_root: Path) -> Path:
    """Add ms-pred-dev to sys.path and return its root."""
    cfg_path = project_root / "configs" / "default.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    src = Path(cfg["models"]["simulator"]["ms_pred_src"]).expanduser()
    if not src.exists():
        raise FileNotFoundError(
            f"ms_pred_src not found: {src}\n"
            "Set models.simulator.ms_pred_src in configs/default.yaml."
        )
    ms_pred_src = str(src / "src")
    if ms_pred_src not in sys.path:
        sys.path.insert(0, ms_pred_src)
    return src


def assign_subformulae(
    spectrum_path: Path,
    formula: str,
    adduct: str,
    output_dir: Path,
    ms_pred_root: Path,
    smiles: str = "",
    use_all: bool = True,
    mass_diff_thresh: float = 10.0,
    mass_diff_type: str = "ppm",
    max_formulae: int = 50,
) -> Path:
    """
    Assign per-peak subformulae for a single spectrum, in-process.

    Returns path to the output HDF5 file.
    """
    import importlib
    import ms_pred.common as common

    # Force ms-pred-dev version of common (not FRIGID's submodule)
    importlib.reload(common)

    spec_name = spectrum_path.stem
    ms_content = spectrum_path.read_text()

    # parse_spectra returns (metadata, CompositeMassSpec)
    meta, comp_spec = common.parse_spectra(ms_content.split("\n"))
    comp_spec.process_spec_file(
        parentmass=meta.get("parentmass"),
    )

    # get_output_dict lives in the data_scripts — import it directly
    subform_script = (
        ms_pred_root / "data_scripts" / "forms" / "01_assign_subformulae.py"
    )
    import importlib.util

    spec_mod = importlib.util.spec_from_file_location(
        "assign_subformulae_mod", subform_script
    )
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    get_output_dict = mod.get_output_dict

    out_dir = output_dir / "default_subformulae"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect peaks across all CEs, then merge into one JSON keyed by spec stem.
    # FRIGID's featurizer reads one file per spectrum (caffeine.json), not per-CE.
    all_mz, all_inten = [], []
    cand_ion = adduct

    for ce, spec_obj in comp_spec.items():
        spec_arr = spec_obj.spec if hasattr(spec_obj, "spec") else spec_obj
        result = get_output_dict(
            spec_name=spec_name,
            spec=spec_arr,
            form=formula,
            mass_diff_type=mass_diff_type,
            mass_diff_thresh=mass_diff_thresh,
            inten_thresh=0.001,
            adduct_type=adduct,
            max_formulae=max_formulae,
            use_all=use_all,
            smiles=smiles,
            use_magma=False,
        )
        cand_ion = result["cand_ion"]
        tbl = result.get("output_tbl")
        if tbl:
            all_mz.extend(tbl["mz"])
            all_inten.extend(tbl["ms2_inten"])

    merged = {
        "cand_form": formula,
        "spec_name": spec_name,
        "cand_ion": cand_ion,
        "output_tbl": {
            "mz": all_mz,
            "ms2_inten": all_inten,
            "rel_inten": all_inten,
            "mono_mass": all_mz,
            "formula_mass_no_adduct": all_mz,
            "mass_diff": [0.0] * len(all_mz),
            "formula": [""] * len(all_mz),
            # ions must be ionization strings (e.g. "[M+H]+"), not masses
            "ions": [adduct] * len(all_mz),
        }
        if all_mz
        else None,
    }

    out_path = out_dir / f"{spec_name}.json"
    out_path.write_text(json.dumps(merged, indent=4))
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign per-peak subformulae to an MS/MS spectrum for FRIGID input."
    )
    parser.add_argument(
        "--spectrum", required=True, help="Spectrum in ms-pred .ms format"
    )
    parser.add_argument(
        "--formula", required=True, help="Precursor molecular formula (e.g. C12H17NO3)"
    )
    parser.add_argument(
        "--adduct", default="[M+H]+", help="Ionization adduct (default: [M+H]+)"
    )
    parser.add_argument(
        "--smiles",
        default="",
        help="Known SMILES (omit for de novo; enables --use-all formula enumeration)",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory to write subformulae output"
    )
    parser.add_argument(
        "--mass-diff-thresh",
        type=float,
        default=10.0,
        help="Peak-formula tolerance in ppm",
    )
    parser.add_argument(
        "--max-formulae", type=int, default=50, help="Max candidate formulae per peak"
    )
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent.parent.parent
    ms_pred_root = _setup_ms_pred(project_root)
    use_all = not args.smiles

    out = assign_subformulae(
        spectrum_path=Path(args.spectrum),
        formula=args.formula,
        adduct=args.adduct,
        output_dir=Path(args.output_dir),
        ms_pred_root=ms_pred_root,
        smiles=args.smiles,
        use_all=use_all,
        mass_diff_thresh=args.mass_diff_thresh,
        max_formulae=args.max_formulae,
    )

    (Path(args.output_dir) / "input_configs.yaml").write_text(yaml.dump(vars(args)))
    print(out)


if __name__ == "__main__":
    main()
