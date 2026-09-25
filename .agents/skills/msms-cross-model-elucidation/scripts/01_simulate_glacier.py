"""Stage 1 of the cross-model cascade: simulate [M+H]+ MS/MS spectra with GLACIER.

Reads a plain text file of SMILES (one per line, no header), builds the
labels.tsv/split.tsv pair that `ms_pred.glacier.predict_smis_joint` expects,
shells out to the ms-pred uv venv to run joint fragment + intensity prediction,
then reads the resulting PredSpecDB HDF5 back into one JSON per
(molecule, collision energy) holding masses, intensities and per-fragment
formula strings.

GLACIER lives in a sibling repo with its own uv-managed venv, so it is invoked
with an absolute interpreter path rather than through this project's pixi envs.
The read-back also runs under that interpreter, since `ms_pred` is only
importable there.

Usage:
    # Env: default
    pixi run --environment default python \\
        .agents/skills/msms-cross-model-elucidation/scripts/01_simulate_glacier.py \\
        --smiles-file data/oprd_experiments_260904/rxn_7/rxn_7.txt \\
        --output-dir results/<timestamp>/01_glacier \\
        --name-prefix rxn7
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

MS_PRED_ROOT = Path("/mnt/home/magled/ms-pred")
MS_PRED_PYTHON = MS_PRED_ROOT / ".venv/bin/python"
DEFAULT_CHECKPOINT = Path(
    ".agents/skills/msms-cross-model-elucidation/resources/checkpoints/glacier/best.ckpt"
)

ADDUCT = "[M+H]+"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for the GLACIER simulation stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smiles-file",
        required=True,
        help="Text file with one SMILES per line, no header and no index column.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for labels.tsv, the PredSpecDB HDF5 and the per-spectrum JSONs.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(DEFAULT_CHECKPOINT),
        help="GLACIER joint-model checkpoint (.ckpt).",
    )
    parser.add_argument(
        "--name-prefix",
        default="rxn7",
        help="Prefix for generated per-molecule spectrum ids (<prefix>_<0-based index>).",
    )
    parser.add_argument(
        "--collision-energies",
        default="20,40,60",
        help="Comma-separated collision energies in eV to simulate per molecule.",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Pass --gpu to GLACIER. Omit to run inference on CPU.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, help="GLACIER inference batch size."
    )
    parser.add_argument(
        "--num-cpu-workers",
        type=int,
        default=4,
        help="DataLoader featurization workers for GLACIER.",
    )
    parser.add_argument(
        "--sparse-k",
        type=int,
        default=100,
        help="Number of top predicted fragments kept per spectrum.",
    )
    return parser.parse_args()


def read_smiles(smiles_file: Path) -> list[str]:
    """Read non-empty, stripped SMILES lines from a headerless text file."""
    lines = smiles_file.read_text().splitlines()
    return [line.strip() for line in lines if line.strip()]


def write_labels(
    output_dir: Path,
    smiles: list[str],
    names: list[str],
    collision_energies: list[int],
) -> tuple[Path, Path]:
    """Write the labels.tsv and split.tsv that predict_smis_joint expects."""
    ce_literal = "[" + ",".join(str(ce) for ce in collision_energies) + "]"
    labels_path = output_dir / "labels.tsv"
    with labels_path.open("w") as handle:
        handle.write("spec\tsmiles\tionization\tcollision_energies\tinstrument\n")
        for name, smi in zip(names, smiles):
            handle.write(f"{name}\t{smi}\t{ADDUCT}\t{ce_literal}\tOrbitrap\n")

    split_path = output_dir / "split.tsv"
    with split_path.open("w") as handle:
        handle.write("spec\tsplit\n")
        for name in names:
            handle.write(f"{name}\ttest\n")
    return labels_path, split_path


def run_glacier(args: argparse.Namespace, labels_path: Path, split_path: Path) -> Path:
    """Invoke GLACIER's predict_smis_joint in the ms-pred venv and return the HDF5 path."""
    save_dir = Path(args.output_dir) / "glacier_out"
    command = [
        str(MS_PRED_PYTHON),
        "-m",
        "ms_pred.glacier.predict_smis_joint",
        "--checkpoint",
        str(Path(args.checkpoint).resolve()),
        "--dataset-labels",
        str(labels_path.resolve()),
        "--split-name",
        str(split_path.resolve()),
        "--save-dir",
        str(save_dir.resolve()),
        "--sparse-out",
        "--sparse-k",
        str(args.sparse_k),
        "--frag-form-vecs",
        "--batch-size",
        str(args.batch_size),
        "--num-cpu-workers",
        str(args.num_cpu_workers),
    ]
    if args.gpu:
        command.append("--gpu")

    print("Running GLACIER:\n  " + " ".join(command), flush=True)
    subprocess.run(command, cwd=MS_PRED_ROOT, check=True)
    return save_dir / "preds.hdf5"


