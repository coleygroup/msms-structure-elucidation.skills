"""File-based endpoint protocol for local, SSH and Kubernetes workspace gateways.

Inputs are JSON on stdin. Large artifacts are streamed as tar on stdout; they are
never embedded in MCP messages. Run only on explicitly configured trusted hosts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from msms_structure_elucidation.mcp.backends import make_backend
from msms_structure_elucidation.config import settings as load_settings, asset, overlay
from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, read_json, write_json
from msms_structure_elucidation.prediction_cache import file_fingerprint, source_fingerprint, file_lock


def inventory(folder: Path) -> dict:
    """Fingerprint only publishable files, including logs, without loading spectra into RAM."""
    files = {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Endpoint artifact must not be a symlink: {path}')
        relative = path.relative_to(folder).as_posix()
        if path.is_file() and (relative.startswith(('native_outputs/', 'native_logs/')) or
                              path.name in ('result.json', 'status.json', 'launch.json', 'gpu-metrics.json', 'gpu-samples.csv') or
                              path.suffix in ('.log', '.txt')):
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    hasher.update(block)
            files[relative] = {'sha256': hasher.hexdigest(), 'size': path.stat().st_size}
    if not any(name.startswith('native_outputs/') and name.endswith('.hdf5') for name in files):
        raise RuntimeError('Completed endpoint has no HDF5 outputs')
    return files


def remap(params: dict, endpoint: dict, folder: Path, prepared: dict | None) -> dict:
    """Change paths and runtime tuning, then require identical weights and native implementation."""
    params = json.loads(json.dumps(params))
    params.pop('_parent_job', None)  # parent is in the coordinator's independent JobStore
    arguments = params['arguments']
    profile = endpoint.get('simulator') or {}
    if arguments['model'] == 'glacier':
        profile = {**profile, **profile.get('glacier', {})}
        if profile.get('num_gpu_workers', 1) != 1 or profile.get('prediction_processes_per_gpu', 1) != 1:
            raise ValueError('GLACIER requires one GPU worker and one prediction process per GPU')
    runtime = profile
    for key in ('batch_size', 'num_cpu_workers', 'num_gpu_workers', 'num_writers',
                'num_h5_chunks', 'feat_cache_size'):
        if key in runtime:
            arguments[key] = runtime[key]
    # sparse_k changes scientific output, so it cannot be overridden by host tuning.
    if 'sparse_k' in runtime and runtime['sparse_k'] != arguments['sparse_k']:
        raise ValueError('Endpoint sparse_k differs from the confirmed prediction request')
    keys = ('checkpoint',) if arguments['model'] == 'glacier' else ('gen_checkpoint', 'inten_checkpoint')
    mapped = {}
    for key in keys:
        original = arguments[key]
        replacement = profile.get(key) or original
        expected = params['weights'][original]
        if file_fingerprint(Path(replacement), folder.parent / 'fingerprints') != expected:
            raise ValueError(f'Endpoint {key} does not match the approved weights')
        arguments[key] = replacement
        mapped[replacement] = expected
    params['weights'] = mapped
    params['python'] = profile.get('python') or params['python']
    params['ms_pred_dir'] = profile.get('ms_pred_dir') or params['ms_pred_dir']
    if not (Path(params['ms_pred_dir']) / 'src/ms_pred').is_dir():
        raise FileNotFoundError('Endpoint ms_pred_dir does not contain src/ms_pred')
    if source_fingerprint(Path(params['ms_pred_dir']) / 'src/ms_pred') != params['native_source']:
        raise ValueError('Endpoint native source differs from the confirmed model implementation')
    actual_adapter = source_fingerprint(Path(__file__).parents[1])
    if params.get('adapter_source') and params['adapter_source'] != actual_adapter:
        from msms_structure_elucidation.prediction_cache import _raw_adapter_source
        raw = _raw_adapter_source(Path(__file__).parents[1]/'worker.py')
        if endpoint.get('compatible_prepared_adapters',{}).get(params['adapter_source'])!=raw:
            raise ValueError('Endpoint package source differs from the coordinator; synchronize the checkout')
        params['prepared_adapter_source'] = params['adapter_source']
        params['adapter_source'] = actual_adapter
    arguments['cuda_devices'] = None  # assigned by the endpoint's scheduler
    if arguments['model'] == 'glacier':
        arguments['num_gpu_workers'] = 1
    else:
        devices = str(profile.get('cuda_devices') or '0').split(',')
        arguments['num_gpu_workers'] = max(1, int(arguments['num_gpu_workers']) // (
            len(devices) if 'num_gpu_workers' in profile else 1))
    if prepared is not None:
        if arguments['model'] == 'glacier':
            prepared['options']['checkpoint'] = arguments['checkpoint']
            for key in ('batch_size', 'num_cpu_workers', 'num_writers', 'num_h5_chunks', 'feat_cache_size'):
                prepared['options'][key] = arguments[key]
        manifest = folder / 'prepared.json'
        write_json(manifest, prepared)
        params['prepared'] = str(manifest)
    else:
        params['prepared'] = None
    return params


def handle(operation: str, data: dict):
    """Idempotent submission, status, collection and acknowledged cleanup."""
    endpoint = data['endpoint']
    saved = None
    if endpoint.get('config'):
        saved = load_settings(endpoint['config'])
        profile = saved.get('models', {}).get('simulator', {})
        mapped = dict(profile)
        for key, source in {'ms_pred_dir': 'ms_pred_src', 'checkpoint': 'glacier_ckpt',
                            'gen_checkpoint': 'gen_ckpt', 'inten_checkpoint': 'inten_ckpt'}.items():
            if profile.get(source):
                mapped[key] = asset(profile[source], saved)
        if profile.get('python'):
            interpreter = Path(profile['python'])
            mapped['python'] = asset(profile['python'], saved) if interpreter.is_absolute() or len(interpreter.parts) > 1 else profile['python']
        overlay(mapped, endpoint.get('simulator', {}))
        mcp = dict(saved.get('mcp', {}))
        overlay(mcp, endpoint.get('mcp', {}))
        endpoint = {**endpoint, 'simulator': mapped, 'mcp': mcp}
    if operation.startswith('worker-'):
        from msms_structure_elucidation.mcp.workers import handle as worker_handle
        return worker_handle(operation,data,endpoint,saved)
    store = JobStore(Path(endpoint['job_root']))
    config = {'mcp': {**endpoint.get('mcp', {}), 'backend': endpoint.get('backend', 'local')},
              '_mcp_python': endpoint.get('python') or sys.executable}
    config['mcp'].setdefault('scheduler_process', True)
    backend = make_backend(config['mcp']['backend'], store, config, config['_mcp_python'])
    if operation == 'probe':
        from msms_structure_elucidation import hostprobe
        description = backend.describe()
        return {**description, 'cpu': hostprobe.probe(), 'active_jobs': sum(
            row['state'] in ACTIVE for row in store.active())}
    if operation == 'validate':
        with tempfile.TemporaryDirectory(dir=store.root) as temp:
            remap(data['params'], endpoint, Path(temp), None)
        return {'compatible': True}
    if operation == 'submit':
        # The token is stable across lost SSH responses and never depends on endpoint-specific paths.
        token = {'coordinator': data['coordinator'], 'job': data['job_id'], 'attempt': data.get('attempt')}
        with file_lock(store.root / (JobStore.job_id('submission', token) + '.lock')):
            expected_id = store.job_id('model_inference', {**token, '_inputs': {}})
            if (store.root / expected_id / 'ready.json').is_file():
                job_id, reused = expected_id, True
            else:
                job_id, reused = store.create('model_inference', token, kind='gpu', config=config, initial_state='staging')
            if not (store.path(job_id) / 'ready.json').is_file():
                params = remap(data['params'], endpoint, store.path(job_id), data.get('prepared'))
                write_json(store.path(job_id) / 'request.json',
                           {'task': 'model_inference', 'kind': 'gpu', 'params': params})
                write_json(store.path(job_id) / 'ready.json', {'token': token})
                store.set_status(job_id, 'queued')
            if not reused or (store.status(job_id)['state'] == 'queued' and not store.launch(job_id)):
                backend.submit(job_id)
        return {'job_id': job_id, 'status': backend.poll(job_id),
                'manifest': read_json(store.path(job_id) / 'kubernetes-job.json')}
    job_id = data['remote_job_id']
    if Path(job_id).name != job_id:
        raise ValueError('Invalid remote job identifier')
    cleaned = store.root / 'cleaned' / (job_id + '.json')
    if operation == 'cleanup' and cleaned.is_file():
        if read_json(cleaned) != data.get('acknowledged_inventory'):
            raise ValueError('Cleanup acknowledgement differs from the archived receipt')
        if (store.root / job_id).is_dir():
            shutil.rmtree(store.path(job_id))
        return {'cleaned': True}
    if operation == 'poll':
        status = backend.poll(job_id)
        # Once terminal, retain an immutable collection manifest across retries.
        manifest = store.path(job_id) / 'collection.json'
        if status['state'] == 'done' and not manifest.exists():
            write_json(manifest, inventory(store.path(job_id)))
        return {'status': status, 'inventory': read_json(manifest),
                'log_tail': store.log_tail(job_id) if status['state'] == 'failed' else None}
    if operation == 'cancel':
        return backend.cancel(job_id)
    if operation == 'bundle':
        files = read_json(store.path(job_id) / 'collection.json')
        if not files:
            raise ValueError('Poll the completed job before collecting artifacts')
        with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
            for name in files:
                archive.add(store.path(job_id) / name, arcname=name, recursive=False)
        return None
    if operation == 'cleanup':
        files = read_json(store.path(job_id) / 'collection.json')
        if not files or files != data.get('acknowledged_inventory') or store.status(job_id)['state'] != 'done':
            raise ValueError('Cleanup requires acknowledgement of the exact collected artifact manifest')
        if hasattr(backend, 'cleanup'):
            backend.cleanup(job_id)
        cleaned.parent.mkdir(exist_ok=True)
        write_json(cleaned, files)
        shutil.rmtree(store.path(job_id))
        return {'cleaned': True}
    raise ValueError(f'Unknown endpoint operation {operation}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['probe', 'validate', 'submit', 'poll', 'bundle', 'cleanup', 'cancel',
        'worker-probe','worker-submit','worker-poll','worker-bundle','worker-cleanup','worker-cancel',
        'worker-scheduler-update'])
    args = parser.parse_args()
    try:
        result = handle(args.operation, json.load(sys.stdin))
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({'endpoint_error': str(exc), 'retryable': False}))
        return
    if result is not None:
        print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
