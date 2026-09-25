#!/usr/bin/env python3
"""
Batch-lookup predicted MS/MS spectra for a list of SMILES against the
precomputed ICEBERG spectral atlas (MGF library, keyed by formula + InChIKey).

No model inference — this is a direct file lookup against spectra ICEBERG
already predicted. Molecules absent from the atlas are recorded as failures
(run msms-sim-iceberg for those instead).

Parallelized over rows (multiprocessing.Pool) and checkpointed by chunk:
progress is written to --checkpoint-dir as one JSONL file per chunk, so a
killed/crashed run can be resumed by re-invoking with the same
--checkpoint-dir — completed chunks are skipped, not reprocessed.

Usage:
    # Env: retrieval
    python .agents/skills/msms-atlas-lookup/scripts/run.py \\
        --input-csv /home/datashare/impurities/uspto/uspto_products.csv \\
        --smiles-col smiles \\
        --adduct "[M+H]+" \\
        --checkpoint-dir results/atlas_lookup_full/checkpoints \\
        --output results/atlas_lookup_full/spectra.hdf5 \\
        --failure-log results/atlas_lookup_full/failures.log \\
        --workers 32

Requirements:
    - Env: retrieval
    - Atlas root configured in configs/default.yaml (models.atlas.atlas_dir)
"""

import argparse
import csv
import json
import multiprocessing as mp
import os
import re
from pathlib import Path

import h5py
import numpy as np
import yaml
from rdkit import Chem
from rdkit import RDLogger
from rdkit.Chem import rdMolDescriptors

RDLogger.DisableLog("rdApp.*")

FORMULA_ELEM_RE = re.compile(r"([A-Z][a-z]?)(\d*)")
CHUNK_SIZE = 200

# Set once per worker process via the Pool initializer, to avoid re-reading
# the config/CLI args on every task.
_WORKER_ADDUCT_ROOT: Path | None = None


def formula_subdir(formula: str) -> Path:
    """Bucket a molecular formula into the atlas's C/O/H nested subdir layout."""
    parts = {}
    for symbol, num in FORMULA_ELEM_RE.findall(formula):
        if symbol in ("C", "O", "H") and symbol:
            parts[symbol] = f"{symbol}{num or ''}"
    subdir = Path()
    for symbol in ("C", "O", "H"):
        if symbol in parts:
            subdir = subdir / parts[symbol]
    return subdir if str(subdir) else Path("others")


