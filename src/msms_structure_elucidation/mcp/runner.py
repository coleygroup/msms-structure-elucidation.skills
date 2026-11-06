"""Run one MCP job directory: python -m msms_structure_elucidation.mcp.runner <job_dir>.

Both backends execute this, locally as a detached process and on Slurm inside the
allocation. The GPUs a job may use arrive as CUDA_VISIBLE_DEVICES, set by the local
backend or by the scheduler. A job that already has a result is not run again, so a
requeued Slurm job is safe.
"""
from __future__ import annotations

import os
import socket
import sys
import traceback
from pathlib import Path

from msms_structure_elucidation.config import settings
from msms_structure_elucidation.mcp import tasks
from msms_structure_elucidation.mcp.jobs import JobStore, now, read_json, write_json


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print('usage: python -m msms_structure_elucidation.mcp.runner <job_dir>', file=sys.stderr)
        return 2
    job_dir = Path(argv[0]).resolve()
    store, job_id = JobStore(job_dir.parent), job_dir.name
    state = store.status(job_id)['state']
    if state == 'cancelled' or (state == 'done' and (job_dir / 'result.json').is_file()):
        return 0
    devices = os.environ.get('CUDA_VISIBLE_DEVICES') or None
    store.set_status(job_id, 'running', host=socket.gethostname(), pid=os.getpid(), started=now(),
                     devices=devices, slurm_job_id=os.environ.get('SLURM_JOB_ID'))
    request = store.request(job_id)
    config = read_json(job_dir / 'config.json') or settings(None)
    if (job_dir/'config.json').is_file():
        os.environ['MSMS_CONFIG'] = str(job_dir/'config.json')
    if request.get('kind') == 'gpu' and config.get('mcp', {}).get('backend') == 'kubernetes' and not devices:
        # Device plugins expose only the allocated GPUs. Do not set physical GPU
        # indices in the Pod manifest; some clusters prohibit that env override.
        devices = ','.join(str(index) for index in range(int(config['mcp']['kubernetes'].get('gpus', 1))))
    from msms_structure_elucidation.mcp.pipeline import CONTEXT
    if request.get('kind') == 'pipeline':
        # CPU parents and every scientific subprocess inherit this stage boundary.
        os.environ[CONTEXT] = str(job_dir)
    else:
        os.environ.pop(CONTEXT, None)
    store.set_phase(job_id, 'gpu_prediction' if request.get('kind') == 'gpu' else 'cpu_preparation')
    with (job_dir / 'log.txt').open('a') as log:
        try:
            environment = config.get('mcp', {}).get('worker_environment', {})
            if any(key in environment for key in ('CUDA_VISIBLE_DEVICES', 'MSMS_PIPELINE_JOB','MSMS_CONFIG')):
                raise ValueError('worker_environment must not override scheduler GPU or pipeline ownership')
            os.environ.update({str(key): str(value) for key, value in environment.items()})
            os.environ.setdefault('MSMS_PUBCHEM_RATE_FILE', str(store.root / 'pubchem-rate.json'))
            os.environ.setdefault('MSMS_PUBCHEM_CACHE_DIR', str(store.root / 'pubchem-responses'))
            os.environ.setdefault('MSMS_PUBCHEM_CACHE_TTL_SECONDS',
                                  str(config.get('mcp', {}).get('pubchem_cache_ttl_seconds', 86400)))
            if request.get('kind') == 'gpu':
                from msms_structure_elucidation.mcp.metrics import GpuMonitor
                with GpuMonitor(job_dir, devices, config.get('mcp', {}).get('gpu_monitor_interval_seconds', 1.)):
                    result = tasks.run(request['task'], request['params'], job_dir, config, devices, log)
            else:
                result = tasks.run(request['task'], request['params'], job_dir, config, devices, log)
        except Exception as exc:  # any task failure is reported to the MCP client through status.json
            traceback.print_exc(file=log)
            store.set_status(job_id, 'failed', error=f'{type(exc).__name__}: {exc}'[-4000:], finished=now(), exit_code=1)
            from msms_structure_elucidation.mcp.admission import Admission
            Admission(store, config).record(job_id, 'failed', str(exc) + '\n' + store.log_tail(job_id, 16000))
            from msms_structure_elucidation.mcp.pipeline import cancel_children, dispatch_pending
            cancel_children(store, job_id, config)
            dispatch_pending(store, config)
            return 1
    store.set_phase(job_id, 'complete')
    write_json(job_dir / 'result.json', result)
    store.set_status(job_id, 'done', finished=now(), exit_code=0)
    from msms_structure_elucidation.mcp.admission import Admission
    Admission(store, config).record(job_id, 'done')
    from msms_structure_elucidation.mcp.pipeline import dispatch_pending
    dispatch_pending(store, config)
    return 0


if __name__ == '__main__':
    sys.exit(main())
