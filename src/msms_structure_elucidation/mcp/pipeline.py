"""CPU orchestrators submit prepared, cache-missing inference work to GPU jobs.

Only file paths and model inputs cross the stage boundary. Experimental scoring
stays in the orchestrator; model outputs retain the existing raw-cache validation.
"""
from __future__ import annotations

import os
import math
import sys
import time
from pathlib import Path

from msms_structure_elucidation.mcp.backends import make_backend
from msms_structure_elucidation.mcp.jobs import ACTIVE, FINISHED, JobStore, read_json, write_json

CONTEXT = 'MSMS_PIPELINE_JOB'


def context() -> tuple[JobStore, str, dict] | None:
    """Return the current CPU orchestrator and its immutable configuration snapshot."""
    value = os.environ.get(CONTEXT)
    if not value:
        return None
    folder = Path(value)
    config = read_json(folder / 'config.json')
    if not config:
        raise RuntimeError('Missing MCP pipeline configuration snapshot')
    return JobStore(folder.parent), folder.name, config


def group_size(config: dict, model: str, energy_count: int = 5) -> int:
    """Target a bounded work duration using measured throughput, independently of CUDA batch size."""
    settings = config.get('mcp', {})
    explicit = settings.get('prediction_group_size', 'auto')
    if explicit not in ('auto', None):
        if int(explicit) < 1:
            raise ValueError('prediction_group_size must be positive or auto')
        return int(explicit)
    profile = config.get('models', {}).get('simulator', {})
    if model == 'glacier':
        profile = profile.get('glacier', {})
    rate = float(profile.get('spectra_per_second') or 0)
    duration = float(settings.get('target_prediction_seconds', 600))
    if not math.isfinite(rate) or not math.isfinite(duration) or rate < 0 or duration <= 0:
        raise ValueError('Prediction rate must be finite/nonnegative and target duration positive')
    estimate = int(rate * duration / max(1, energy_count)) if rate else 4096
    spectrum_target = settings.get('prediction_group_spectra')
    if spectrum_target is not None:
        if int(spectrum_target)<1:
            raise ValueError('prediction_group_spectra must be positive')
        estimate = max(estimate,math.ceil(int(spectrum_target)/max(1,energy_count)))
    from msms_structure_elucidation import hostprobe
    available = hostprobe.ram_gb()[1]
    # Decoded peaks, fragments, cache validation and JSON can coexist in RAM.
    # This is a conservative admission estimate, not a native-model memory guarantee.
    peaks = int(profile.get('sparse_k', 100))
    if available is not None:
        estimate = min(estimate, int(available * 1024**3 * .2 / (max(1, energy_count) * max(1, peaks) * 512)))
    maximum = int(settings.get('max_group_candidates', 131072 if spectrum_target is not None else 32768))
    if maximum < 1:
        raise ValueError('max_group_candidates must be positive')
    return max(1, min(maximum, estimate))


