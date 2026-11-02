"""Launch MCP jobs on this host (with a GPU picker) or through Slurm.

Nothing here names a host, partition or GPU type: the local backend measures the GPUs it
finds, and the Slurm backend emits only the sbatch options set in `mcp.slurm`.
"""
from __future__ import annotations

import os
import math
import shlex
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from msms_structure_elucidation import hostprobe
from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, now
from msms_structure_elucidation.prediction_cache import file_lock

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
        devices = settings.get('gpu_devices')
        self.devices = None if devices is None else {int(i) for i in devices}
        if self.devices is not None and any(i < 0 for i in self.devices):
            raise ValueError('mcp.gpu_devices must contain non-negative physical GPU indices')
        detected = hostprobe.gpus()
        if self.devices is not None and self.devices - {gpu['index'] for gpu in detected}:
            raise ValueError('mcp.gpu_devices contains an index that is not detected on this host')
        self.gpu_count = sum(self.devices is None or gpu['index'] in self.devices for gpu in detected)
        limit = settings.get('max_concurrent_jobs', 'auto')
        self.max_jobs = max(1, self.gpu_count) if limit in (None, 'auto') else int(limit)
        self.max_cpu_jobs = int(settings.get('max_cpu_jobs', 2))
        prepared = settings.get('max_prepared_jobs', 'auto')
        self.max_prepared_jobs = max(2, 2 * self.gpu_count) if prepared in (None, 'auto') else int(prepared)
        self.min_free_memory_gb = float(settings.get('min_free_memory_gb', 2.))
        if min(self.max_jobs, self.max_cpu_jobs, self.max_prepared_jobs) < 1:
            raise ValueError('MCP concurrency and preparation limits must be positive')
        if not math.isfinite(self.min_free_memory_gb) or self.min_free_memory_gb < 0:
            raise ValueError('MCP free-RAM floor must be finite and non-negative')
        self.host = socket.gethostname()
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.exclude_tasks: set[str] = set()
        self.child_cancel = self.cancel

    def submit(self, job_id: str) -> None:
        self.pump()

    def _running(self) -> list[dict]:
        rows = []
        for job in self.store.list(limit=10_000):
            if job['state'] in ('submitted', 'running'):
                launch = self.store.launch(job['job_id'])
                if launch.get('backend') == self.name and launch.get('host') == self.host:
                    rows.append({**launch, **self.store.status(job['job_id']), 'job_id': job['job_id'],
                                 'kind': self.store.request(job['job_id']).get('kind', 'gpu')})
        return rows

    def pump(self) -> None:
        """Launch queued jobs, oldest first, while GPUs and job slots are free."""
        with self.lock, file_lock(self.store.root / 'scheduler.lock'):
            for row in self._running():
                self._reconcile(row['job_id'])
            for job_id in list(self.processes):
                if self.processes[job_id].poll() is not None:
                    del self.processes[job_id]
            # Resume scoring before starting new preparation. Sleeping orchestrators do
            # not consume CPU slots, and CPU work never consumes the GPU concurrency limit.
            running = self._running()
            cpu = sum(row['kind'] != 'gpu' and row.get('phase') not in ('waiting_gpu', 'waiting_cpu', 'waiting_cache')
                      for row in running)
            for row in running:
                if row.get('phase') == 'waiting_cpu' and cpu < self.max_cpu_jobs:
                    self.store.set_phase(row['job_id'], 'cpu_scoring')
                    cpu += 1
            queued = [job for job in self.store.list(limit=10_000) if job['state'] == 'queued']
            for job in sorted(queued, key=lambda job: job.get('updated', '')):
                if job['task'] in self.exclude_tasks:
                    continue
                running = self._running()
                request = self.store.request(job['job_id'])
                kind = request.get('kind', 'gpu')
                parent = request['params'].get('_parent_job')
                if parent and self.store.status(parent)['state'] not in ACTIVE:
                    self.store.set_status(job['job_id'], 'cancelled', finished=now(), error='Parent job ended')
                    continue
                if kind == 'gpu':
                    if sum(row['kind'] == 'gpu' for row in running) >= self.max_jobs:
                        continue
                else:
                    cpu = sum(row['kind'] != 'gpu' and row.get('phase') not in ('waiting_gpu', 'waiting_cpu', 'waiting_cache')
                              for row in running)
                    prepared = sum(row.get('phase') == 'waiting_gpu' for row in running)
                    if cpu >= self.max_cpu_jobs or prepared >= self.max_prepared_jobs:
                        continue
                    available = hostprobe.ram_gb()[1]
                    if available is not None and available < self.min_free_memory_gb:
                        continue
                reserved = {int(i) for launch in running for i in (launch.get('devices') or '').split(',') if i}
                gpus = free_gpus(self.min_free_fraction, reserved) if self.gpu_count and kind == 'gpu' else []
                if self.devices is not None:
                    gpus = [i for i in gpus if i in self.devices]
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
        status = self._reconcile(job_id)
        if status['state'] == 'queued':
            self.pump()
            status = self.store.status(job_id)
        return status

    def _reconcile(self, job_id: str) -> dict:
        """Release slots and child jobs when a runner exits without publishing a result."""
        status = self.store.status(job_id)
        launch = self.store.launch(job_id)
        if status['state'] in ('submitted', 'running') and launch.get('host') == self.host:
            process = self.processes.get(job_id)
            ended = process.poll() is not None if process else not _alive(int(launch['pid']))
            status = self.store.status(job_id)  # the runner may have finished meanwhile
            if ended and status['state'] in ('submitted', 'running'):
                status = self.store.set_status(job_id, 'failed', finished=now(),
                    error='runner exited without a result; see runner.log and log.txt')
                for child in self.store.children(job_id):
                    if self.store.status(child)['state'] in ACTIVE:
                        self.child_cancel(child)
        return status

    def cancel(self, job_id: str) -> dict:
        # Publish cancellation before killing processes so late writers cannot revive a job.
        active = self.store.status(job_id)['state'] in ('submitted', 'running')
        self.store.set_status(job_id, 'cancelled', finished=now())
        for child in self.store.children(job_id):
            if self.store.status(child)['state'] in ACTIVE:
                self.child_cancel(child)
        launch = self.store.launch(job_id)
        if active and launch.get('pid'):
            if launch.get('host') != self.host:
                raise RuntimeError(f'job {job_id} runs on {launch.get("host")}; cancel it from that host')
            if _alive(int(launch['pid'])):
                os.killpg(int(launch['pid']), signal.SIGTERM)
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        return {'backend': self.name, 'host': self.host, 'max_concurrent_jobs': self.max_jobs,
                'cpu': hostprobe.probe(),
                'max_cpu_jobs': self.max_cpu_jobs, 'max_prepared_jobs': self.max_prepared_jobs,
                'min_free_memory_gb': self.min_free_memory_gb,
                'gpu_devices': sorted(self.devices) if self.devices is not None else None,
                'gpu_min_free_fraction': self.min_free_fraction, 'gpus': hostprobe.gpus(),
                'free_gpus': free_gpus(self.min_free_fraction,
                    {int(index) for row in self._running() for index in (row.get('devices') or '').split(',') if index})}


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
        self.child_cancel = self.cancel

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
        if status['state'] == 'done' and status.get('scheduler_confirmed'):
            return status
        if status['state'] not in ('submitted', 'running', 'done') or not job:
            return status
        state = scheduler_state(job)
        status = self.store.status(job_id)
        if status['state'] in ('failed', 'cancelled'):
            return status
        if state == 'CANCELLED':
            return self.store.set_status(job_id, 'cancelled', scheduler_state=state, finished=now())
        if status['state'] == 'done':
            if state == 'COMPLETED':
                run = subprocess.run(['sacct', '-j', job, '-X', '-n', '-P', '-o', 'ExitCode'],
                                     capture_output=True, text=True)
                codes = [line.strip() for line in run.stdout.splitlines() if line.strip()] if not run.returncode else []
                if codes and codes[-1] == '0:0' and self.store.result(job_id) is not None:
                    return self.store.set_status(job_id, 'done', scheduler_state=state,
                                                 scheduler_confirmed=True, scheduler_exit_code='0:0')
                if codes and ':' in codes[-1] and codes[-1] != '0:0':
                    return self.store.set_status(job_id, 'failed', scheduler_state=state,
                        error=f'Slurm exit code {codes[-1]} after runner result', finished=now())
            elif state in SLURM_FAILED or state == 'CANCELLED':
                return self.store.set_status(job_id, 'failed', scheduler_state=state,
                    error=f'Slurm ended as {state} after runner result', finished=now())
            return {**status, 'state': 'running', 'scheduler_state': state or 'Awaiting accounting'}
        if state is None:
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
        for child in self.store.children(job_id):
            if self.store.status(child)['state'] in ACTIVE:
                self.child_cancel(child)
        job = self.store.launch(job_id).get('slurm_job_id')
        status = self.store.status(job_id)
        if status['state'] == 'done' and status.get('scheduler_confirmed'):
            return status
        if job and status['state'] == 'done':
            observed = self.poll(job_id)
            if observed['state'] in ('done', 'failed', 'cancelled'):
                return observed
        if job and status['state'] in ACTIVE + ('done',) and not status.get('cancel_requested'):
            cancelled = subprocess.run(['scancel', job], capture_output=True, text=True)
            if cancelled.returncode:
                observed = self.poll(job_id)
                if observed['state'] in ('done', 'failed', 'cancelled'):
                    return observed
                raise RuntimeError(f'Slurm cancellation not confirmed: {cancelled.stderr[-1000:]}')
            self.store.set_status(job_id, status['state'], cancel_requested=True)
        if job:
            return self.poll(job_id)
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        return {'backend': self.name, 'host': socket.gethostname(),
                'slurm': {k: v for k, v in self.slurm.items() if v not in (None, '', [], {})},
                'inventory': self.inventory()}

    def inventory(self) -> dict:
        """Observe partition availability and allocated/idle resources; Slurm owns admission."""
        output = {}
        for name, command in {'nodes': ['sinfo', '-N', '-h', '-o', '%N|%P|%t|%C|%m|%e|%G'],
                              'queue': ['squeue', '-h', '-u', os.environ.get('USER', ''), '-o', '%i|%T|%R']}.items():
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            output[name] = result.stdout if not result.returncode else {'error': result.stderr[-1000:]}
        return output


def make_backend(name: str, store: JobStore, config: dict, python: str | None = None):
    from msms_structure_elucidation.mcp.kubernetes import KubernetesBackend
    from msms_structure_elucidation.mcp.fleet import FleetBackend
    backends = {'local': LocalBackend, 'slurm': SlurmBackend, 'kubernetes': KubernetesBackend, 'pool': FleetBackend}
    if name not in backends:
        raise ValueError(f'Unknown MCP backend {name!r}; use local, slurm, kubernetes or pool')
    return backends[name](store, config, python)
