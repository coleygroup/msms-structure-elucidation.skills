"""Launch MCP jobs on this host (with a GPU picker) or through Slurm.

Nothing here names a host, partition or GPU type: the local backend measures the GPUs it
finds, and the Slurm backend emits only the sbatch options set in `mcp.slurm`.
"""
from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from msms_structure_elucidation import hostprobe
from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, now

RUNNER = 'msms_structure_elucidation.mcp.runner'
REPO = Path(__file__).resolve().parents[3]


def runner_command(job_dir: Path, python: str | None = None) -> list[str]:
    return [python or sys.executable, '-m', RUNNER, str(job_dir)]


def busy_gpu_indices() -> set[int]:
    """Indices of GPUs that have compute processes, from nvidia-smi."""
    def query(*args):
        run = subprocess.run(['nvidia-smi', *args, '--format=csv,noheader'], capture_output=True, text=True)
        return [line.split(',') for line in run.stdout.strip().splitlines()] if run.returncode == 0 else []
    index = {uuid.strip(): int(i) for i, uuid in query('--query-gpu=index,uuid')}
    return {index[row[0].strip()] for row in query('--query-compute-apps=gpu_uuid') if row[0].strip() in index}


def free_gpus(min_free_fraction: float, reserved: set[int]) -> list[int]:
    """Idle GPUs with at least min_free_fraction of their own memory free, not reserved by our jobs."""
    found = hostprobe.gpus()
    busy = busy_gpu_indices() if found else set()
    return [g['index'] for g in found if g['index'] not in busy | reserved
            and g['memory_total_gb'] and g['memory_free_gb'] / g['memory_total_gb'] >= min_free_fraction]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalBackend:
    """Run jobs as detached processes on this host, one GPU per GPU job."""
    name = 'local'

    def __init__(self, store: JobStore, config: dict, python: str | None = None):
        settings = config.get('mcp', {})
        self.store, self.python = store, python
        self.min_free_fraction = float(settings.get('gpu_min_free_fraction', 0.8))
        self.gpu_count = len(hostprobe.gpus())
        limit = settings.get('max_concurrent_jobs', 'auto')
        self.max_jobs = max(1, self.gpu_count) if limit in (None, 'auto') else int(limit)
        self.host = socket.gethostname()
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def submit(self, job_id: str) -> None:
        self.pump()

    def _running(self) -> list[dict]:
        rows = []
        for job in self.store.list(limit=10_000):
            if job['state'] in ('submitted', 'running'):
                launch = self.store.launch(job['job_id'])
                if launch.get('backend') == self.name and launch.get('host') == self.host:
                    rows.append(launch)
        return rows

    def pump(self) -> None:
        """Launch queued jobs, oldest first, while GPUs and job slots are free."""
        with self.lock:
            for job_id in list(self.processes):
                if self.processes[job_id].poll() is not None:
                    del self.processes[job_id]
            queued = [job for job in self.store.list(limit=10_000) if job['state'] == 'queued']
            for job in sorted(queued, key=lambda job: job.get('updated', '')):
                running = self._running()
                if len(running) >= self.max_jobs:
                    return
                kind = self.store.request(job['job_id']).get('kind', 'gpu')
                reserved = {int(i) for launch in running for i in (launch.get('devices') or '').split(',') if i}
                gpus = free_gpus(self.min_free_fraction, reserved) if self.gpu_count and kind != 'cpu' else []
                if kind == 'gpu' and self.gpu_count and not gpus:
                    continue  # wait for a GPU; CPU-capable jobs behind it may still start
                self._launch(job['job_id'], str(gpus[0]) if gpus else '')

    def _launch(self, job_id: str, devices: str) -> None:
        job_dir = self.store.path(job_id)
        env = {**os.environ, 'CUDA_VISIBLE_DEVICES': devices}
        with (job_dir / 'runner.log').open('a') as log:
            process = subprocess.Popen(runner_command(job_dir, self.python), cwd=REPO, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes[job_id] = process
        self.store.set_launch(job_id, backend=self.name, host=self.host, pid=process.pid, devices=devices)
        self.store.set_status(job_id, 'submitted', devices=devices or None, submitted=now())

    def poll(self, job_id: str) -> dict:
        status = self.store.status(job_id)
        launch = self.store.launch(job_id)
        if status['state'] in ('submitted', 'running') and launch.get('host') == self.host:
            process = self.processes.get(job_id)
            ended = process.poll() is not None if process else not _alive(int(launch['pid']))
            status = self.store.status(job_id)  # the runner may have finished meanwhile
            if ended and status['state'] in ('submitted', 'running'):
                status = self.store.set_status(job_id, 'failed', finished=now(),
                    error='runner exited without a result; see runner.log and log.txt')
        if status['state'] == 'queued':
            self.pump()
            status = self.store.status(job_id)
        return status

    def cancel(self, job_id: str) -> dict:
        launch = self.store.launch(job_id)
        if self.store.status(job_id)['state'] in ('submitted', 'running') and launch.get('pid'):
            if launch.get('host') != self.host:
                raise RuntimeError(f'job {job_id} runs on {launch.get("host")}; cancel it from that host')
            if _alive(int(launch['pid'])):
                os.killpg(int(launch['pid']), signal.SIGTERM)
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        return {'backend': self.name, 'host': self.host, 'max_concurrent_jobs': self.max_jobs,
                'gpu_min_free_fraction': self.min_free_fraction, 'gpus': hostprobe.gpus(),
                'free_gpus': free_gpus(self.min_free_fraction, set())}


def sbatch_script(slurm: dict, kind: str, command: list[str]) -> tuple[list[str], str]:
    """sbatch options and a bash job script from mcp.slurm; unset keys are left out."""
    resources = dict(slurm)
    if kind != 'gpu':  # CPU tasks use the cpu block where set, and never request GPUs
        resources.update({k: v for k, v in (slurm.get('cpu') or {}).items() if v not in (None, '')})
        resources['gpus'] = None
    options = []
    for key, flag in (('partition', '--partition'), ('account', '--account'), ('qos', '--qos'),
                      ('time', '--time'), ('gpus', '--gpus'), ('cpus_per_task', '--cpus-per-task'), ('mem', '--mem')):
        if resources.get(key) not in (None, ''):
            options.append(f'{flag}={resources[key]}')
    if slurm.get('requeue'):
        options.append('--requeue')
    options += [str(arg) for arg in slurm.get('extra_args') or []]
    lines = ['#!/bin/bash', *[str(line) for line in slurm.get('setup') or []],
             'exec ' + ' '.join(shlex.quote(part) for part in command)]
    return options, '\n'.join(lines) + '\n'


def parse_job_id(stdout: str) -> str:
    """Job id from `sbatch --parsable` output (id or id;cluster)."""
    return stdout.strip().splitlines()[-1].split(';')[0].strip()


def scheduler_state(job: str) -> str | None:
    """The job's Slurm state from sacct, or from squeue while sacct has no record yet."""
    run = subprocess.run(['sacct', '-j', job, '-X', '-n', '-P', '-o', 'State'], capture_output=True, text=True)
    lines = [line.strip() for line in run.stdout.splitlines() if line.strip()] if run.returncode == 0 else []
    if not lines:
        run = subprocess.run(['squeue', '-h', '-j', job, '-o', '%T'], capture_output=True, text=True)
        lines = [line.strip() for line in run.stdout.splitlines() if line.strip()] if run.returncode == 0 else []
    return lines[-1].split()[0] if lines else None


SLURM_WAITING = {'PENDING', 'CONFIGURING', 'REQUEUED', 'REQUEUE_HOLD', 'REQUEUE_FED', 'RESIZING', 'SUSPENDED', 'PREEMPTED'}
SLURM_FAILED = {'FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE', 'COMPLETED'}


class SlurmBackend:
    """Submit each job with sbatch; the runner's status.json is authoritative once it exists."""
    name = 'slurm'

    def __init__(self, store: JobStore, config: dict, python: str | None = None):
        self.store, self.python = store, python
        self.slurm = config.get('mcp', {}).get('slurm') or {}

    def submit(self, job_id: str) -> None:
        job_dir = self.store.path(job_id)
        request = self.store.request(job_id)
        options, script = sbatch_script(self.slurm, request.get('kind', 'gpu'), runner_command(job_dir, self.python))
        (job_dir / 'job.sh').write_text(script)
        command = ['sbatch', '--parsable', f'--job-name=msms-{request["task"]}', f'--output={job_dir}/slurm-%j.log',
                   f'--chdir={REPO}', *options, str(job_dir / 'job.sh')]
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode:
            self.store.set_status(job_id, 'failed', error=f'sbatch failed: {done.stderr.strip()[-2000:]}', finished=now())
            raise RuntimeError(f'sbatch failed: {done.stderr.strip()[-2000:]}')
        job = parse_job_id(done.stdout)
        self.store.set_launch(job_id, backend=self.name, slurm_job_id=job, sbatch=command)
        self.store.set_status(job_id, 'submitted', slurm_job_id=job, submitted=now())

    def poll(self, job_id: str) -> dict:
        status = self.store.status(job_id)
        job = self.store.launch(job_id).get('slurm_job_id')
        if status['state'] not in ('submitted', 'running') or not job:
            return status
        state = scheduler_state(job)
        status = self.store.status(job_id)
        if status['state'] not in ACTIVE or state is None:
            return status
        if state == 'CANCELLED':
            return self.store.set_status(job_id, 'cancelled', scheduler_state=state, finished=now())
        if state in SLURM_FAILED or (state == 'PREEMPTED' and not self.slurm.get('requeue')):
            return self.store.set_status(job_id, 'failed', scheduler_state=state, finished=now(),
                error=f'Slurm job {job} ended as {state} without a result; see slurm-{job}.log')
        # A requeued job waits again even if its runner had started.
        return self.store.set_status(job_id, 'submitted' if state in SLURM_WAITING else status['state'],
                                     scheduler_state=state)

    def cancel(self, job_id: str) -> dict:
        job = self.store.launch(job_id).get('slurm_job_id')
        if job and self.store.status(job_id)['state'] in ACTIVE:
            subprocess.run(['scancel', job], capture_output=True, text=True, check=True)
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        return {'backend': self.name, 'host': socket.gethostname(),
                'slurm': {k: v for k, v in self.slurm.items() if v not in (None, '', [], {})}}


def make_backend(name: str, store: JobStore, config: dict, python: str | None = None):
    backends = {'local': LocalBackend, 'slurm': SlurmBackend}
    if name not in backends:
        raise ValueError(f'Unknown MCP backend {name!r}; use local or slurm')
    return backends[name](store, config, python)