def _prepare_submission(arguments: dict, store: JobStore, parent: str, config: dict) -> tuple[str, bool]:
    """Create a CPU-prepared group; native CUDA graph generation remains in inference."""
    if store.status(parent)['state'] not in ACTIVE:
        raise RuntimeError('Parent analysis is no longer active')
    store.set_phase(parent, 'cpu_preparation')
    prepared = None
    if arguments['model'] == 'glacier':
        from msms_structure_elucidation import glacier
        from msms_structure_elucidation.worker import _check_glacier_features
        checkpoint = arguments['checkpoint']
        if not checkpoint or not Path(checkpoint).is_file():
            raise FileNotFoundError('GLACIER checkpoint required')
        _check_glacier_features(checkpoint)
        keys = ('smiles', 'energies', 'nce', 'adduct', 'instrument', 'checkpoint', 'cuda_devices',
                'batch_size', 'num_cpu_workers', 'num_writers', 'num_h5_chunks', 'feat_cache_size', 'sparse_k')
        options = {key: arguments[key] for key in keys}
        workers = config.get('mcp', {}).get('cpu_preparation_workers', 'auto')
        if workers == 'auto':
            allocated = os.environ.get('SLURM_CPUS_PER_TASK')
            if allocated:
                cpus = int(allocated)
            elif hasattr(os, 'sched_getaffinity'):
                cpus = len(os.sched_getaffinity(0))
            else:
                cpus = os.cpu_count() or 1
            workers = min(options['num_cpu_workers'], cpus)
        if int(workers) < 0:
            raise ValueError('CPU preparation workers must be nonnegative or auto')
        options['num_cpu_workers'] = int(workers)
        prepared = glacier.prepare(**options, work_dir=store.path(parent))
        manifest = read_json(prepared)
        manifest['options']['num_cpu_workers'] = arguments['num_cpu_workers']
        write_json(prepared, manifest)
    else:
        from msms_structure_elucidation import iceberg
        # Older runtimes without the native CLI retain their existing wrapper;
        # setup reports their staging limitation rather than patching the checkout.
        import ms_pred
        if (Path(ms_pred.__file__).parent / 'iceberg/predict_smis.py').is_file():
            prepared = iceberg.prepare(arguments, store.path(parent))
    import ms_pred
    from msms_structure_elucidation.prediction_cache import file_fingerprint, source_fingerprint
    weights = [arguments['checkpoint']] if arguments['model'] == 'glacier' else [
        arguments['gen_checkpoint'], arguments['inten_checkpoint']]
    if any(not path or not Path(path).is_file() for path in weights):
        raise FileNotFoundError('Selected model checkpoints are missing before GPU submission')
    params = {'arguments': arguments, 'prepared': str(prepared) if prepared else None,
              'python': sys.executable, 'ms_pred_dir': str(Path(ms_pred.__file__).resolve().parents[2]),
              '_parent_job': parent,
              'weights': {path: file_fingerprint(Path(path), store.root / 'fingerprints') for path in weights},
              'native_source': source_fingerprint(Path(ms_pred.__file__).parent)}
    params['adapter_source'] = source_fingerprint(Path(__file__).parents[1])
    child, reused = store.create('model_inference', params, kind='gpu', config=config)
    return child, reused


def run_groups(arguments: dict):
    """Keep a bounded queue of prepared groups across endpoints, yielding complete groups to CPU decoding."""
    store, parent, config = context()
    # Launchers must use the small MCP interpreter, not import scientific packages
    # into the server. The actual model task starts the recorded scientific Python.
    launcher = config.get('_mcp_python') or sys.executable
    backend = make_backend(config.get('mcp', {}).get('backend', 'local'), store, config, launcher)
    size = group_size(config, arguments['model'], len(arguments['energies']))
    configured = config.get('mcp', {}).get('prepared_groups', 'auto')
    slots = backend.capacity() if hasattr(backend, 'capacity') else getattr(backend, 'max_jobs', 1)
    width = min(8, 2 * slots) if configured == 'auto' else int(configured)
    if width < 1:
        raise ValueError('mcp.prepared_groups must be positive')
    pending = []
    children = []
    next_start = 0
    try:
        while pending or next_start < len(arguments['smiles']):
            while len(pending) < width and next_start < len(arguments['smiles']):
                if store.status(parent).get('phase') in ('waiting_gpu', 'waiting_cpu'):
                    resume_cpu(store, parent, config, backend)
                part = {**arguments, 'smiles': arguments['smiles'][next_start:next_start + size]}
                child, reused = _prepare_submission(part, store, parent, config)
                next_start += size
                children.append(child)
                pending.append(child)
                store.set_phase(parent, 'waiting_gpu', prediction_job=child, prediction_jobs=children)
                if not reused:
                    backend.submit(child)
            if store.status(parent)['state'] not in ACTIVE:
                raise RuntimeError('Parent analysis ended while waiting for prediction')
            completed = None
            for child in pending:
                status = backend.poll(child)
                if status['state'] in FINISHED:
                    if status['state'] != 'done':
                        raise RuntimeError(f'Prediction job {child} {status["state"]}: {status.get("error", "see child logs")}')
                    completed = child
                    break
            if completed:
                folder = Path(store.result(completed)['save_dir'])
                if not list(folder.glob('preds*.hdf5')):
                    raise RuntimeError(f'Prediction job {completed} has no output files at {folder}')
                pending.remove(completed)
                resume_cpu(store, parent, config, backend)
                yield folder
                if pending:
                    store.set_phase(parent, 'waiting_gpu', prediction_job=pending[0])
                continue
            if hasattr(backend, 'pump'):
                backend.pump()
            time.sleep(.5)
    finally:
        for child in pending:
            if store.status(child)['state'] in ACTIVE:
                backend.cancel(child)
        for child in children:
            process = getattr(backend, 'processes', {}).get(child)
            if process is not None and store.status(child)['state'] in FINISHED:
                process.wait(timeout=5)
        resume_cpu(store, parent, config, backend)


