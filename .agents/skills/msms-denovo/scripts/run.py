"""
De novo structure prediction from an experimental MS/MS spectrum using FRIGID.

FRIGID generates candidate SMILES via masked diffusion (FRIGID-base), then
optionally refines them with ICEBERG-guided inference-time scaling.

Pipeline:
  1. Build a minimal FRIGID-compatible dataset dir from the single spectrum.
  2. Run FRIGID's spec2mol_scaling.py as a subprocess (benchmark mode, 1 spectrum).
  3. Parse and return ranked candidates.

Input spectrum must be in ms-pred .ms format. Subformulae must be pre-computed
with msms-subformulae (or pass --subform-dir to an existing subformulae folder).

Usage:
    python run.py --spectrum sample.ms --formula C12H17NO3 --adduct "[M+H]+" \\
        --subform-dir results/subformulae/ --output results/denovo_output.json

Requirements:
    - Env: denovo
    - FRIGID installed via setup_env.sh
    - Subformulae pre-computed via msms-subformulae skill
    - Checkpoint paths set in configs/default.yaml
"""

# Env: denovo

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def load_config(project_root: Path, config_rel: str = "configs/default.yaml") -> dict:
    with open(project_root / config_rel) as f:
        return yaml.safe_load(f)


def resolve(p: str, root: Path) -> Path:
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else root / path


def build_dataset_dir(
    tmp: Path,
    spectrum_path: Path,
    subform_dir: Path,
    formula: str,
    adduct: str,
    instrument: str,
) -> Path:
    """
    Construct a minimal FRIGID-compatible dataset directory for a single spectrum.

    Layout expected by spec2mol_scaling.py:
      data/
        labels.tsv
        split.tsv
        spec_files/<name>.ms
        subformulae/default_subformulae/   ← symlink or copy from subform_dir
    """
    spec_name = spectrum_path.stem
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True)

    # FRIGID reads loose .ms files from spec_files/
    spec_files_dir = data_dir / "spec_files"
    spec_files_dir.mkdir()
    shutil.copy2(spectrum_path, spec_files_dir / spectrum_path.name)

    # labels.tsv — SMILES column required by parser; use placeholder for de novo
    (data_dir / "labels.tsv").write_text(
        "spec\tformula\tionization\tsmiles\tinstrument\n"
        f"{spec_name}\t{formula}\t{adduct}\tC\t{instrument}\n"
    )

    # split.tsv — FRIGID expects a "name" column (not "spec")
    (data_dir / "split.tsv").write_text(f"name\tsplit\n{spec_name}\ttest\n")
    # Upstream uses labels SMILES for evaluation. The BUDDY file supplies the
    # actual unknown formula for generation instead of the placeholder SMILES.
    (data_dir / "buddy_formulas.csv").write_text(
        f"identifier,formula_rank_1\n{spec_name},{formula}\n"
    )

    # subformulae — symlink the folder produced by msms-subformulae
    subform_dst = data_dir / "subformulae" / "default_subformulae"
    subform_dst.parent.mkdir(parents=True)
    subform_src = (subform_dir / "default_subformulae").resolve()
    if not subform_src.exists():
        raise FileNotFoundError(
            f"Subformulae not found at {subform_src}.\n"
            "Run msms-subformulae skill first:\n"
            "  python "
            ".agents/skills/msms-subformulae/scripts/run.py --spectrum ... --formula ..."
        )
    subform_dst.symlink_to(subform_src)

    return data_dir


