#!/usr/bin/env python3
"""
Predict molecular fingerprints from MS/MS spectra using only FRIGID's MIST
encoder — no diffusion generation, no ICEBERG refinement.

Builds the same minimal FRIGID-compatible dataset dir as msms-denovo/run.py,
then runs just the MIST forward pass on each spectrum to get a predicted
4096-bit Morgan-like fingerprint (sigmoid probabilities + thresholded binary).

Usage:
    # Env: denovo
    python .agents/skills/msms-mist-fingerprint/scripts/run.py \\
        --ms-dir results/atlas_lookup_1k/ms_files \\
        --formulae-csv .agents/test/uspto_sample1k.csv \\
        --adduct "[M+H]+" \\
        --instrument "Orbitrap (LCMS)" \\
        --output results/atlas_lookup_1k/mist_fingerprints.hdf5 \\
        --failure-log results/atlas_lookup_1k/mist_failures.log

Requirements:
    - Env: denovo
    - FRIGID installed via msms-denovo/scripts/setup_env.sh
    - .ms files + per-spectrum subformulae already computed
      (msms-atlas-to-ms + msms-subformulae/run_batch.py)
    - MIST checkpoint path set in configs/default.yaml (models.denovo.mist_ckpt)
"""

# Env: denovo

import argparse
import csv
import shutil
import sys
from pathlib import Path

import h5py
import numpy as np
import torch
import yaml
from rdkit import Chem
from rdkit import RDLogger
from rdkit.Chem import rdMolDescriptors

RDLogger.DisableLog("rdApp.*")


def resolve(p: str, root: Path) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else root / path


def load_mist_encoder_fixed(encoder_cls, config: dict, device) -> torch.nn.Module:
    """
    Load the MIST encoder, working around a bug in FRIGID's own
    spec2mol_scaling.load_mist_encoder: it strips an "encoder." prefix from
    checkpoint state_dict keys, but mist_msg.ckpt actually uses a
    "spectra_encoder." prefix. The upstream code's fallback silently loads
    the raw (wrongly-prefixed) state_dict with strict=False, matching 0/35
    keys — i.e. the model runs with random init and no error is raised.
    """
    checkpoint_path = config["checkpoint"]
    encoder = encoder_cls(
        form_embedder=config.get("form_embedder", "pos-cos"),
        output_size=config.get("output_size", 4096),
        hidden_size=config.get("hidden_size", 512),
        spectra_dropout=config.get("spectra_dropout", 0.1),
        peak_attn_layers=config.get("peak_attn_layers", 2),
        num_heads=config.get("num_heads", 8),
        set_pooling=config.get("set_pooling", "cls"),
        refine_layers=config.get("refine_layers", 4),
        pairwise_featurization=config.get("pairwise_featurization", True),
        embed_instrument=config.get("embed_instrument", False),
        inten_transform=config.get("inten_transform", "float"),
        magma_modulo=config.get("magma_modulo", 2048),
        inten_prob=config.get("inten_prob", 0.1),
        remove_prob=config.get("remove_prob", 0.5),
        cls_type=config.get("cls_type", "ms1"),
        spec_features=config.get("spec_features", "peakformula"),
        mol_features=config.get("mol_features", "fingerprint"),
        top_layers=config.get("top_layers", 1),
    )

    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    raw_state_dict = (
        checkpoint["state_dict"] if "state_dict" in checkpoint else checkpoint
    )

    model_keys = set(encoder.state_dict().keys())
    stripped = {
        k.replace("spectra_encoder.", "", 1): v
        for k, v in raw_state_dict.items()
        if k.startswith("spectra_encoder.")
    }
    matched = model_keys & set(stripped.keys())
    if len(matched) < 0.9 * len(model_keys):
        raise RuntimeError(
            f"MIST checkpoint prefix mismatch: only {len(matched)}/{len(model_keys)} keys matched "
            "after stripping 'spectra_encoder.' — checkpoint format may have changed."
        )

    missing_before_load = model_keys - set(stripped.keys())
    encoder.load_state_dict(stripped, strict=False)
    print(
        f"Loaded MIST encoder weights: {len(matched)}/{len(model_keys)} keys matched "
        f"({len(missing_before_load)} left at init: {sorted(missing_before_load)})"
    )

    encoder = encoder.to(device)
    encoder.eval()
    return encoder