def lookup_keys(smiles: str) -> dict:
    """Canonical SMILES, molecular formula, and InChIKey for atlas lookup."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    inchi = Chem.MolToInchi(mol)
    canon_mol = Chem.MolFromInchi(inchi) if inchi else mol
    canon_mol = canon_mol or mol
    return {
        "canonical_smiles": Chem.MolToSmiles(canon_mol),
        "formula": rdMolDescriptors.CalcMolFormula(canon_mol),
        "inchikey": Chem.MolToInchiKey(canon_mol),
        "inchikey14": Chem.MolToInchiKey(canon_mol)[:14],
    }


def load_mgf_index(mgf_path: Path) -> dict | None:
    """Load the .mgf.idx sidecar {inchikey14 -> [[offset, length], ...]}, or None."""
    idx_path = mgf_path.with_suffix(".mgf.idx")
    if not idx_path.exists():
        return None
    try:
        with open(idx_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def parse_mgf_block(text: str) -> tuple[dict, np.ndarray] | tuple[None, None]:
    """Parse a single BEGIN IONS ... END IONS text chunk into (meta, peaks[N,2])."""
    meta: dict = {}
    peaks: list[tuple[float, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line in ("BEGIN IONS", "END IONS"):
            continue
        if "=" in line and not line[0].isdigit():
            k, _, v = line.partition("=")
            meta[k] = v
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                peaks.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    if not peaks:
        return None, None
    return meta, np.asarray(peaks, dtype=float)


def blocks_for_inchikey14(
    mgf_path: Path, inchikey14: str
) -> list[tuple[dict, np.ndarray]]:
    """Retrieve all MGF blocks for an InChIKey (2D layer), via .idx sidecar if readable."""
    idx = load_mgf_index(mgf_path)
    if idx is not None:
        ranges = idx.get(inchikey14)
        if not ranges:
            ranges = [r for k, v in idx.items() if k.startswith(inchikey14) for r in v]
        if ranges:
            blocks = []
            with open(mgf_path, "rb") as f:
                for offset, length in ranges:
                    f.seek(offset)
                    chunk = f.read(length).decode("utf-8", errors="replace")
                    meta, peaks = parse_mgf_block(chunk)
                    if peaks is not None:
                        blocks.append((meta, peaks))
            return blocks

    # ponytail: no readable .idx sidecar (many are owner-locked 600) -> full scan
    needle_prefix = f"INCHIKEY={inchikey14}"
    blocks = []
    in_block = False
    meta: dict = {}
    lines: list[str] = []
    matches = False
    with open(mgf_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line == "BEGIN IONS":
                in_block, meta, lines, matches = True, {}, [], False
                continue
            if line == "END IONS":
                if matches:
                    m, peaks = parse_mgf_block("\n".join(lines))
                    if peaks is not None:
                        blocks.append((m, peaks))
                in_block = False
                continue
            if not in_block:
                continue
            lines.append(line)
            if line.startswith(needle_prefix):
                matches = True
    return blocks


def _init_worker(adduct_root: str) -> None:
    global _WORKER_ADDUCT_ROOT
    _WORKER_ADDUCT_ROOT = Path(adduct_root)


def _lookup_one(args: tuple[int, str]) -> dict:
    """Run in a worker process: look up one SMILES, return a JSON-able result dict."""
    i, smiles = args
    try:
        keys = lookup_keys(smiles)
        mgf_path = (
            _WORKER_ADDUCT_ROOT
            / formula_subdir(keys["formula"])
            / f"{keys['formula']}.mgf"
        )
        blocks = (
            blocks_for_inchikey14(mgf_path, keys["inchikey14"])
            if mgf_path.exists()
            else []
        )
        if not blocks:
            return {
                "row": i,
                "smiles": smiles,
                "ok": False,
                "error": f"Not in atlas (formula={keys['formula']}, mgf_exists={mgf_path.exists()})",
            }
        return {
            "row": i,
            "smiles": smiles,
            "ok": True,
            "canonical_smiles": keys["canonical_smiles"],
            "inchikey14": keys["inchikey14"],
            "formula": keys["formula"],
            "blocks": [
                {
                    "ce": meta.get("COLLISION_ENERGY", ""),
                    "mz": peaks[:, 0].tolist(),
                    "intensity": peaks[:, 1].tolist(),
                }
                for meta, peaks in blocks
            ],
        }
    except Exception as e:
        return {"row": i, "smiles": smiles, "ok": False, "error": str(e)}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch atlas lookup for MS/MS spectra by SMILES"
    )
    p.add_argument(
        "--input-csv", required=True, type=Path, help="CSV with a SMILES column"
    )
    p.add_argument(
        "--smiles-col",
        default="smiles",
        help="Column name holding SMILES (default: smiles)",
    )
    p.add_argument("--adduct", default="[M+H]+", choices=["[M+H]+", "[M-H]-"])
    p.add_argument("--config", default="configs/default.yaml")
    p.add_argument(
        "--limit", type=int, default=None, help="Only process the first N rows"
    )
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    p.add_argument(
        "--checkpoint-dir",
        required=True,
        type=Path,
        help="Dir for per-chunk resume state",
    )
    p.add_argument("--output", required=True, type=Path, help="Output HDF5 path")
    p.add_argument(
        "--failure-log", required=True, type=Path, help="Path for the failure log"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    project_root = Path(__file__).parent.parent.parent.parent.parent
    with open(project_root / args.config) as f:
        cfg = yaml.safe_load(f)

    atlas_cfg = cfg["models"]["atlas"]
    atlas_dir = Path(atlas_cfg["atlas_dir"]).expanduser()
    adduct_subdir = {"[M+H]+": "h_plus_out_mgf", "[M-H]-": "h_minus_out_mgf"}[
        args.adduct
    ]
    adduct_root = atlas_dir / adduct_subdir
    if not adduct_root.exists():
        raise FileNotFoundError(f"Atlas adduct dir not found: {adduct_root}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)
    args.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    with open(args.input_csv) as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]
    smiles_list = [row[args.smiles_col].strip() for row in rows]

    chunks = [
        (start, smiles_list[start : start + CHUNK_SIZE])
        for start in range(0, len(smiles_list), CHUNK_SIZE)
    ]

    pending = [
        (start, chunk_smiles)
        for start, chunk_smiles in chunks
        if not (args.checkpoint_dir / f"chunk_{start:08d}.jsonl").exists()
    ]
    n_done_chunks = len(chunks) - len(pending)
    if n_done_chunks:
        print(
            f"Resuming: {n_done_chunks}/{len(chunks)} chunks already complete, skipping.",
            flush=True,
        )

    with mp.Pool(
        args.workers, initializer=_init_worker, initargs=(str(adduct_root),)
    ) as pool:
        for chunk_idx, (start, chunk_smiles) in enumerate(pending):
            tasks = list(enumerate(chunk_smiles, start=start))
            results = pool.map(_lookup_one, tasks, chunksize=8)

            checkpoint_path = args.checkpoint_dir / f"chunk_{start:08d}.jsonl"
            tmp_path = checkpoint_path.with_suffix(".jsonl.tmp")
            with open(tmp_path, "w") as f:
                for r in results:
                    f.write(json.dumps(r) + "\n")
            tmp_path.rename(
                checkpoint_path
            )  # atomic: a crash mid-chunk leaves no partial file

            done_rows = min(start + CHUNK_SIZE, len(smiles_list))
            print(
                f"{done_rows}/{len(smiles_list)} processed "
                f"(chunk {n_done_chunks + chunk_idx + 1}/{len(chunks)})",
                flush=True,
            )

    # Merge all chunk checkpoints into the final HDF5 + failure log.
    smiles_out, inchikey14_out, mz_out, inten_out, ce_out, formula_out = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    n_found, n_failed = 0, 0

    with open(args.failure_log, "w") as fail_log:
        for start, _ in chunks:
            checkpoint_path = args.checkpoint_dir / f"chunk_{start:08d}.jsonl"
            with open(checkpoint_path) as f:
                for line in f:
                    r = json.loads(line)
                    if not r["ok"]:
                        n_failed += 1
                        fail_log.write(f"{r['row']}\t{r['smiles']}\t{r['error']}\n")
                        continue
                    n_found += 1
                    for block in r["blocks"]:
                        smiles_out.append(r["canonical_smiles"])
                        inchikey14_out.append(r["inchikey14"])
                        formula_out.append(r["formula"])
                        ce_out.append(block["ce"])
                        mz_out.append(np.array(block["mz"], dtype=np.float64))
                        inten_out.append(np.array(block["intensity"], dtype=np.float64))

    dt_vlen_f64 = h5py.special_dtype(vlen=np.float64)
    with h5py.File(args.output, "w") as h5:
        h5.create_dataset(
            "smiles", data=np.array(smiles_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset(
            "inchikey14", data=np.array(inchikey14_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset(
            "formula", data=np.array(formula_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset(
            "collision_energy", data=np.array(ce_out, dtype=h5py.string_dtype())
        )
        h5.create_dataset("mz", data=np.array(mz_out, dtype=object), dtype=dt_vlen_f64)
        h5.create_dataset(
            "intensity", data=np.array(inten_out, dtype=object), dtype=dt_vlen_f64
        )

    print(f"\nDone. {n_found} found, {n_failed} not in atlas.")
    print(f"Spectra: {args.output}")
    print(f"Failures: {args.failure_log}")


if __name__ == "__main__":
    main()
