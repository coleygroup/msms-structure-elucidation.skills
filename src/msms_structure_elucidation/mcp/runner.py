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
from msms_structure_elucidation.mcp.jobs import JobStore, now, write_json


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
    with (job_dir / 'log.txt').open('a') as log:
        try:
            result = tasks.run(request['task'], request['params'], job_dir, settings(None), devices, log)
        except Exception as exc:  # any task failure is reported to the MCP client through status.json
            traceback.print_exc(file=log)
            store.set_status(job_id, 'failed', error=f'{type(exc).__name__}: {exc}'[-4000:], finished=now())
            return 1
    write_json(job_dir / 'result.json', result)
    store.set_status(job_id, 'done', finished=now())
    return 0


if __name__ == '__main__':
    sys.exit(main())
