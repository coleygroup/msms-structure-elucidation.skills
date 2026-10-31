"""Run GLACIER's native single-GPU prediction pipeline with explicit I/O settings."""
from __future__ import annotations

import csv
import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(smiles: list[str], energies: list, nce: bool, adduct: str, instrument: str | None,
        checkpoint: str, cuda_devices: str | None, batch_size: int, num_cpu_workers: int,
        num_writers: int, num_h5_chunks: int, feat_cache_size: int, sparse_k: int) -> Path:
    from ms_pred import common
    from ms_pred.glacier import glacier_elucidation
    script = Path(glacier_elucidation.__file__).with_name('predict_smis_joint.py')
    if not script.is_file():
        raise FileNotFoundError(f'GLACIER native prediction script is unavailable: {script}')
    supported = {arg.value for node in ast.walk(ast.parse(script.read_text()))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'add_argument' for arg in node.args
                 if isinstance(arg, ast.Constant) and isinstance(arg.value, str)}
    missing = {'--num-writers', '--num-h5-chunks', '--feat-cache-size'} - supported
    if missing:
        raise RuntimeError('This GLACIER runtime lacks ' + ', '.join(sorted(missing)) +
                           '; select a compatible ms-pred checkout with native parallel HDF5 writers. '
                           'The requested inference settings were not ignored.')
    folder = Path(tempfile.mkdtemp(prefix='msms-glacier-'))
    labels = folder / 'candidates.tsv'
    instrument = common.normalize_instrument(instrument)
    with labels.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['spec', 'smiles', 'ionization', 'instrument',
                                                   'inchikey', 'precursor', 'collision_energies'], delimiter='\t')
        writer.writeheader()
        for index, smi in enumerate(smiles):
            precursor = common.mass_from_smi(smi) + common.ion2mass[adduct]
            ev = [common.nce_to_ev(e, precursor) if nce and not common.is_gc_instrument(instrument) else e
                  for e in energies]
            writer.writerow({'spec': f'candidate_{index}', 'smiles': common.rm_stereo(smi),
                'ionization': adduct, 'instrument': instrument, 'inchikey': common.inchikey_from_smiles(smi),
                'precursor': precursor, 'collision_energies': [f'{float(e):.0f}' for e in sorted(ev)]})
    devices = cuda_devices.split(',') if cuda_devices else []
    command = [sys.executable, str(script), '--dataset-labels', str(labels), '--checkpoint', str(checkpoint),
               '--save-dir', str(folder), '--batch-size', str(batch_size), '--num-cpu-workers', str(num_cpu_workers),
               '--num-gpu-workers', str(max(1, len(devices))), '--num-writers', str(num_writers), '--num-h5-chunks', str(num_h5_chunks),
               '--feat-cache-size', str(feat_cache_size), '--sparse-out', '--sparse-k', str(sparse_k)]
    environment = os.environ.copy()
    if cuda_devices:
        # Native sharding launches one prediction process per visible GPU.
        environment['CUDA_VISIBLE_DEVICES'] = cuda_devices
        command.append('--gpu')
    completed = subprocess.run(command, env=environment)
    if completed.returncode:
        raise RuntimeError(f'GLACIER native prediction exited {completed.returncode}; logs: {folder}')
    if not list(folder.glob('preds*.hdf5')):
        raise RuntimeError(f'GLACIER did not publish its HDF5 chunk/shard files: {folder}')
    (folder / 'glacier_run_successful').touch()
    return folder
