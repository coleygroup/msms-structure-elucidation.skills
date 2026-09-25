"""Stage 2b of the cross-model cascade: predict Morgan fingerprints with JAM,
injecting the known reaction's reactant fingerprint (reactant-conditioned MistNet).

Minimal variant of `02_predict_jam_fingerprint.py`: identical subformula-tree
building and Spectrum construction, but loads the reactant-conditioned MistNet
checkpoint (`model.reactant_inject=concat_pooled`, `reactant_combine=max`,
`reactant_fp_dim=4096`) and attaches a single, constant reactant fingerprint to
every record's batch entry. All molecules in this experiment (rxn_7) come from
the same OPRD reaction (`2_1` in dataset_v7_0729.json, Friedel-Crafts acylation),
whose only reactant is 2-chlorobenzoyl chloride (`O=C(Cl)c1ccccc1Cl`), so one
reactant fingerprint is computed once and reused for every spectrum.

The reactant fingerprint is computed with jam's own `FingerprintSpec` /
`Molecule.from_smiles` (not bare RDKit calls) to guarantee bit-for-bit
consistency with how the checkpoint was trained (see
`/home/datashare/impurities/data/oprd/build_oprd_reactant_fps.py`, the reference
pattern for the OPRD reactant-fp cache).

`ReactantFeaturizer.collate` (jam/src/jam/data/featurizers.py) normally reads a
per-spec-name hdf5 cache and produces `reactant_fps` (B, R, 4096),
`reactant_mask` (B, R) bool and `has_reactant` (B,) bool. Since none of these
spec names are in any hdf5, that path would yield an all-zero, masked-out
reactant (i.e. no conditioning). Instead, after collation each batch's
`reactant_fps`/`reactant_mask`/`has_reactant` are overwritten in place with the
one constant reactant (R=1, mask=True) before `model.encode_spectra(batch)` is
called, matching exactly what `MistNet._reactant_vector`
(jam/src/jam/models/mist.py) and `ReactantCombiner.forward`
(jam/src/jam/models/mist_modules.py, combine="max") expect.

JAM lives in a sibling pixi project, so this script is executed with that
project's interpreter, same as stage 2.

Usage:
    # Env: external jam pixi project (not a snowmageddon pixi env)
    pixi run --manifest-path /mnt/home/magled/jam/pixi.toml python \\
        .agents/skills/msms-cross-model-elucidation/scripts/02b_predict_jam_fingerprint_reactant_aug.py \\
        --spectra-dir results/<timestamp>/01_glacier/spectra \\
        --manifest results/<timestamp>/01_glacier/manifest.json \\
        --output-dir results/<timestamp>/02b_jam_reactant_aug \\
        --spec-names rxn7_4_ce20 rxn7_4_ce40 ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
import yaml

JAM_ROOT = Path("/mnt/home/magled/jam")
sys.path.insert(0, str(JAM_ROOT / "src"))

from jam import chem  # noqa: E402
from jam.data.datasets import SpectraDataModule, SpectraMolDataset  # noqa: E402
from jam.data.featurizers import FingerprintSpec, get_paired_featurizer  # noqa: E402
from jam.models.base import get_model_cls  # noqa: E402
from jam.molecule import Molecule, Spectrum  # noqa: E402
from rdkit import Chem, RDLogger  # noqa: E402
from rdkit.Chem import rdFingerprintGenerator  # noqa: E402

RDLogger.DisableLog("rdApp.*")

DEFAULT_CHECKPOINT = Path(
    "/home/magled/jam/results/"
    "2026-09-14_01-44-04_reactant_concat_pooled_max_all_maxtrain_4096_bs128/best.ckpt"
)
ADDUCT = "[M+H]+"
DEFAULT_THRESHOLD = 0.15
REACTANT_SMILES = "O=C(Cl)c1ccccc1Cl"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the reactant-augmented JAM fingerprint stage."""
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
        help="JAM (reactant-conditioned MistNet) checkpoint (.ckpt).",
    )
    parser.add_argument(
        "--reactant-smiles",
        default=REACTANT_SMILES,
        help="SMILES of the reactant to condition on (constant across all records).",
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
    parser.add_argument(
        "--spec-names",
        nargs="*",
        default=None,
        help="Optional subset of stage-1 spec_names to process (default: all).",
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


def inject_constant_reactant(batch: dict, reactant_fp: torch.Tensor) -> dict:
    """Overwrite a collated batch's reactant fields with one constant reactant.

    `ReactantFeaturizer.collate` normally builds `reactant_fps` (B, R, fp_dim),
    `reactant_mask` (B, R) bool and `has_reactant` (B,) bool from a per-spec-name
    hdf5 lookup. Since none of these spec names are in any hdf5, that path
    yields an all-zero, fully-masked-out reactant (no conditioning at all). This
    overwrites those three fields in place with R=1 and every mask entry True,
    matching what `MistNet._reactant_vector` / `ReactantCombiner.forward`
    (combine="max") expect for a single known reactant per example.
    """
    batch_size = batch["reactant_mask"].shape[0]
    batch["reactant_fps"] = reactant_fp.view(1, 1, -1).expand(batch_size, 1, -1).clone()
    batch["reactant_mask"] = torch.ones(batch_size, 1, dtype=torch.bool)
    batch["has_reactant"] = torch.ones(batch_size, dtype=torch.bool)
    return batch


def main() -> None:
    """Run the reactant-augmented JAM fingerprint prediction stage end to end."""
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
    reactant_fp_dim = int(model.hparams.reactant_fp_dim)
    print(
        f"Loaded reactant-conditioned MistNet ({fingerprint_spec['kind']}, "
        f"n_bits={n_bits}, radius={radius}, reactant_inject="
        f"{model.hparams.reactant_inject}, reactant_fp_dim={reactant_fp_dim})",
        flush=True,
    )
    assert model.reactant_combiner is not None, (
        "Loaded checkpoint has no reactant_combiner; this is not the "
        "reactant-conditioned checkpoint."
    )

    reactant_spec = FingerprintSpec(kind="morgan", n_bits=reactant_fp_dim, radius=2)
    reactant_mol = Molecule.from_smiles(args.reactant_smiles)
    if reactant_mol is None:
        raise ValueError(f"Could not parse reactant SMILES {args.reactant_smiles!r}")
    reactant_fp_np = reactant_spec.compute(reactant_mol).astype(np.float32)
    reactant_fp = torch.as_tensor(reactant_fp_np)
    print(
        f"Reactant fingerprint for {args.reactant_smiles!r}: "
        f"{int(reactant_fp_np.sum())} bits set out of {reactant_fp_dim}",
        flush=True,
    )

    with (output_dir / "input_configs.yaml").open("w") as handle:
        yaml.safe_dump(
            {
                **vars(args),
                "adduct": ADDUCT,
                "model_class": "MistNet",
                "reactant_conditioned": True,
                "reactant_inject": model.hparams.reactant_inject,
                "reactant_combine": model.hparams.reactant_combine,
                "reactant_fp_dim": reactant_fp_dim,
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

    spec_name_filter = set(args.spec_names) if args.spec_names else None
    records, skipped = [], []
    for spec_path in sorted(Path(args.spectra_dir).glob("*.json")):
        spectrum = json.loads(spec_path.read_text())
        if (
            spec_name_filter is not None
            and spectrum["spec_name"] not in spec_name_filter
        ):
            continue
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

    if spec_name_filter is not None:
        missing = (
            spec_name_filter
            - {r["spec_name"] for r in records}
            - {s["spec_name"] for s in skipped}
        )
        if missing:
            raise ValueError(
                f"Requested spec_names not found in spectra-dir: {missing}"
            )

    featurizer = get_paired_featurizer(
        spec_features=model_cls.spec_features(mode="test"),
        mol_features=model_cls.mol_features(),
        subform_folder=str(subform_dir),
        reactant_fp_folder=None,
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
            batch = inject_constant_reactant(batch, reactant_fp.to(model.device))
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
        f"Stage 2b wrote {len(records)} reactant-augmented fingerprints "
        f"(mean Tanimoto {mean_tanimoto:.4f}, {len(skipped)} skipped)",
        flush=True,
    )


# ponytail: self-check
# After a real run, metrics.json must hold one record per requested spec_name
# with a fingerprint_tanimoto strictly inside [0, 1], and
# predicted_fingerprints.npz's pred_fp_bits must be (n_records, 4096). The
# load-bearing risk in this script is the reactant injection silently being a
# no-op (e.g. because `reactant_combiner is None`, or because the model still
# reads a stale zeroed `reactant_fps` from collate before the override). Confirm
# after a run that the predicted probabilities differ from the plain,
# unconditioned stage-2 checkpoint's output for the same spec_name -- if they
# are bit-identical (or the two checkpoints' probability arrays are within
# float noise of each other), the reactant conditioning is not taking effect
# and must be debugged before trusting any Tanimoto numbers downstream:
#   python -c "
#   import numpy as np
#   a = np.load('results/<timestamp>/02_jam/predicted_fingerprints.npz')
#   b = np.load('results/<timestamp>/02b_jam_reactant_aug/predicted_fingerprints.npz')
#   names_a = {n: i for i, n in enumerate(a['spec_names'])}
#   for n in b['spec_names']:
#       ia, ib = names_a[n], list(b['spec_names']).index(n)
#       diff = np.abs(a['pred_fp_probs'][ia] - b['pred_fp_probs'][ib]).max()
#       print(n, 'max abs prob diff:', diff)
#   "

if __name__ == "__main__":
    sys.exit(main())
