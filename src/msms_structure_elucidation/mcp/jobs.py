"""Job directories shared by the MCP server, its backends and the runner.

A job is a directory <root>/<job_id> holding request.json, status.json, result.json,
log.txt and launch.json. The id is derived from the request, so a repeated request finds
the earlier job and its result instead of running the model again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from msms_structure_elucidation.prediction_cache import file_lock

ACTIVE = ('queued', 'submitted', 'running')
FINISHED = ('done', 'failed', 'cancelled')
_JOB_ID = re.compile(r'^[a-z_]+-[0-9a-f]{16}$')


def now() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def write_json(path: Path, data) -> None:
    """Replace a JSON file atomically, so readers on shared file systems never see half a file."""
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.',
                                     suffix='.tmp', delete=False) as stream:
        temp = Path(stream.name)
        stream.write(json.dumps(data, indent=2, allow_nan=False))
    temp.replace(path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def job_id(task: str, params: dict) -> str:
        digest = hashlib.sha256(json.dumps([task, params], sort_keys=True).encode()).hexdigest()[:16]
        return f'{task}-{digest}'

    def path(self, job_id: str) -> Path:
        if not _JOB_ID.match(job_id):
            raise ValueError(f'Not a job id: {job_id!r}')
        path = self.root / job_id
        if not (path / 'request.json').is_file():
            raise FileNotFoundError(f'No job {job_id} under {self.root}')
        return path

    def create(self, task: str, params: dict, inputs: dict[str, str] | None = None,
               kind: str = 'gpu', config: dict | None = None, initial_state: str = 'queued') -> tuple[str, bool]:
        """Create the job directory; returns (job_id, reused) where reused means an earlier live or done job."""
        job_id = self.job_id(task, {**params, '_inputs': inputs or {}})
        path = self.root / job_id
        with file_lock(self.root / (job_id + '.lock')):
            state = (read_json(path / 'status.json') or {}).get('state')
            if state in ACTIVE + ('done', 'staging'):
                return job_id, True
            if path.exists():  # failed or cancelled: start again from a clean directory
                shutil.rmtree(path)
            path.mkdir(parents=True)
            for name, text in (inputs or {}).items():
                (path / name).write_text(text)
            if config is not None:
                write_json(path / 'config.json', config)
            write_json(path / 'status.json', {'state': initial_state, 'updated': now()})
            # Publish the request last: schedulers discover jobs through request.json.
            write_json(path / 'request.json', {'task': task, 'kind': kind, 'params': params,
                                             'created': now(), 'attempt': uuid.uuid4().hex})
        return job_id, False

    def request(self, job_id: str) -> dict:
        return read_json(self.path(job_id) / 'request.json')

    def status(self, job_id: str) -> dict:
        return read_json(self.path(job_id) / 'status.json', {'state': 'queued'})

    def set_status(self, job_id: str, state: str, **extra) -> dict:
        path = self.path(job_id) / 'status.json'
        with file_lock(path.with_suffix('.lock')):
            previous = read_json(path, {})
            if previous.get('state') == 'cancelled' and state != 'cancelled':
                return previous
            if previous.get('state') == 'done' and state in ACTIVE:
                return previous
            status = {**previous, **extra, 'state': state, 'updated': now()}
            write_json(path, status)
            return status

    def set_phase(self, job_id: str, phase: str, **extra) -> dict:
        """Account for stage wall time without changing the job's terminal state."""
        path = self.path(job_id) / 'status.json'
        with file_lock(path.with_suffix('.lock')):
            status = read_json(path, {})
            if status.get('state') in FINISHED:
                return status
            clock = time.time()
            elapsed = dict(status.get('phase_seconds', {}))
            if status.get('phase'):
                key = status['phase']
                elapsed[key] = elapsed.get(key, 0.) + max(0., clock - status.get('phase_started', clock))
            status.update(extra, phase=phase, phase_started=clock, phase_seconds=elapsed, updated=now())
            write_json(path, status)
            return status

    def children(self, job_id: str) -> list[str]:
        """Internal prediction jobs belonging to an orchestrator."""
        return [row['job_id'] for row in self.list(100_000)
                if self.request(row['job_id'])['params'].get('_parent_job') == job_id]

    def result(self, job_id: str):
        return read_json(self.path(job_id) / 'result.json')

    def launch(self, job_id: str) -> dict:
        return read_json(self.path(job_id) / 'launch.json', {})

    def set_launch(self, job_id: str, **launch) -> None:
        write_json(self.path(job_id) / 'launch.json', launch)

    def log_tail(self, job_id: str, max_bytes: int = 4000) -> str:
        """The end of a job's logs; only a bounded tail is read."""
        parts = []
        for log in sorted(self.path(job_id).glob('*.log')) + [self.path(job_id) / 'log.txt']:
            if log.is_file():
                with log.open('rb') as file:
                    file.seek(max(0, log.stat().st_size - max_bytes))
                    parts.append(file.read().decode(errors='replace'))
        return '\n'.join(parts)[-max_bytes:]

    def list(self, limit: int = 20) -> list[dict]:
        jobs = []
        for request in self.root.glob('*/request.json'):
            job_id = request.parent.name
            if _JOB_ID.match(job_id):
                jobs.append({'job_id': job_id, 'task': read_json(request)['task'],
                             **{k: v for k, v in self.status(job_id).items()
                                if k in ('state', 'updated', 'error', 'phase', 'phase_seconds')}})
        return sorted(jobs, key=lambda job: job.get('updated', ''), reverse=True)[:limit]