READER_SOURCE = '''
import json, sys
from pathlib import Path
import numpy as np
import ms_pred.common as common
from ms_pred.common import chem_utils

h5_path, out_dir, names_json = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3])
out_dir.mkdir(parents=True, exist_ok=True)

def vec_to_formula(vec):
    """ms-pred element-count vector -> formula string in ms-pred element order."""
    parts = []
    for idx in np.nonzero(vec)[0]:
        count = int(vec[idx])
        parts.append(f"{chem_utils.VALID_ELEMENTS[idx]}{count if count > 1 else ''}")
    return "".join(parts)

db = common.PredSpecDB(h5_path=Path(h5_path), mode="r")
available = set(db.get_all_names())
written = []
for name in names_json:
    pred_name = f"pred_{name}"
    if pred_name not in available:
        print(f"MISSING {pred_name}", flush=True)
        continue
    spec_dict, has_remark = db.read_from_name(pred_name)
    # read_from_name nests by remark (the inchikey) when one was written.
    ce_dicts = list(spec_dict.values()) if has_remark else [spec_dict]
    for ce_dict in ce_dicts:
        for ce_key, spec in ce_dict.items():
            ce = int(round(float(spec.collision_energy)))
            formulas = [vec_to_formula(v) for v in np.asarray(spec.frag_form_vecs)]
            payload = {
                "name": name,
                "spec_name": f"{name}_ce{ce}",
                "smiles": spec.root_canonical_smiles,
                "adduct": spec.adduct,
                "collision_energy": ce,
                "masses": np.asarray(spec.masses).astype(float).tolist(),
                "intensities": np.asarray(spec.intens).astype(float).tolist(),
                "frag_formulas": formulas,
                "element_order": list(chem_utils.VALID_ELEMENTS),
            }
            out_path = out_dir / f"{payload['spec_name']}.json"
            out_path.write_text(json.dumps(payload, indent=1))
            written.append(payload["spec_name"])
print("WROTE " + json.dumps(sorted(written)))
'''


def read_predictions(h5_path: Path, spectra_dir: Path, names: list[str]) -> list[str]:
    """Convert the PredSpecDB HDF5 into one JSON per (molecule, collision energy)."""
    reader_path = (spectra_dir.parent / "_read_predspec.py").resolve()
    reader_path.write_text(READER_SOURCE)
    result = subprocess.run(
        [
            str(MS_PRED_PYTHON),
            str(reader_path),
            str(Path(h5_path).resolve()),
            str(spectra_dir.resolve()),
            json.dumps(names),
        ],
        cwd=MS_PRED_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout, flush=True)
    for line in result.stdout.splitlines():
        if line.startswith("WROTE "):
            return json.loads(line[len("WROTE ") :])
    raise RuntimeError("PredSpecDB reader produced no WROTE manifest line")


def main() -> None:
    """Run the GLACIER simulation stage end to end."""
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    collision_energies = [int(ce) for ce in args.collision_energies.split(",")]
    smiles = read_smiles(Path(args.smiles_file))
    names = [f"{args.name_prefix}_{i}" for i in range(len(smiles))]
    print(f"Read {len(smiles)} SMILES from {args.smiles_file}", flush=True)

    with (output_dir / "input_configs.yaml").open("w") as handle:
        yaml.safe_dump(
            {
                **vars(args),
                "adduct": ADDUCT,
                "collision_energies_parsed": collision_energies,
                "num_smiles": len(smiles),
                "spectrum_names": names,
                "ms_pred_python": str(MS_PRED_PYTHON),
            },
            handle,
            sort_keys=True,
        )

    labels_path, split_path = write_labels(
        output_dir, smiles, names, collision_energies
    )
    h5_path = run_glacier(args, labels_path, split_path)

    spectra_dir = output_dir / "spectra"
    written = read_predictions(h5_path, spectra_dir, names)
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "adduct": ADDUCT,
                "collision_energies": collision_energies,
                "molecules": [{"name": n, "smiles": s} for n, s in zip(names, smiles)],
                "spectra": written,
            },
            indent=1,
        )
    )
    print(f"Stage 1 wrote {len(written)} spectra to {spectra_dir}", flush=True)


# ponytail: self-check
# Run with --help to confirm the argparse surface, then verify that the input
# parser yields 11 molecules and 3 collision energies for the rxn_7 input:
#   pixi run --environment default python .agents/skills/.../01_simulate_glacier.py --help
#   python -c "from pathlib import Path; \
#       import importlib.util as u; \
#       s=u.spec_from_file_location('m','01_simulate_glacier.py'); \
#       m=u.module_from_spec(s); s.loader.exec_module(m); \
#       print(len(m.read_smiles(Path('data/oprd_experiments_260904/rxn_7/rxn_7.txt'))))"
# Expect 11. After a real run, each spectra/*.json must have equal-length
# masses/intensities/frag_formulas and adduct == "[M+H]+".

if __name__ == "__main__":
    sys.exit(main())
