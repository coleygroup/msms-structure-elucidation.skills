"""Dispatch prepared prediction groups to a configured pool of compatible servers."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, now, read_json, write_json
from msms_structure_elucidation.mcp.jobs import timestamp
from msms_structure_elucidation.mcp.network import NetworkQueue, Pending
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
        prefetch = settings.get('prefetch_jobs', 'auto')
        if prefetch != 'auto' and int(prefetch) < 0:
            raise ValueError('Endpoint prefetch_jobs must be nonnegative or auto')
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
            if self.settings.get('prefix'):
                body = self.settings['prefix'] + ' ' + shlex.join(command)
                if self.settings.get('repo'):
                    body = f'cd {shlex.quote(self.settings["repo"])} && ' + body
                return ['bash', '-lc', body]
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
        cwd = self.settings.get('repo') if self.transport == 'local' else None
        if output is None:
            done = subprocess.run(self.command(operation), input=data, text=True, capture_output=True, timeout=timeout, cwd=cwd)
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
                if operation.startswith('worker-'):
                    if operation!='worker-scheduler-update':
                        from msms_structure_elucidation.mcp.worker_scheduler import sync
                        result = sync(self,result,(payload or {}).get('model','glacier'))
                    if operation=='worker-probe':
                        result = self._scheduler('probe',payload or {},result)
                else:
                    result = self._scheduler(operation, payload or {}, result)
            return result
        with output.open('wb') as stream:
            done = subprocess.run(self.command(operation), input=data.encode(), stdout=stream,
                                  stderr=subprocess.PIPE, timeout=float(self.settings.get('transfer_timeout_seconds', 1800)), cwd=cwd)
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


def collect(endpoint: Endpoint, remote_job: str, inventory: dict, destination: Path, operation: str = 'bundle') -> None:
    """Stream, validate paths, sizes and hashes, then atomically publish a local result tree."""
    with tempfile.TemporaryDirectory(prefix='collection-', dir=destination.parent) as temp:
        folder = Path(temp)
        archive_path = folder / 'outputs.tar'
        endpoint.call(operation, {'remote_job_id': remote_job}, output=archive_path)
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
        self.coordinator_id = settings.get('coordinator_identity') or hashlib.sha256(
            (str(store.root) + socket.gethostname()).encode()).hexdigest()
        if not re.fullmatch('[0-9a-f]{64}', self.coordinator_id):
            raise ValueError('coordinator_identity must be a stable 64-character hexadecimal token')
        self.role = os.environ.get('MSMS_MCP_ROLE') or settings.get('dispatcher_role', 'owner')
        if self.role not in ('owner', 'observer'):
            raise ValueError('MCP dispatcher_role must be owner or observer')
        self.snapshots = {}
        self.asynchronous = bool(settings.get('pool_async_dispatch', True))
        self.network = NetworkQueue(int(settings.get('pool_rpc_workers', 4)),
                                    int(settings.get('pool_transfer_workers', 2)),
                                    int(settings.get('pool_submission_workers', 2))) if self.asynchronous else None
        self.rpc_workers_per_server = int(settings.get('pool_rpc_workers_per_server', 0))
        self.submission_workers_per_server = int(settings.get('pool_submission_workers_per_server', 1))
        if self.rpc_workers_per_server < 0:
            raise ValueError('pool_rpc_workers_per_server must be nonnegative')
        if self.submission_workers_per_server < 1:
            raise ValueError('pool_submission_workers_per_server must be positive')
        self.server_rpc = {}
        self.validated = set()
        self.validation_errors = {}

    def _call(self, endpoint: Endpoint, operation: str, payload: dict | None = None, identity: str = ''):
        """Endpoint I/O never holds the queue lock while an asynchronous operation runs."""
        if self.network is None:
            return endpoint.call(operation, payload)
        queue = self.network
        if self.rpc_workers_per_server:
            group = endpoint.settings.get('resource_group',endpoint.name)
            if group not in self.server_rpc:
                self.server_rpc[group] = NetworkQueue(self.rpc_workers_per_server,1,self.submission_workers_per_server)
            queue = self.server_rpc[group]
        return queue.call((operation, endpoint.name, identity), lambda: endpoint.call(operation, payload))

    @property
    def dispatcher(self) -> bool:
        """Scientific CPU workers publish work; only the owning MCP process uses endpoint credentials."""
        owner = self.config.get('_mcp_dispatcher')
        return self.role == 'owner' and (not owner or owner == {'host': socket.gethostname(), 'pid': os.getpid()})

    def submit(self, job_id: str) -> None:
        if not self.dispatcher:
            if self.role == 'observer' and self.store.request(job_id)['task'] != 'model_inference':
                self.cpu.submit(job_id)
            return
        if self.store.request(job_id)['task'] == 'model_inference':
            self.pump()
        else:
            self.cpu.submit(job_id)

    def _probe(self, name: str, asynchronous: bool = False) -> dict:
        cached = self.snapshots.get(name)
        if cached and time.monotonic() - cached[0] < float(self.settings.get('poll_seconds', 15)):
            return cached[1]
        endpoint = self.endpoints[name]
        operation = 'worker-probe' if endpoint.settings.get('features') else 'probe'
        payload = {'model':endpoint.settings['models'][0]} if operation=='worker-probe' else None
        arguments = (operation,payload) if payload is not None else (operation,)
        snapshot = self._call(endpoint,*arguments) if asynchronous else endpoint.call(*arguments)
        self.snapshots[name] = (time.monotonic(), snapshot)
        return snapshot

    def capacity(self) -> int:
        """Bound look-ahead by configured endpoint slots; scheduler queues default to one."""
        slots = {}
        if not self.dispatcher:
            for name, endpoint in self.endpoints.items():
                group = endpoint.settings.get('resource_group', name)
                value = endpoint.settings.get('max_jobs', 'auto')
                slots[group] = max(slots.get(group, 0), 1 if value == 'auto' else int(value))
            return int(self.config.get('_mcp_pool_capacity') or sum(slots.values()))
        for name, endpoint in self.endpoints.items():
            group = endpoint.settings.get('resource_group', name)
            configured = endpoint.settings.get('max_jobs', 'auto')
            if configured != 'auto':
                value = max(1, int(configured))
            else:
                try:
                    value = max(1, int(self._probe(name).get('max_concurrent_jobs', 1)))
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    value = 1
            slots[group] = max(slots.get(group, 0), value)
        return max(1, sum(slots.values()))

    def pump(self) -> None:
        if not self.dispatcher:
            return
        if hasattr(self.cpu, 'pump'):
            self.cpu.pump()
        with file_lock(self.store.root / 'fleet.lock'):
            jobs = [row for row in self.store.active() if row['task'] == 'model_inference']
            for marker in (self.store.root / 'cleanup-pending').glob('*'):
                jobs.append({'job_id': marker.name, 'task': 'model_inference', **self.store.status(marker.name)})
            if self.network:
                self.network.discard_finished({row['job_id'] for row in jobs})
            for row in jobs:
                if row['state'] == 'done' and self.store.status(row['job_id']).get('remote_cleanup_pending'):
                    job_id = row['job_id']
                    receipt = read_json(self.store.path(job_id) / 'collection-receipt.json')
                    if receipt:
                        try:
                            self._call(self.endpoints[receipt['server']], 'cleanup', {
                                'remote_job_id': receipt['remote_job_id'],
                                'acknowledged_inventory': receipt['inventory']}, job_id)
                            self.store.set_status(job_id, 'done', remote_cleanup_pending=False, remote_cleanup_error=None)
                            (self.store.root / 'cleanup-pending' / job_id).unlink(missing_ok=True)
                        except Pending:
                            pass
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
                    except Pending:
                        pass
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        self.store.set_status(row['job_id'], self.store.status(row['job_id'])['state'],
                                              transport_error=str(exc)[-2000:])
            active = [row for row in jobs if self.store.status(row['job_id'])['state'] in ACTIVE]
            for row in sorted(jobs, key=lambda r: (timestamp(r.get('created')), r['job_id'])):
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
                    if endpoint.settings.get('features'):
                        continue
                    if params['arguments']['model'] not in endpoint.settings.get('models', ['iceberg', 'glacier']):
                        continue
                    group = endpoint.settings.get('resource_group', name)
                    aliases = {key for key, value in self.endpoints.items()
                               if value.settings.get('resource_group', key) == group}
                    count = sum(self.store.launch(item['job_id']).get('server') in aliases for item in active)
                    try:
                        probe = self._probe(name, self.asynchronous)
                    except Pending:
                        continue
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors[name] = str(exc)[-1000:]
                        continue
                    configured = endpoint.settings.get('max_jobs', 'auto')
                    limit = int(probe.get('max_concurrent_jobs', 1) if configured == 'auto' else configured)
                    prefetch = endpoint.settings.get('prefetch_jobs', 'auto')
                    prefetch = (limit if self.asynchronous and endpoint.settings.get('backend', 'local') == 'local'
                                else 0) if prefetch == 'auto' else int(prefetch)
                    if count >= limit + prefetch:
                        continue
                    if endpoint.settings.get('backend', 'local') == 'local' and not probe.get('free_gpus') and not prefetch:
                        continue
                    try:
                        identity = hashlib.sha256(json.dumps({
                            'weights': params.get('weights'), 'native_source': params.get('native_source'),
                            'adapter_source': params.get('adapter_source'),
                            'model': params['arguments']['model'], 'sparse_k': params['arguments'].get('sparse_k')
                        }, sort_keys=True).encode()).hexdigest()
                        rejected = self.validation_errors.get((name, identity))
                        if rejected and time.monotonic() - rejected[0] < float(self.settings.get('poll_seconds', 15)):
                            errors[name] = rejected[1]
                            continue
                        if (name, identity) not in self.validated:
                            self._call(endpoint, 'validate', {'params': params}, identity)
                            self.validated.add((name, identity))
                            self.validation_errors.pop((name, identity), None)
                    except Pending:
                        continue
                    except IncompatibleEndpoint as exc:
                        errors[name] = str(exc)[-1000:]
                        self.validation_errors[name, identity] = (time.monotonic(), errors[name])
                        continue
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors[name] = str(exc)[-1000:]
                        continue
                    # Prefer observed idle GPUs; otherwise use bounded scheduler queues.
                    score = (0 if probe.get('free_gpus') and count < limit else 1, count / max(1, limit),
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
                except Pending:
                    pass
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self.store.set_status(job_id, 'submitted', transport_error=str(exc)[-2000:])
                if not self.asynchronous:
                    self.snapshots.pop(name, None)

    def _submit_remote(self, job_id: str) -> None:
        launch = self.store.launch(job_id)
        endpoint = self.endpoints[launch['server']]
        def submit():
            request = self.store.request(job_id)
            params = request['params']
            return endpoint.call('submit', {'coordinator': self.coordinator_id, 'job_id': job_id,
                'attempt': request.get('attempt'), 'params': params,
                'prepared': read_json(Path(params['prepared'])) if params.get('prepared') else None})
        result = self.network.call(('submit', endpoint.name, job_id), submit) if self.network else submit()
        self.store.set_launch(job_id, **{**launch, 'remote_job_id': result['job_id']})

    def _poll_remote(self, job_id: str) -> dict:
        launch = self.store.launch(job_id)
        if not launch.get('remote_job_id'):
            self._submit_remote(job_id)
            launch = self.store.launch(job_id)
        endpoint = self.endpoints[launch['server']]
        completion = self.store.path(job_id) / 'remote-completion.json'
        receipt = read_json(completion) or self._call(endpoint, 'poll', {'remote_job_id': launch['remote_job_id']}, job_id)
        status = receipt['status']
        if receipt.get('scheduler_records'):
            write_json(self.store.path(job_id) / 'scheduler-records.json', receipt['scheduler_records'])
        if status['state'] == 'done':
            if not completion.exists():
                write_json(completion, receipt)
                self.store.set_phase(job_id, 'result_collection')
            destination = self.store.path(job_id) / 'collected'
            def action():
                if not destination.exists():
                    collect(endpoint, launch['remote_job_id'], receipt['inventory'], destination)
            if self.network:
                self.network.call(('collect', endpoint.name, job_id),action,transfer=True)
            else:
                action()
            if read_json(destination / 'verified-inventory.json') != receipt['inventory']:
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
                    self._call(endpoint, 'cleanup', {'remote_job_id': launch['remote_job_id'],
                                             'acknowledged_inventory': receipt['inventory']}, job_id)
                except Pending:
                    return self.store.status(job_id)
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
                servers[name] = self._probe(name, asynchronous=self.asynchronous)
            except Pending:
                cached = self.snapshots.get(name)
                servers[name] = cached[1] if cached else {'state': 'probing'}
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                servers[name] = {'error': str(exc)[-1000:]}
        slots = {}
        for name, endpoint in self.endpoints.items():
            group = endpoint.settings.get('resource_group', name)
            value = servers[name].get('max_concurrent_jobs', 1) if endpoint.settings.get('max_jobs', 'auto') == 'auto' else endpoint.settings['max_jobs']
            slots[group] = max(slots.get(group, 0), max(1, int(value)))
        self.config['_mcp_pool_capacity'] = sum(slots.values())
        groups = [row for row in self.store.active() if row['task'] == 'model_inference']
        workload = {'unassigned_groups': sum(not self.store.launch(row['job_id']).get('server') for row in groups),
                    'assigned_groups': sum(bool(self.store.launch(row['job_id']).get('server')) for row in groups),
                    'collecting_groups': sum(row.get('phase') == 'result_collection' for row in groups),
                    'transport': self.network.describe() if self.network else None}
        return {'backend': self.name, 'coordinator': self.cpu.describe(), 'servers': servers,
                'asynchronous_dispatch': self.asynchronous, 'dispatcher_role': self.role,
                'server_rpc': {name:queue.describe() for name,queue in self.server_rpc.items()},
                'prediction_workload': workload}

    def close(self) -> None:
        """Finish outstanding transport operations when an embedding coordinator shuts down."""
        for queue in self.server_rpc.values():
            queue.close(cancel_waiting_reads=True)
        if self.network:
            self.network.close(cancel_waiting_reads=True)