def build_dataset_dir(
    tmp: Path,
    ms_dir: Path,
    smiles_formula_map: dict,
    subform_dir: Path,
    adduct: str,
    instrument: str,
) -> Path:
    """Build a minimal FRIGID-compatible dataset dir covering every .ms file in ms_dir."""
    data_dir = tmp / "data"
    spec_files_dir = data_dir / "spec_files"
    spec_files_dir.mkdir(parents=True)

    labels_rows = ["spec\tformula\tionization\tsmiles\tinstrument"]
    split_rows = ["name\tsplit"]
    n_included = 0

    for ms_path in sorted(ms_dir.glob("*.ms")):
        inchikey14 = ms_path.stem
        rec = smiles_formula_map.get(inchikey14)
        if rec is None:
            continue
        shutil.copy2(ms_path, spec_files_dir / ms_path.name)
        labels_rows.append(
            f"{inchikey14}\t{rec['formula']}\t{adduct}\t{rec['smiles']}\t{instrument}"
        )
        split_rows.append(f"{inchikey14}\ttest")
        n_included += 1

    (data_dir / "labels.tsv").write_text("\n".join(labels_rows) + "\n")
    (data_dir / "split.tsv").write_text("\n".join(split_rows) + "\n")

    subform_dst = data_dir / "subformulae" / "default_subformulae"
    subform_dst.parent.mkdir(parents=True)
    subform_src = (subform_dir / "default_subformulae").resolve()
    if not subform_src.exists():
        raise FileNotFoundError(f"Subformulae not found at {subform_src}")
    subform_dst.symlink_to(subform_src)

    print(f"Dataset dir built: {n_included} spectra")
    return data_dir


def load_smiles_formula_map(csv_path: Path) -> dict:
    """Map inchikey14 -> {smiles, formula} from a lookup CSV."""
    out = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            smiles = row["smiles"].strip()
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            inchikey14 = Chem.MolToInchiKey(mol)[:14]
            out[inchikey14] = {
                "smiles": smiles,
                "formula": rdMolDescriptors.CalcMolFormula(mol),
            }
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MIST-only fingerprint prediction from MS/MS spectra"
    )
    p.add_argument(
        "--ms-dir", required=True, type=Path, help="Directory of <inchikey14>.ms files"
    )
    p.add_argument(
        "--formulae-csv", required=True, type=Path, help="CSV with a smiles column"
    )
    p.add_argument(
        "--subform-dir",
        required=True,
        type=Path,
        help="Output dir from msms-subformulae/run_batch.py",
    )
    p.add_argument("--adduct", default="[M+H]+")
    p.add_argument("--instrument", default="Orbitrap (LCMS)")
    p.add_argument(
        "--fp-threshold",
        type=float,
        default=0.172,
        help="Binarization threshold (FRIGID default)",
    )
    p.add_argument("--config", default="configs/default.yaml")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--failure-log", required=True, type=Path)
    return p.parse_args()


