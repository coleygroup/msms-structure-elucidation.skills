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
from pathlib import Path

ACTIVE = ('queued', 'submitted', 'running')
FINISHED = ('done', 'failed', 'cancelled')
_JOB_ID = re.compile(r'^[a-z_]+-[0-9a-f]{16}$')


def now() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def write_json(path: Path, data) -> None:
    """Replace a JSON file atomically, so readers on shared file systems never see half a file."""
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2, allow_nan=False))
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
               kind: str = 'gpu') -> tuple[str, bool]:
        """Create the job directory; returns (job_id, reused) where reused means an earlier live or done job."""
        job_id = self.job_id(task, {**params, '_inputs': inputs or {}})
        path = self.root / job_id
        state = (read_json(path / 'status.json') or {}).get('state')
        if state in ACTIVE + ('done',):
            return job_id, True
        if path.exists():  # failed or cancelled: start again from a clean directory
            shutil.rmtree(path)
        path.mkdir(parents=True)
        for name, text in (inputs or {}).items():
            (path / name).write_text(text)
        write_json(path / 'request.json', {'task': task, 'kind': kind, 'params': params, 'created': now()})
        write_json(path / 'status.json', {'state': 'queued', 'updated': now()})
        return job_id, False

    def request(self, job_id: str) -> dict:
        return read_json(self.path(job_id) / 'request.json')

    def status(self, job_id: str) -> dict:
        return read_json(self.path(job_id) / 'status.json', {'state': 'queued'})

    def set_status(self, job_id: str, state: str, **extra) -> dict:
        path = self.path(job_id) / 'status.json'
        status = {**read_json(path, {}), **extra, 'state': state, 'updated': now()}
        write_json(path, status)
        return status

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
                             **{k: v for k, v in self.status(job_id).items() if k in ('state', 'updated', 'error')}})
        return sorted(jobs, key=lambda job: job.get('updated', ''), reverse=True)[:limit]