def run_frigid(
    data_dir: Path,
    frigid_python: str,
    frigid_dir: Path,
    cfg: dict,
    project_root: Path,
    output_dir: Path,
    num_rounds: int,
    batch_size: int,
) -> list[dict]:
    """Call spec2mol_scaling.py for the single-spectrum dataset, parse output."""
    denovo = cfg["models"]["denovo"]
    frigid_cfg = frigid_dir / "configs" / "spec2mol_benchmark_canopus.yaml"
    if not frigid_cfg.exists():
        raise FileNotFoundError(f"FRIGID config not found: {frigid_cfg}")

    for key in ("mist_ckpt", "dlm_ckpt"):
        path = resolve(denovo.get(key, ""), project_root)
        if not denovo.get(key) or not path.is_file():
            raise FileNotFoundError(f"Missing {key}: supply --{key.replace('_', '-')} or download public FRIGID assets")
    if num_rounds > 0:
        for key in ("iceberg_gen_ckpt", "iceberg_inten_ckpt"):
            path = resolve(denovo.get(key, ""), project_root)
            if not denovo.get(key) or not path.is_file():
                raise FileNotFoundError(f"Missing {key} for ICEBERG refinement")

    cmd = [
        frigid_python,
        str(frigid_dir / "scripts" / "spec2mol_scaling.py"),
        "--config",
        str(frigid_cfg),
        "--mist-checkpoint",
        str(resolve(denovo["mist_ckpt"], project_root)),
        "--dlm-checkpoint",
        str(resolve(denovo["dlm_ckpt"], project_root)),
        "--data-dir",
        str(data_dir),
        "--split",
        "test",
        "--max-spectra",
        "1",
        "--num-rounds",
        str(num_rounds),
        "--batch-size",
        str(batch_size),
        "--buddy-formula-path",
        str(data_dir / "buddy_formulas.csv"),
        "--output-dir",
        str(output_dir),
        "--seed",
        "42",
    ]
    # Upstream counts the initial generation as round 1 and requires at least
    # one round. Zero requested refinement rounds therefore means one base round.
    cmd[cmd.index("--num-rounds") + 1] = str(num_rounds + 1)
    if num_rounds == 0:
        cmd += ["--num-unique-to-refine", "0", "--masks-per-molecule", "0"]
    if num_rounds > 0:
        cmd += ["--iceberg-gen-ckpt", str(resolve(denovo["iceberg_gen_ckpt"], project_root)),
                "--iceberg-inten-ckpt", str(resolve(denovo["iceberg_inten_ckpt"], project_root))]

    cuda = denovo.get("cuda_devices")
    env = os.environ.copy()
    if cuda:
        # spec2mol_scaling.py hardcodes device "cuda" (i.e. cuda:0) with no device
        # flag of its own, so remap the configured GPU to index 0 via visibility.
        cmd += ["--iceberg-gpu"] + str(cuda).split(",")
        env["CUDA_VISIBLE_DEVICES"] = str(cuda).split(",")[0]

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"FRIGID failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    # FRIGID writes a CSV with pred_smiles_1, pred_smiles_2, ... columns
    pred_csvs = [output_dir / "predictions.csv"] if (output_dir / "predictions.csv").exists() else sorted(output_dir.rglob("predictions_round_*.csv"), reverse=True)
    pred_jsons = list(output_dir.rglob("predictions*.json"))

    candidates = []
    if pred_csvs:
        import csv

        with open(pred_csvs[0]) as f:
            rows = list(csv.DictReader(f))
        if rows:
            row = rows[0]
            i = 1
            while f"pred_smiles_{i}" in row and row[f"pred_smiles_{i}"]:
                candidates.append({"smiles": row[f"pred_smiles_{i}"], "rank": i})
                i += 1
    elif pred_jsons:
        data = json.loads(pred_jsons[0].read_text())
        for entry in data if isinstance(data, list) else [data]:
            smiles = entry.get("smiles") or entry.get("pred_smiles")
            score = (
                entry.get("score")
                or entry.get("tanimoto")
                or entry.get("fp_sim")
                or 0.0
            )
            if smiles:
                candidates.append({"smiles": smiles, "score": float(score)})
    else:
        raise FileNotFoundError(
            f"FRIGID produced no predictions CSV/JSON under {output_dir}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    return candidates


def annotate_formula(candidates: list[dict], target_formula: str) -> list[dict]:
    """Add formula_match flag to each candidate and sort matches first."""
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
        from rdkit import RDLogger

        RDLogger.DisableLog("rdApp.*")
    except ImportError:
        return candidates

    annotated = []
    for c in candidates:
        mol = Chem.MolFromSmiles(c["smiles"])
        formula = rdMolDescriptors.CalcMolFormula(mol) if mol else None
        annotated.append(
            {**c, "formula": formula, "formula_match": formula == target_formula}
        )

    matched = [c for c in annotated if c["formula_match"]]
    unmatched = [c for c in annotated if not c["formula_match"]]
    return matched + unmatched


def main() -> None:
    parser = argparse.ArgumentParser(
        description="De novo structure prediction from MS/MS spectrum using FRIGID."
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
        "--subform-dir",
        required=True,
        help="Directory containing default_subformulae/ from msms-subformulae skill",
    )
    parser.add_argument(
        "--instrument", default="Orbitrap", help="Instrument type (default: Orbitrap)"
    )
    parser.add_argument("--top-k", type=int, default=10, help="Candidates to return")
    parser.add_argument(
        "--num-rounds",
        type=int,
        default=None,
        help="ICEBERG refinement rounds (0 = base only)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None, help="Samples per round"
    )
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--frigid-dir", help="FRIGID checkout; overrides config")
    parser.add_argument("--frigid-python", help="FRIGID environment Python")
    parser.add_argument("--mist-ckpt", help="MIST checkpoint")
    parser.add_argument("--dlm-ckpt", help="DLM checkpoint")
    parser.add_argument("--iceberg-gen-ckpt", help="ICEBERG generator checkpoint")
    parser.add_argument("--iceberg-inten-ckpt", help="ICEBERG intensity checkpoint")
    parser.add_argument("--cuda-devices", help="GPU IDs, for example 0; overrides config")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent.parent.parent
    cfg = load_config(project_root, args.config)
    denovo_cfg = cfg["models"]["denovo"]

    for arg_name, config_name in (("mist_ckpt", "mist_ckpt"), ("dlm_ckpt", "dlm_ckpt"),
                                  ("iceberg_gen_ckpt", "iceberg_gen_ckpt"),
                                  ("iceberg_inten_ckpt", "iceberg_inten_ckpt")):
        value = getattr(args, arg_name)
        if value:
            denovo_cfg[config_name] = value
    if args.cuda_devices is not None:
        denovo_cfg["cuda_devices"] = args.cuda_devices or None
    frigid_dir = resolve(args.frigid_dir or denovo_cfg["frigid_src"], project_root)
    frigid_python = args.frigid_python or str(project_root / ".cache/frigid-venv/bin/python")
    if not Path(frigid_python).is_file():
        raise FileNotFoundError(f"FRIGID Python missing: {frigid_python}. Run the denovo setup script or pass --frigid-python.")
    if not frigid_dir.exists():
        raise FileNotFoundError(
            f"frigid_src not found: {frigid_dir}\n"
            "Set models.denovo.frigid_src in configs/default.yaml."
        )

    num_rounds = (
        args.num_rounds
        if args.num_rounds is not None
        else denovo_cfg.get("num_rounds", 2)
    )
    batch_size = (
        args.batch_size
        if args.batch_size is not None
        else denovo_cfg.get("batch_size", 128)
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frigid_output = output_path.parent / "frigid_raw"
    frigid_output.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = build_dataset_dir(
            tmp=Path(tmp),
            spectrum_path=Path(args.spectrum),
            subform_dir=Path(args.subform_dir),
            formula=args.formula,
            adduct=args.adduct,
            instrument=args.instrument,
        )

        candidates = run_frigid(
            data_dir=data_dir,
            frigid_python=frigid_python,
            frigid_dir=frigid_dir,
            cfg=cfg,
            project_root=project_root,
            output_dir=frigid_output,
            num_rounds=num_rounds,
            batch_size=batch_size,
        )

    candidates = annotate_formula(candidates, args.formula)
    candidates = candidates[: args.top_k]

    n_matched = sum(1 for c in candidates if c.get("formula_match"))
    if n_matched == 0:
        print(
            f"Warning: 0/{len(candidates)} candidates match formula {args.formula}. "
            "GPU inference recommended for meaningful results.",
            file=sys.stderr,
        )

    result = {
        "query_spectrum": args.spectrum,
        "formula": args.formula,
        "adduct": args.adduct,
        "top_k": args.top_k,
        "num_rounds": num_rounds,
        "candidates": candidates,
    }

    output_path.write_text(json.dumps(result, indent=2))
    (output_path.parent / "input_configs.yaml").write_text(yaml.dump(vars(args)))

    grid_path = output_path.with_suffix(".png")
    _draw_candidates(candidates, args.formula, grid_path)

    print(output_path)


def _draw_candidates(candidates: list[dict], formula: str, out_path: Path) -> None:
    """Save a grid image of top candidates with rank, formula match, and SMILES."""
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw
        from rdkit.Chem.Draw import rdMolDraw2D
        from PIL import Image
        import io
    except ImportError:
        return

    mols, legends = [], []
    for i, c in enumerate(candidates, 1):
        mol = Chem.MolFromSmiles(c["smiles"])
        if mol is None:
            mol = Chem.MolFromSmiles("C")  # placeholder for unparseable
        mols.append(mol)
        match = "✓" if c.get("formula_match") else "✗"
        legends.append(f"#{i} {match} {c['smiles'][:30]}")

    n = len(mols)
    cols = min(5, n)
    rows = (n + cols - 1) // cols
    img = Draw.MolsToGridImage(
        mols,
        molsPerRow=cols,
        subImgSize=(300, 250),
        legends=legends,
        returnPNG=False,
    )
    img.save(str(out_path))
    print(f"Grid image: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
