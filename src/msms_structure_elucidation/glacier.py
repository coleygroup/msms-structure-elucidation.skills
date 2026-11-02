"""Run GLACIER's native single-GPU prediction pipeline with explicit I/O settings."""
from __future__ import annotations

import csv
import ast
import importlib
import json
import random
from collections import OrderedDict
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _request(smiles: list[str], energies: list, nce: bool, adduct: str, instrument: str | None,
        checkpoint: str, cuda_devices: str | None, batch_size: int, num_cpu_workers: int,
        num_writers: int, num_h5_chunks: int, feat_cache_size: int, sparse_k: int,
        work_dir: Path | None = None) -> tuple[Path, list[str]]:
    """Prepare native labels and explicit runtime arguments without loading CUDA."""
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
    folder = Path(tempfile.mkdtemp(prefix='msms-glacier-', dir=work_dir))
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
    if cuda_devices:
        command.append('--gpu')
    return folder, command


def run(smiles: list[str], energies: list, nce: bool, adduct: str, instrument: str | None,
        checkpoint: str, cuda_devices: str | None, batch_size: int, num_cpu_workers: int,
        num_writers: int, num_h5_chunks: int, feat_cache_size: int, sparse_k: int) -> Path:
    """Run the native CLI for standalone prediction, preserving its sharding behavior."""
    arguments = locals().copy()
    folder, command = _request(**arguments)
    environment = os.environ.copy()
    if arguments.get('cuda_devices'):
        environment['CUDA_VISIBLE_DEVICES'] = arguments['cuda_devices']
    completed = subprocess.run(command, env=environment)
    if completed.returncode:
        raise RuntimeError(f'GLACIER native prediction exited {completed.returncode}; logs: {folder}')
    if not list(folder.glob('preds*.hdf5')):
        raise RuntimeError(f'GLACIER did not publish its HDF5 chunk/shard files: {folder}')
    (folder / 'glacier_run_successful').touch()
    return folder


def prepare(**arguments) -> Path:
    """Finish native GLACIER entry preparation in a CPU orchestrator before GPU allocation."""
    if min(arguments['batch_size'], arguments['num_writers'], arguments['num_h5_chunks'], arguments['sparse_k']) < 1:
        raise ValueError('GLACIER batch, writer, chunk and peak settings must be positive')
    folder, command = _request(**arguments)
    native = importlib.import_module('ms_pred.glacier.predict_smis_joint')
    if not all(hasattr(native, name) for name in ('get_args', '_prepare_entries_parallel', 'run_shard')):
        raise RuntimeError('GLACIER runtime does not expose staged entry preparation and run_shard')
    previous = sys.argv
    try:
        sys.argv = command[1:]
        options = vars(native.get_args())
    finally:
        sys.argv = previous
    with (folder / 'candidates.tsv').open(newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    entries = native._prepare_entries_parallel(rows, arguments['num_cpu_workers'])
    expected = len(rows) * len(set(arguments['energies']))
    if len(entries) != expected:
        raise RuntimeError(f'GLACIER preparation returned {len(entries)} entries, expected {expected}')
    manifest = folder / 'prepared.json'
    manifest.write_text(json.dumps({'entries': entries, 'options': options}, allow_nan=False))
    return manifest


def run_prepared(manifest: Path, devices: str | None) -> Path:
    """Use GLACIER's native prefetched featurization and parallel writers on prepared entries."""
    native = importlib.import_module('ms_pred.glacier.predict_smis_joint')
    payload = json.loads(manifest.read_text())
    options = payload['options']
    # Keep HDF5's many small writes on GPU-node scratch. The worker publishes
    # complete files to the shared job directory after the native writers finish.
    folder = Path(tempfile.mkdtemp(prefix='msms-glacier-'))
    options['save_dir'] = str(folder)
    native.common.setup_logger(folder, log_name='joint_pred.log', debug=options['debug'])
    count = len(devices.split(',')) if devices else 0
    if count <= 1:
        native.run_shard(payload['entries'], options['checkpoint'], 'cuda:0' if devices else 'cpu',
                         folder / options['out_name'], options)
    else:
        # Use the native per-GPU entrypoint and keep every molecule's energies
        # together, matching its CLI sharding without repeating entry preparation.
        by_molecule = OrderedDict()
        for entry in payload['entries']:
            by_molecule.setdefault(entry[0], []).append(entry)
        groups = list(by_molecule.values())
        random.Random(options.get('shard_seed', 1234)).shuffle(groups)
        shards = [[] for _ in range(count)]
        for index, entries in enumerate(groups):
            shards[index % count].extend(entries)
        name = Path(options['out_name'])
        outputs = [folder / f'{name.stem}_shard{index}{name.suffix}' for index in range(count)]
        native.mp.spawn(native._shard_entrypoint,
            args=(shards, options['checkpoint'], outputs, options), nprocs=count, join=True)
    if not list(folder.glob('preds*.hdf5')):
        raise RuntimeError(f'GLACIER did not publish prepared prediction outputs: {folder}')
    (folder / 'glacier_run_successful').touch()
    return folder