def run_model(arguments: dict) -> Path:
    """Compatibility entry point for callers requiring exactly one native output directory."""
    current = context()
    size = group_size(current[2], arguments['model'], len(arguments['energies']))
    if len(arguments['smiles']) > size:
        raise ValueError('Large staged predictions must consume run_groups to preserve per-group native identities')
    return list(run_groups(arguments))[0]


def resume_cpu(store: JobStore, parent: str, config: dict, backend=None) -> None:
    """Reacquire a CPU slot after a resource or shared-prediction wait."""
    backend = backend or make_backend(config.get('mcp', {}).get('backend', 'local'), store, config,
                                       config.get('_mcp_python'))
    store.set_phase(parent, 'waiting_cpu')
    scheduler = getattr(backend, 'cpu', backend)
    if scheduler.name != 'local':
        store.set_phase(parent, 'cpu_scoring')
    else:
        while store.status(parent).get('phase') == 'waiting_cpu':
            if store.status(parent)['state'] not in ACTIVE:
                raise RuntimeError('Parent analysis ended while waiting for CPU resources')
            if not getattr(scheduler, 'managed', False) and getattr(backend, 'dispatcher', True):
                backend.pump()
            time.sleep(.25)


def cache_wait_callbacks() -> tuple:
    """Cache lock waiters must release CPU slots so the cache owner can publish its result."""
    current = context()
    if current is None:
        return None, None
    store, parent, config = current
    return (lambda: store.set_phase(parent, 'waiting_cache'),
            lambda: resume_cpu(store, parent, config))


def inference(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    """Execute one prepared request using only the devices granted to this child job."""
    from msms_structure_elucidation import cli
    from msms_structure_elucidation.prediction_cache import file_fingerprint, source_fingerprint
    current = {path: file_fingerprint(Path(path), job_dir.parent / 'fingerprints') for path in params['weights']}
    if current != params['weights'] or source_fingerprint(Path(params['ms_pred_dir']) / 'src/ms_pred') != params['native_source']:
        raise RuntimeError('Model weights or native source changed while the prediction was queued')
    arguments = dict(params['arguments'])
    arguments['cuda_devices'] = devices
    if arguments['model'] == 'glacier':
        for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            os.environ.setdefault(name, '1')
        arguments['num_gpu_workers'] = 1
    else:
        arguments['num_gpu_workers'] = max(1, int(arguments['num_gpu_workers'])) * max(1, len(devices.split(',')) if devices else 1)
    request = job_dir / 'model.json'
    write_json(request, {'arguments': arguments, 'prepared': params.get('prepared'),
                         'publish_dir': str(job_dir / 'native_outputs')})
    result = cli.worker(params['python'], 'run-model', model_request=request,
                        ms_pred_dir=params['ms_pred_dir'], log_handle=log)
    if params.get('publish_raw_records') and not params.get('defer_raw_publication'):
        import subprocess
        env = {**os.environ, 'CUDA_VISIBLE_DEVICES': '', 'PYTHONPATH':
            str(Path(params['ms_pred_dir']) / 'src') + os.pathsep +
            str(Path(__file__).parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')}
        subprocess.run([params['python'], '-m', 'msms_structure_elucidation.mcp.raw_outputs',
            '--folder', result['save_dir'], '--request', str(request)], env=env, check=True,
            stdout=log, stderr=subprocess.STDOUT)
    return result


def cancel_children(store: JobStore, job_id: str, config: dict) -> None:
    """Stop separately scheduled prediction children when their orchestrator fails."""
    backend = make_backend(config.get('mcp', {}).get('backend', 'local'), store, config,
                           config.get('_mcp_python'))
    for child in store.children(job_id):
        if store.status(child)['state'] in ACTIVE:
            backend.cancel(child)


def dispatch_pending(store: JobStore, config: dict) -> None:
    """Continue detached local queues after a runner finishes, even without a connected client."""
    name = config.get('mcp', {}).get('backend', 'local')
    if name in ('local', 'pool') and not config.get('mcp', {}).get('scheduler_process', False):
        make_backend(name, store, config, config.get('_mcp_python')).pump()
