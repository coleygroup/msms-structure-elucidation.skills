"""Prepare ICEBERG conditions on CPU and invoke the unchanged native predictor."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def prepare(arguments: dict, work_dir: Path) -> Path:
    """Normalize molecule identities and energies without allocating a GPU or loading weights."""
    from ms_pred import common
    rows = []
    seen = set()
    instrument = common.normalize_instrument(arguments.get('instrument'))
    adduct = arguments['adduct']
    for smiles in arguments['smiles']:
        smiles = common.rm_stereo(smiles)
        key = common.inchikey_from_smiles(smiles)
        if key in seen:
            continue
        seen.add(key)
        precursor = common.mass_from_smi(smiles) + common.ion2mass[adduct]
        energies = [common.nce_to_ev(e, precursor) if arguments['nce'] and not common.is_gc_instrument(instrument)
                    else e for e in arguments['energies']]
        rows.append({'spec': 'msms_prediction', 'smiles': smiles, 'ionization': adduct,
                     'instrument': instrument, 'inchikey': key, 'precursor': precursor,
                     'collision_energies': [f'{float(e):.0f}' for e in sorted(energies)]})
    folder = Path(tempfile.mkdtemp(prefix='msms-iceberg-', dir=work_dir))
    manifest = folder / 'prepared.json'
    manifest.write_text(json.dumps({'model': 'iceberg', 'rows': sorted(rows, key=lambda row: row['inchikey'])}, allow_nan=False))
    return manifest


def run_prepared(manifest: Path, arguments: dict) -> Path:
    """Keep native fragment generation/intensity inference together; generated graphs depend on the model."""
    from ms_pred.iceberg import iceberg_elucidation
    payload = json.loads(manifest.read_text())
    folder = Path(tempfile.mkdtemp(prefix='msms-iceberg-'))
    labels = folder / 'candidates.tsv'
    with labels.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(payload['rows'][0]), delimiter='\t')
        writer.writeheader()
        writer.writerows(payload['rows'])
    command = [sys.executable, str(Path(iceberg_elucidation.__file__).with_name('predict_smis.py')),
        '--dataset-labels', str(labels), '--gen-checkpoint', arguments['gen_checkpoint'],
        '--inten-checkpoint', arguments['inten_checkpoint'], '--save-dir', str(folder),
        '--batch-size', str(arguments['batch_size']), '--num-cpu-workers', str(arguments['num_cpu_workers']),
        '--num-gpu-workers', str(arguments['num_gpu_workers']), '--sparse-out', '--sparse-k', '100',
        '--max-nodes', '100', '--threshold', '0.1', '--adduct-shift']
    environment = dict(os.environ)
    if arguments['cuda_devices']:
        environment['CUDA_VISIBLE_DEVICES'] = arguments['cuda_devices']
        command.append('--gpu')
    done = subprocess.run(command, env=environment)
    if done.returncode or not list(folder.glob('preds*.hdf5')):
        raise RuntimeError(f'Native ICEBERG prediction failed ({done.returncode}); retained scratch at {folder}')
    (folder / 'iceberg_run_successful').touch()
    return folder