def main() -> None:
    import tempfile

    args = parse_args()
    project_root = Path(__file__).parent.parent.parent.parent.parent
    with open(project_root / args.config) as f:
        cfg = yaml.safe_load(f)
    denovo_cfg = cfg["models"]["denovo"]

    frigid_dir = resolve(denovo_cfg["frigid_src"], project_root)
    if not frigid_dir.exists():
        raise FileNotFoundError(f"frigid_src not found: {frigid_dir}")
    sys.path.insert(0, str(frigid_dir / "scripts"))
    sys.path.insert(0, str(frigid_dir / "src"))

    import spec2mol_scaling as s2m

    mist_ckpt = resolve(denovo_cfg["mist_ckpt"], project_root)
    if not mist_ckpt.exists():
        raise FileNotFoundError(f"mist_ckpt not found: {mist_ckpt}")

    device = torch.device(
        f"cuda:{denovo_cfg['cuda_devices']}"
        if denovo_cfg.get("cuda_devices")
        else "cpu"
    )

    encoder_config = s2m.get_default_config()["mist_encoder"]
    encoder_config["checkpoint"] = str(mist_ckpt)
    # MSG Large Model dims (matches configs/spec2mol_benchmark_msg.yaml, since
    # mist_ckpt here is mist_msg.ckpt, not the smaller CANOPUS checkpoint)
    encoder_config["hidden_size"] = 640
    encoder_config["magma_modulo"] = 2048

    smiles_formula_map = load_smiles_formula_map(args.formulae_csv)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = build_dataset_dir(
            tmp=Path(tmp),
            ms_dir=args.ms_dir,
            smiles_formula_map=smiles_formula_map,
            subform_dir=args.subform_dir,
            adduct=args.adduct,
            instrument=args.instrument,
        )

        mist_encoder = load_mist_encoder_fixed(
            s2m.SpectraEncoderGrowing, encoder_config, device
        )

        dataset, split_data, _, _, spec_to_formula = s2m.load_spec_data(
            config={
                "datadir": str(data_dir),
                "labels_file": str(data_dir / "labels.tsv"),
                "split_file": str(data_dir / "split.tsv"),
                "spec_folder": str(data_dir / "spec_files"),
                "subform_folder": str(data_dir / "subformulae" / "default_subformulae"),
            },
            encoder_config=encoder_config,
            split="test",
        )

        dataloader = s2m.get_paired_loader(
            dataset, shuffle=False, batch_size=1, num_workers=0
        )

        inchikey14_out, smiles_out, fp_probs_out, fp_binary_out = [], [], [], []
        n_ok, n_failed = 0, 0

        # MIST hard-filters unsupported atoms (only C,O,P,N,S,Cl,F,H) inside
        # get_paired_spectra; any input molecule silently dropped there is
        # otherwise invisible, so log those misses too.
        loaded_inchikeys = {spec.get_spec_name() for spec, _ in split_data}
        with open(args.failure_log, "w") as fail_log:
            for ms_path in sorted(args.ms_dir.glob("*.ms")):
                ikey = ms_path.stem
                if ikey in smiles_formula_map and ikey not in loaded_inchikeys:
                    n_failed += 1
                    fail_log.write(
                        f"{ikey}\tMIST unsupported atom (only C,O,P,N,S,Cl,F,H allowed)\n"
                    )

            for idx, batch in enumerate(dataloader):
                spec, mol = split_data[idx]
                spec_name = spec.get_spec_name()
                try:
                    batch = {
                        k: v.to(device) if isinstance(v, torch.Tensor) else v
                        for k, v in batch.items()
                    }
                    with torch.no_grad():
                        pred_fp_probs, _ = mist_encoder(batch)
                        pred_fp_probs = pred_fp_probs.cpu().numpy()[0]
                    pred_fp_binary = s2m.binarize_fingerprint(
                        pred_fp_probs, args.fp_threshold
                    )

                    inchikey14_out.append(spec_name)
                    smiles_out.append(mol.get_smiles())
                    fp_probs_out.append(pred_fp_probs.astype(np.float32))
                    fp_binary_out.append(pred_fp_binary.astype(np.uint8))
                    n_ok += 1
                except Exception as e:
                    n_failed += 1
                    fail_log.write(f"{spec_name}\t{e}\n")

                if (idx + 1) % 50 == 0:
                    print(
                        f"{idx + 1}/{len(dataset)} processed (ok={n_ok}, failed={n_failed})",
                        flush=True,
                    )

    with h5py.File(args.output, "w") as h5:
        h5.create_dataset(
            "inchikey14", data=np.array(inchikey14_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset(
            "smiles", data=np.array(smiles_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset(
            "fp_probs",
            data=np.stack(fp_probs_out)
            if fp_probs_out
            else np.zeros((0, 4096), dtype=np.float32),
        )
        h5.create_dataset(
            "fp_binary",
            data=np.stack(fp_binary_out)
            if fp_binary_out
            else np.zeros((0, 4096), dtype=np.uint8),
        )

    print(f"\nDone. {n_ok} fingerprints predicted, {n_failed} failed.")
    print(f"Output: {args.output}")
    print(f"Failures: {args.failure_log}")


if __name__ == "__main__":
    main()
