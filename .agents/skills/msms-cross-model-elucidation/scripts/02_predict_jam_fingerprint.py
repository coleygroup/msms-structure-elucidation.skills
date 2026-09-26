"""Stage 2 of the cross-model cascade: predict Morgan fingerprints with JAM.

Consumes the per-(molecule, collision energy) JSONs written by stage 1, converts
each into the subformula-assignment tree that `jam.data.featurizers.
PeakFormulaFeaturizer` expects, featurizes it, runs the JAM (MistNet) encoder to
get per-bit sigmoid probabilities, binarizes at a fixed threshold, and scores the
result against an RDKit Morgan fingerprint of the ground-truth SMILES.

JAM lives in a separate project, so this script is executed with that
project's interpreter (`$JAM_PYTHON`)
rather than one of this repo's envs. The stage-1 fragment formula strings are
re-emitted through `jam.chem.formula_to_vec`/`vec_to_formula` so the element
ordering matches jam's own convention (ms-pred orders elements differently).

Usage:
    # Env: JAM environment (not a msms-structure-elucidation Python env)
    $JAM_PYTHON \\
        .agents/skills/msms-cross-model-elucidation/scripts/02_predict_jam_fingerprint.py \\
        --spectra-dir results/<timestamp>/01_glacier/spectra \\
        --manifest results/<timestamp>/01_glacier/manifest.json \\
        --output-dir results/<timestamp>/02_jam
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import os

import numpy as np
import torch
import yaml

JAM_ROOT = Path(os.environ.get("JAM_DIR", "../jam")).resolve()
sys.path.insert(0, str(JAM_ROOT / "src"))

from jam import chem  # noqa: E402
from jam.data.datasets import SpectraDataModule, SpectraMolDataset  # noqa: E402
from jam.data.featurizers import get_paired_featurizer  # noqa: E402
from jam.models.base import get_model_cls  # noqa: E402
from jam.molecule import Molecule, Spectrum  # noqa: E402
from rdkit import Chem, RDLogger  # noqa: E402
from rdkit.Chem import rdFingerprintGenerator  # noqa: E402

RDLogger.DisableLog("rdApp.*")

DEFAULT_CHECKPOINT = Path(
    ".agents/skills/msms-cross-model-elucidation/resources/checkpoints/jam/best.ckpt"
)
ADDUCT = "[M+H]+"
# Documented project default, not tuned on these 11 molecules. jam.evaluate
# normally sweeps the threshold on a val split; there is no val split here, so a
# fixed mid-range value is used and reported as a caveat.
DEFAULT_THRESHOLD = 0.15


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the JAM fingerprint stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spectra-dir",
        required=True,
        help="Directory of stage-1 per-(molecule, collision energy) spectrum JSONs.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Stage-1 manifest.json listing molecule names, SMILES and spectra.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for the subformula trees, predicted fingerprints and metrics.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(DEFAULT_CHECKPOINT),
        help="JAM (MistNet) checkpoint (.ckpt).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Sigmoid probability threshold used to binarize the predicted fingerprint.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for JAM inference, e.g. cpu or cuda:0.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, help="JAM inference batch size."
    )
    return parser.parse_args()


def normalize_formula(formula: str) -> str:
    """Re-emit a formula string in jam's VALID_ELEMENTS order."""
    return chem.vec_to_formula(chem.formula_to_vec(formula))


def write_subform_tree(
    spectrum: dict, parent_formula: str, out_dir: Path
) -> tuple[Path, int]:
    """Write one jam subformula-assignment tree JSON; returns its path and peak count."""
    formulas, intensities = [], []
    for formula, intensity in zip(spectrum["frag_formulas"], spectrum["intensities"]):
        # Drop zero-intensity padding peaks and any fragment whose formula did not
        # survive vectorization (empty vector => empty string).
        normalized = normalize_formula(formula)
        if not normalized or intensity <= 0:
            continue
        formulas.append(normalized)
        intensities.append(float(intensity))

    peak_max = max(intensities) if intensities else 0.0
    if peak_max > 0:
        intensities = [value / peak_max for value in intensities]

    tree = {
        "output_tbl": {
            "formula": formulas,
            "ms2_inten": intensities,
            "ions": [ADDUCT] * len(formulas),
        },
        "cand_form": parent_formula,
        "cand_ion": ADDUCT,
    }
    out_path = out_dir / f"{spectrum['spec_name']}.json"
    out_path.write_text(json.dumps(tree, indent=1))
    return out_path, len(formulas)


def morgan_fingerprint(smiles: str, n_bits: int, radius: int) -> np.ndarray:
    """RDKit Morgan fingerprint of `smiles` as a float32 0/1 array."""
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)
    mol = Chem.MolFromSmiles(smiles)
    return np.array(generator.GetFingerprint(mol), dtype=np.float32)


def tanimoto(predicted: np.ndarray, target: np.ndarray) -> float:
    """Tanimoto similarity between two binary fingerprint vectors."""
    intersection = float(np.sum(predicted * target))
    union = float(np.sum(np.clip(predicted + target, 0, 1)))
    return intersection / union if union > 0 else 0.0


