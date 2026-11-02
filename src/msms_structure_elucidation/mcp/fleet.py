"""Dispatch prepared prediction groups to a configured pool of compatible servers."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, now, read_json, write_json
from msms_structure_elucidation.prediction_cache import file_lock


class IncompatibleEndpoint(RuntimeError):
    """A configuration mismatch requires correction, not a duplicate launch elsewhere."""


class Endpoint:
    """A small stdlib agent runs through local exec, SSH, or kubectl exec into a workspace gateway."""

    def __init__(self, name: str, settings: dict):
        self.name, self.settings = name, settings
        self.transport = settings.get('transport', 'ssh')
        if self.transport not in ('local', 'ssh', 'kubernetes'):
            raise ValueError(f'Unsupported endpoint transport {self.transport}')
        if not settings.get('job_root') or not Path(settings['job_root']).is_absolute():
            raise ValueError('Every endpoint needs an absolute private job_root')
        if settings.get('backend', 'local') not in ('local', 'slurm', 'kubernetes'):
            raise ValueError('Endpoint backend must be local, slurm or kubernetes')
        limit = settings.get('max_jobs', 'auto')
        if limit != 'auto' and int(limit) < 1:
            raise ValueError('Endpoint max_jobs must be positive or auto')
        if self.transport == 'ssh' and not settings.get('host'):
            raise ValueError('SSH endpoints require an explicit host')
        if self.transport == 'kubernetes':
            gateway, cluster = settings.get('gateway', {}), settings.get('mcp', {}).get('kubernetes', {})
            if not gateway.get('pod'):
                raise ValueError('Kubernetes endpoints require a bounded workspace gateway pod')
            for key in ('context', 'namespace'):
                if not gateway.get(key) or gateway[key] != cluster.get(key):
                    raise ValueError(f'The Kubernetes gateway and GPU jobs must use the same explicit {key}')

    def command(self, operation: str) -> list[str]:
        command = [self.settings.get('python') or sys.executable, '-m',
                   'msms_structure_elucidation.mcp.endpoint', operation]
        if self.transport == 'local':
            return command
        if self.transport == 'ssh':
            host = self.settings['host']
            if host.startswith('-'):
                raise ValueError('Invalid SSH destination')
            options = [part for option in self.settings.get('ssh_options', []) for part in ('-o', option)]
            body = shlex.join(command)
            if self.settings.get('repo'):
                body = f'cd {shlex.quote(self.settings["repo"])} && ' + body
            if self.settings.get('prefix'):
                body = self.settings['prefix'] + ' ' + body
            return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', *options, host, body]
        gateway = self.settings['gateway']
        return [gateway.get('kubectl', 'kubectl'), '--context', gateway['context'], '-n', gateway['namespace'],
                'exec', '-i', gateway['pod'], '-c', gateway.get('container', 'gateway'), '--', *command]

    def call(self, operation: str, payload: dict | None = None, output: Path | None = None):
        settings = copy.deepcopy(self.settings)
        if self.transport == 'kubernetes':
            if settings.get('backend') != 'kubernetes':
                raise ValueError('Kubernetes gateways require backend: kubernetes')
            settings.setdefault('mcp', {}).setdefault('kubernetes', {})['external_scheduler'] = True
        data = json.dumps({'endpoint': settings, **(payload or {})}, allow_nan=False)
        timeout = float(self.settings.get('timeout_seconds', 120))
        if output is None:
            done = subprocess.run(self.command(operation), input=data, text=True, capture_output=True, timeout=timeout)
            if done.returncode:
                raise RuntimeError(f'{self.name} {operation}: {done.stderr[-2000:]}')
            try:
                result = json.loads(done.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f'{self.name} returned a non-JSON endpoint response; '
                                   'check noninteractive startup output and the installed package') from exc
            if isinstance(result, dict) and result.get('endpoint_error'):
                raise IncompatibleEndpoint(f'{self.name}: {result["endpoint_error"]}')
            if self.transport == 'kubernetes':
                result = self._scheduler(operation, payload or {}, result)
            return result
        with output.open('wb') as stream:
            done = subprocess.run(self.command(operation), input=data.encode(), stdout=stream,
                                  stderr=subprocess.PIPE, timeout=float(self.settings.get('transfer_timeout_seconds', 1800)))
        if done.returncode:
            raise RuntimeError(f'{self.name} transfer: {done.stderr.decode(errors="replace")[-2000:]}')

    def _scheduler(self, operation: str, payload: dict, result: dict) -> dict:
        """Keep Kubernetes credentials on the coordinator; gateway pods need no API permissions."""
        from msms_structure_elucidation.mcp.kubernetes import kubectl, inventory_summary
        settings = self.settings['mcp']['kubernetes']
        def run(*args, **kwargs):
            response = kubectl(settings, *args, **kwargs)
            if response.returncode:
                raise RuntimeError(f'{self.name}: {response.stderr[-2000:]}')
            return response.stdout
        if operation == 'submit':
            response = kubectl(settings, 'create', '-f', '-', input=json.dumps(result['manifest']))
            if response.returncode and 'AlreadyExists' not in response.stderr:
                raise RuntimeError(f'Kubernetes submission failed: {response.stderr[-2000:]}')
        elif operation in ('poll', 'cancel', 'cleanup'):
            name = 'msms-' + payload['remote_job_id'].replace('_', '-')
            if operation == 'poll':
                job = json.loads(run('get', 'job', name, '-o', 'json'))
                pods = json.loads(run('get', 'pods', '-l', f'job-name={name}', '-o', 'json'))
                result['status']['scheduler_state'] = ','.join(
                    pod.get('status', {}).get('phase', 'Pending') for pod in pods['items']) or 'Pending'
                result['status']['scheduler_reason'] = '; '.join(condition.get('message', '')
                    for pod in pods['items'] for condition in pod.get('status', {}).get('conditions', [])
                    if condition.get('type') == 'PodScheduled' and condition.get('status') == 'False')[-2000:]
                conditions = {c['type'] for c in job.get('status', {}).get('conditions', []) if c['status'] == 'True'}
                if conditions & {'Complete', 'Failed'}:
                    logs = run('logs', f'job/{name}', '--all-containers=true')
                    result['scheduler_records'] = {'job': job, 'pods': pods, 'logs': logs}
                    success = bool(pods['items']) and all(p.get('status', {}).get('phase') == 'Succeeded' and
                        bool(p['status'].get('containerStatuses')) and all(
                        c.get('state', {}).get('terminated', {}).get('exitCode') == 0
                        for c in p['status']['containerStatuses']) for p in pods['items'])
                    if 'Failed' in conditions or not success or result['status']['state'] != 'done':
                        result['status'] = {'state': 'failed', 'error': 'Kubernetes Job, Pod, exit code and result must all confirm success'}
                        result['log_tail'] = logs[-4000:]
                elif result['status']['state'] == 'done':
                    result['status']['state'] = 'running'
            else:
                run('delete', 'job', name, '--ignore-not-found=true', '--wait=true')
        elif operation == 'probe':
            result['inventory'] = {}
            for name, resource in [('quota', 'resourcequota'), ('nodes', 'nodes'), ('pods', 'pods')]:
                response = kubectl(settings, 'get', resource, '-o', 'json')
                result['inventory'][name] = inventory_summary(resource, json.loads(response.stdout), settings.get(
                    'gpu_resource', 'nvidia.com/gpu')) if not response.returncode else {
                    'error': response.stderr[-1000:]}
        return result

    def archive_cancellation(self, remote_job_id: str, destination: Path) -> None:
        """Keep scheduler evidence before canceling a Kubernetes Job, including a pending one."""
        from msms_structure_elucidation.mcp.kubernetes import kubectl
        settings = self.settings['mcp']['kubernetes']
        name = 'msms-' + remote_job_id.replace('_', '-')
        records = {}
        for label, command in {'job': ['get', 'job', name, '-o', 'json'],
                               'pods': ['get', 'pods', '-l', f'job-name={name}', '-o', 'json'],
                               'logs': ['logs', f'job/{name}', '--all-containers=true']}.items():
            result = kubectl(settings, *command)
            if result.returncode and label != 'logs':
                if destination.is_file():
                    return
                raise RuntimeError(f'Cannot archive before cancellation: {result.stderr[-1000:]}')
            records[label] = result.stdout if not result.returncode else {'unavailable': result.stderr[-1000:]}
        write_json(destination, records)


def collect(endpoint: Endpoint, remote_job: str, inventory: dict, destination: Path) -> None:
    """Stream, validate paths, sizes and hashes, then atomically publish a local result tree."""
    with tempfile.TemporaryDirectory(prefix='collection-', dir=destination.parent) as temp:
        folder = Path(temp)
        archive_path = folder / 'outputs.tar'
        endpoint.call('bundle', {'remote_job_id': remote_job}, output=archive_path)
        target = folder / 'artifacts'
        target.mkdir()
        seen = set()
        with tarfile.open(archive_path, 'r:') as archive:
            for member in archive:
                path = Path(member.name)
                if (not member.isfile() or path.is_absolute() or '..' in path.parts or
                        member.name not in inventory or member.name in seen):
                    raise ValueError(f'Unexpected or unsafe endpoint artifact: {member.name}')
                expected = inventory[member.name]
                if member.size != expected['size']:
                    raise ValueError(f'Artifact size mismatch: {member.name}')
                seen.add(member.name)
                output = target / path
                output.parent.mkdir(parents=True, exist_ok=True)
                hasher = hashlib.sha256()
                with archive.extractfile(member) as source, output.open('wb') as stream:
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                        hasher.update(block)
                        stream.write(block)
                if hasher.hexdigest() != expected['sha256']:
                    raise ValueError(f'Artifact checksum mismatch: {member.name}')
        if seen != set(inventory):
            raise ValueError('Incomplete endpoint collection')
        write_json(target / 'verified-inventory.json', inventory)
        target.replace(destination)


class FleetBackend:
    """CPU work stays with its configured coordinator; only prepared model work crosses servers."""
    name = 'pool'

    def __init__(self, store: JobStore, config: dict, python: str | None = None):
        from msms_structure_elucidation.mcp.backends import LocalBackend, make_backend
        self.store, self.config = store, config
        settings = config.get('mcp', {})
        self.settings = settings
        coordinator = settings.get('coordinator_backend', 'local')
        if coordinator not in ('local', 'slurm'):
            raise ValueError('Pool coordinator_backend must be local or slurm')
        self.cpu = make_backend(coordinator, store, config, python)
        if hasattr(self.cpu, 'exclude_tasks'):
            self.cpu.exclude_tasks = {'model_inference'}
        self.cpu.child_cancel = self.cancel
        self.endpoints = {name: Endpoint(name, endpoint) for name, endpoint in (settings.get('servers') or {}).items()
                          if endpoint.get('enabled', True)}
        if not self.endpoints:
            raise ValueError('mcp.backend: pool requires at least one enabled mcp.servers entry')
        self.coordinator_id = hashlib.sha256((str(store.root) + socket.gethostname()).encode()).hexdigest()
        self.snapshots = {}

    @property
    def dispatcher(self) -> bool:
        """Scientific CPU workers publish work; only the owning MCP process uses endpoint credentials."""
        owner = self.config.get('_mcp_dispatcher')
        return not owner or owner == {'host': socket.gethostname(), 'pid': os.getpid()}

    def submit(self, job_id: str) -> None:
        if not self.dispatcher:
            return
        if self.store.request(job_id)['task'] == 'model_inference':
            self.pump()
        else:
            self.cpu.submit(job_id)

    def _probe(self, name: str) -> dict:
        cached = self.snapshots.get(name)
        if cached and time.monotonic() - cached[0] < float(self.settings.get('poll_seconds', 15)):
            return cached[1]
        snapshot = self.endpoints[name].call('probe')
        self.snapshots[name] = (time.monotonic(), snapshot)
        return snapshot

    def capacity(self) -> int:
        """Bound look-ahead by configured endpoint slots; scheduler queues default to one."""
        slots = 0
        if not self.dispatcher:
            return int(self.config.get('_mcp_pool_capacity') or sum(
                1 if endpoint.settings.get('max_jobs', 'auto') == 'auto' else int(endpoint.settings['max_jobs'])
                for endpoint in self.endpoints.values()))
        for name, endpoint in self.endpoints.items():
            configured = endpoint.settings.get('max_jobs', 'auto')
            if configured != 'auto':
                slots += max(1, int(configured))
            else:
                try:
                    slots += max(1, int(self._probe(name).get('max_concurrent_jobs', 1)))
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    slots += 1
        return max(1, slots)

    def pump(self) -> None:
        if not self.dispatcher:
            return
        if hasattr(self.cpu, 'pump'):
            self.cpu.pump()
        with file_lock(self.store.root / 'fleet.lock'):
            jobs = [row for row in self.store.active() if row['task'] == 'model_inference']
            for marker in (self.store.root / 'cleanup-pending').glob('*'):
                jobs.append({'job_id': marker.name, 'task': 'model_inference', **self.store.status(marker.name)})
            for row in jobs:
                if row['state'] == 'done' and self.store.status(row['job_id']).get('remote_cleanup_pending'):
                    job_id = row['job_id']
                    receipt = read_json(self.store.path(job_id) / 'collection-receipt.json')
                    if receipt:
                        try:
                            self.endpoints[receipt['server']].call('cleanup', {
                                'remote_job_id': receipt['remote_job_id'],
                                'acknowledged_inventory': receipt['inventory']})
                            self.store.set_status(job_id, 'done', remote_cleanup_pending=False, remote_cleanup_error=None)
                            (self.store.root / 'cleanup-pending' / job_id).unlink(missing_ok=True)
                        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                            self.store.set_status(job_id, 'done', remote_cleanup_error=str(exc)[-2000:])
            # Reconcile assignments first. A transport failure leaves ownership intact;
            # dispatching another copy would waste compute and risk conflicting outputs.
            for row in jobs:
                if row['state'] in ACTIVE and self.store.launch(row['job_id']).get('server'):
                    try:
                        params = self.store.request(row['job_id'])['params']
                        parent = params.get('_parent_job')
                        if self.store.status(row['job_id']).get('cancel_requested') or (
                                parent and self.store.status(parent)['state'] not in ACTIVE):
                            self.cancel(row['job_id'])
                        else:
                            self._poll_remote(row['job_id'])
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        self.store.set_status(row['job_id'], self.store.status(row['job_id'])['state'],
                                              transport_error=str(exc)[-2000:])
            active = [row for row in jobs if self.store.status(row['job_id'])['state'] in ACTIVE]
            for row in reversed(jobs):
                job_id = row['job_id']
                if self.store.status(job_id)['state'] != 'queued' or self.store.launch(job_id).get('server'):
                    continue
                if self.store.status(job_id).get('cancel_requested'):
                    self.store.set_status(job_id, 'cancelled', finished=now())
                    continue
                params = self.store.request(job_id)['params']
                parent = params.get('_parent_job')
                if parent and self.store.status(parent)['state'] not in ACTIVE:
                    self.store.set_status(job_id, 'cancelled', finished=now())
                    continue
                choices = []
                errors = {}
                for name, endpoint in self.endpoints.items():
                    if params['arguments']['model'] not in endpoint.settings.get('models', ['iceberg', 'glacier']):
                        continue
                    count = sum(self.store.launch(item['job_id']).get('server') == name for item in active)
                    try:
                        probe = self._probe(name)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors[name] = str(exc)[-1000:]
                        continue
                    configured = endpoint.settings.get('max_jobs', 'auto')
                    limit = probe.get('max_concurrent_jobs', 1) if configured == 'auto' else configured
                    if count >= int(limit):
                        continue
                    if endpoint.settings.get('backend', 'local') == 'local' and not probe.get('free_gpus'):
                        continue
                    try:
                        endpoint.call('validate', {'params': params})
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors[name] = str(exc)[-1000:]
                        continue
                    # Prefer observed idle GPUs; otherwise use bounded scheduler queues.
                    score = (0 if probe.get('free_gpus') else 1, count,
                             -float(endpoint.settings.get('priority', 0)))
                    choices.append((score, name))
                if not choices:
                    self.store.set_status(job_id, 'queued', dispatch_errors=errors or None)
                    continue
                _, name = min(choices)
                endpoint = self.endpoints[name]
                # Publish ownership before network I/O. Lost replies are recovered by
                # the endpoint's stable submission token, never sent to a second host.
                self.store.set_launch(job_id, backend=self.name, server=name, remote_job_id=None)
                self.store.set_status(job_id, 'submitted', server=name, submitted=now())
                try:
                    self._submit_remote(job_id)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self.store.set_status(job_id, 'submitted', transport_error=str(exc)[-2000:])
                self.snapshots.pop(name, None)

    def _submit_remote(self, job_id: str) -> None:
        launch = self.store.launch(job_id)
        params = self.store.request(job_id)['params']
        endpoint = self.endpoints[launch['server']]
        result = endpoint.call('submit', {'coordinator': self.coordinator_id, 'job_id': job_id,
            'attempt': self.store.request(job_id).get('attempt'), 'params': params,
            'prepared': read_json(Path(params['prepared'])) if params.get('prepared') else None})
        self.store.set_launch(job_id, **{**launch, 'remote_job_id': result['job_id']})

    def _poll_remote(self, job_id: str) -> dict:
        launch = self.store.launch(job_id)
        if not launch.get('remote_job_id'):
            self._submit_remote(job_id)
            launch = self.store.launch(job_id)
        endpoint = self.endpoints[launch['server']]
        receipt = endpoint.call('poll', {'remote_job_id': launch['remote_job_id']})
        status = receipt['status']
        if receipt.get('scheduler_records'):
            write_json(self.store.path(job_id) / 'scheduler-records.json', receipt['scheduler_records'])
        if status['state'] == 'done':
            destination = self.store.path(job_id) / 'collected'
            if not destination.exists():
                collect(endpoint, launch['remote_job_id'], receipt['inventory'], destination)
            elif read_json(destination / 'verified-inventory.json') != receipt['inventory']:
                raise RuntimeError('Remote artifacts changed after collection')
            write_json(self.store.path(job_id) / 'result.json', {'save_dir': str(destination / 'native_outputs')})
            # Save acknowledgement locally before cleanup; a cleanup failure never loses results.
            write_json(self.store.path(job_id) / 'collection-receipt.json',
                       {'server': endpoint.name, 'remote_job_id': launch['remote_job_id'], 'inventory': receipt['inventory']})
            if endpoint.settings.get('cleanup', True):
                pending = self.store.root / 'cleanup-pending'
                pending.mkdir(exist_ok=True)
                (pending / job_id).touch()
            self.store.set_status(job_id, 'done', server=endpoint.name, finished=now(),
                                  remote_cleanup_pending=endpoint.settings.get('cleanup', True), transport_error=None)
            cleanup_error = None
            if endpoint.settings.get('cleanup', True):
                try:
                    endpoint.call('cleanup', {'remote_job_id': launch['remote_job_id'],
                                             'acknowledged_inventory': receipt['inventory']})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    cleanup_error = str(exc)[-2000:]
            if not cleanup_error:
                (self.store.root / 'cleanup-pending' / job_id).unlink(missing_ok=True)
            return self.store.set_status(job_id, 'done', server=endpoint.name, finished=now(),
                                         remote_cleanup_error=cleanup_error,
                                         remote_cleanup_pending=bool(cleanup_error), transport_error=None)
        if status['state'] in ('failed', 'cancelled'):
            (self.store.path(job_id) / 'remote-error.log').write_text(receipt.get('log_tail') or '')
        return self.store.set_status(job_id, status['state'], server=endpoint.name,
            scheduler_state=status.get('scheduler_state'), scheduler_reason=status.get('scheduler_reason'),
            error=status.get('error'), transport_error=None)

    def poll(self, job_id: str) -> dict:
        self.pump()
        if self.store.request(job_id)['task'] != 'model_inference':
            return self.cpu.poll(job_id)
        return self.store.status(job_id)

    def cancel(self, job_id: str) -> dict:
        if not self.dispatcher:
            return self.store.set_status(job_id, self.store.status(job_id)['state'], cancel_requested=True)
        for child in self.store.children(job_id):
            if self.store.status(child)['state'] in ACTIVE:
                self.cancel(child)
        if self.store.request(job_id)['task'] != 'model_inference':
            return self.cpu.cancel(job_id)
        launch = self.store.launch(job_id)
        if launch.get('server'):
            self.store.set_status(job_id, self.store.status(job_id)['state'], cancel_requested=True)
            try:
                if not launch.get('remote_job_id'):
                    self._submit_remote(job_id)
                    launch = self.store.launch(job_id)
                endpoint = self.endpoints[launch['server']]
                if endpoint.transport == 'kubernetes':
                    endpoint.archive_cancellation(launch['remote_job_id'],
                        self.store.path(job_id) / 'cancellation-scheduler-records.json')
                result = endpoint.call('cancel', {'remote_job_id': launch['remote_job_id']})
                if result and result.get('state') in ACTIVE:
                    return self.store.set_status(job_id, self.store.status(job_id)['state'],
                        cancel_requested=True, scheduler_state=result.get('scheduler_state'))
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                return self.store.set_status(job_id, self.store.status(job_id)['state'],
                    cancel_requested=True, transport_error=f'Cancellation awaits endpoint confirmation: {exc}'[-2000:])
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        servers = {}
        for name in self.endpoints:
            try:
                servers[name] = self._probe(name)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                servers[name] = {'error': str(exc)[-1000:]}
        self.config['_mcp_pool_capacity'] = sum(max(1, int(
            servers[name].get('max_concurrent_jobs', 1) if endpoint.settings.get('max_jobs', 'auto') == 'auto'
            else endpoint.settings['max_jobs'])) for name, endpoint in self.endpoints.items())
        return {'backend': self.name, 'coordinator': self.cpu.describe(), 'servers': servers}