def main() -> None:
    """Run the JAM fingerprint prediction stage end to end."""
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    subform_dir = output_dir / "subformulae"
    subform_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(Path(args.manifest).read_text())
    molecules = {entry["name"]: entry["smiles"] for entry in manifest["molecules"]}

    model_cls = get_model_cls("MistNet")
    model = model_cls.load_from_checkpoint(
        str(Path(args.checkpoint).resolve()), map_location=args.device
    )
    model.eval()
    fingerprint_spec = model.hparams.fingerprints[0]
    n_bits = int(fingerprint_spec["n_bits"])
    radius = int(fingerprint_spec["radius"])
    print(
        f"Loaded MistNet with {fingerprint_spec['kind']} fingerprint, "
        f"n_bits={n_bits}, radius={radius}",
        flush=True,
    )

    with (output_dir / "input_configs.yaml").open("w") as handle:
        yaml.safe_dump(
            {
                **vars(args),
                "adduct": ADDUCT,
                "model_class": "MistNet",
                "fingerprint_kind": fingerprint_spec["kind"],
                "fingerprint_n_bits": n_bits,
                "fingerprint_radius": radius,
                "jam_root": str(JAM_ROOT),
                "spec_features": model_cls.spec_features(mode="test"),
                "mol_features": model_cls.mol_features(),
            },
            handle,
            sort_keys=True,
        )

    records, skipped = [], []
    for spec_path in sorted(Path(args.spectra_dir).glob("*.json")):
        spectrum = json.loads(spec_path.read_text())
        name = spectrum["name"]
        ground_truth_smiles = molecules[name]
        parent_mol = Molecule.from_smiles(ground_truth_smiles)
        if parent_mol is None:
            skipped.append(
                {"spec_name": spectrum["spec_name"], "reason": "rdkit_parse_failed"}
            )
            continue
        parent_formula = normalize_formula(parent_mol.formula)
        _, num_peaks = write_subform_tree(spectrum, parent_formula, subform_dir)
        if num_peaks == 0:
            skipped.append(
                {"spec_name": spectrum["spec_name"], "reason": "no_usable_peaks"}
            )
            continue
        records.append(
            {
                "spec_name": spectrum["spec_name"],
                "name": name,
                "collision_energy": spectrum["collision_energy"],
                "ground_truth_smiles": parent_mol.smiles,
                "parent_formula": parent_formula,
                "num_peaks": num_peaks,
            }
        )

    featurizer = get_paired_featurizer(
        spec_features=model_cls.spec_features(mode="test"),
        mol_features=model_cls.mol_features(),
        subform_folder=str(subform_dir),
        fingerprints=model.hparams.fingerprints,
        magma_aux_loss=False,
        inten_transform=model.hparams.inten_transform,
    )

    pairs = []
    for record in records:
        spectrum_obj = Spectrum(
            name=record["spec_name"],
            formula=record["parent_formula"],
            instrument="Orbitrap",
            collision_energy=float(record["collision_energy"]),
        )
        pairs.append(
            (spectrum_obj, Molecule.from_smiles(record["ground_truth_smiles"]))
        )

    dataset = SpectraMolDataset(pairs, featurizer)
    dataset.set_train_mode(False)
    loader = SpectraDataModule(
        train=dataset, val=dataset, test=dataset, batch_size=args.batch_size
    )._loader(dataset, shuffle=False)

    predicted_probs = []
    with torch.no_grad():
        for batch in loader:
            batch = model.transfer_batch_to_device(batch, model.device, 0)
            pred_fp, _ = model.encode_spectra(batch)
            predicted_probs.append(pred_fp.cpu().numpy())
    probabilities = np.concatenate(predicted_probs, axis=0)
    print(
        f"Predicted {probabilities.shape[0]} fingerprints of width "
        f"{probabilities.shape[1]}",
        flush=True,
    )

    binarized = (probabilities >= args.threshold).astype(np.float32)
    for index, record in enumerate(records):
        target = morgan_fingerprint(record["ground_truth_smiles"], n_bits, radius)
        record["fingerprint_tanimoto"] = tanimoto(binarized[index], target)
        record["num_predicted_bits"] = int(binarized[index].sum())
        record["num_true_bits"] = int(target.sum())

    np.savez_compressed(
        output_dir / "predicted_fingerprints.npz",
        spec_names=np.array([r["spec_name"] for r in records]),
        pred_fp_probs=probabilities,
        pred_fp_bits=binarized,
        threshold=args.threshold,
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(
            {
                "threshold": args.threshold,
                "n_bits": n_bits,
                "radius": radius,
                "records": records,
                "skipped": skipped,
            },
            indent=1,
        )
    )
    mean_tanimoto = float(np.mean([r["fingerprint_tanimoto"] for r in records]))
    print(
        f"Stage 2 wrote {len(records)} fingerprints "
        f"(mean Tanimoto {mean_tanimoto:.4f}, {len(skipped)} skipped)",
        flush=True,
    )


# ponytail: self-check
# After a real run, metrics.json must hold one record per stage-1 spectrum with a
# fingerprint_tanimoto strictly inside [0, 1], and predicted_fingerprints.npz's
# pred_fp_bits must be (n_records, 4096). Sanity check the formula converter
# round-trip, which is the one place ms-pred and jam element orders could diverge:
#   python -c "import sys; sys.path.insert(0,'/mnt/home/magled/jam/src'); \
#       from jam import chem; \
#       print(chem.vec_to_formula(chem.formula_to_vec('C7OH4Cl')))"
# Expect C7H4Cl (jam element order), and jam.chem.get_ion_idx('[M+H]+') == 0.

if __name__ == "__main__":
    sys.exit(main())
