"""Bounded Kubernetes Jobs with persistent workspaces and explicit context/namespace."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, now, write_json, WORKER_OWNED_TASKS, VIRTUAL_BACKENDS
from msms_structure_elucidation.prediction_cache import file_lock


def kubectl(settings: dict, *args: str, **kwargs) -> subprocess.CompletedProcess:
    """Never depend on the user's current context or namespace."""
    for key in ('context', 'namespace'):
        if not settings.get(key):
            raise ValueError(f'mcp.kubernetes.{key} is required')
    return subprocess.run([settings.get('kubectl', 'kubectl'), '--context', settings['context'],
        '-n', settings['namespace'], *args], capture_output=True, text=True, timeout=60, **kwargs)


def job_manifest(settings: dict, name: str, command: list[str], kind: str = 'gpu') -> dict:
    """Build a finite Job; image, mounts, resource names and affinity are user configuration."""
    if not settings.get('image'):
        raise ValueError('mcp.kubernetes.image must contain the installed package and model runtime')
    if int(settings.get('deadline_seconds', 7200)) < 1 or int(settings.get('gpus', 1)) < 1:
        raise ValueError('Kubernetes deadline and GPU count must be positive')
    resources = dict(settings.get('resources') or {'requests': {'cpu': '4', 'memory': '8Gi'},
                                                 'limits': {'cpu': '4', 'memory': '8Gi'}})
    resources = json.loads(json.dumps(resources))
    gpu_resource = settings.get('gpu_resource', 'nvidia.com/gpu')
    for resource_kind in ('requests', 'limits'):
        resources.setdefault(resource_kind, {})
        if kind == 'gpu':
            resources[resource_kind][gpu_resource] = int(settings.get('gpus', 1))
        else:
            resources[resource_kind].pop(gpu_resource, None)
    pod = {'restartPolicy': 'Never',
        'automountServiceAccountToken': bool(settings.get('automount_service_account_token', False)),
        'containers': [{'name': 'runner', 'image': settings['image'],
        'command': [*settings.get('command_prefix', []), *command], 'resources': resources, 'volumeMounts': settings.get('volume_mounts', []),
        'env': settings.get('env', [])}], 'volumes': settings.get('volumes', [])}
    for key in ('nodeSelector', 'affinity', 'tolerations', 'imagePullSecrets', 'serviceAccountName'):
        if settings.get(key):
            pod[key] = settings[key]
    if settings.get('working_dir'):
        pod['containers'][0]['workingDir'] = settings['working_dir']
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'labels': {'app.kubernetes.io/managed-by': 'msms-mcp'}},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': int(settings.get('deadline_seconds', 7200)),
                 'template': {'metadata': {'labels': {'app.kubernetes.io/managed-by': 'msms-mcp'}}, 'spec': pod}}}


def inventory_summary(resource: str, data: dict, gpu_resource: str = 'nvidia.com/gpu') -> dict:
    """Keep resource observations small and exclude pod environments and node annotations."""
    if data.get('error'):
        return data
    items = data.get('items', [])
    if resource == 'resourcequota':
        return {'items': [{'name': item.get('metadata', {}).get('name'),
                           'hard': item.get('status', {}).get('hard', {}),
                           'used': item.get('status', {}).get('used', {})} for item in items]}
    if resource == 'nodes':
        products = {}
        ready = schedulable = 0
        for item in items:
            ok = any(c.get('type') == 'Ready' and c.get('status') == 'True'
                     for c in item.get('status', {}).get('conditions', []))
            ready += ok
            schedulable += ok and not item.get('spec', {}).get('unschedulable', False)
            labels = item.get('metadata', {}).get('labels', {})
            product = labels.get('nvidia.com/gpu.product', 'unspecified')
            amount = int(item.get('status', {}).get('allocatable', {}).get(gpu_resource, 0))
            if amount:
                products[product] = products.get(product, 0) + amount
        return {'nodes': len(items), 'ready_nodes': ready, 'ready_uncordoned_nodes': schedulable,
                'gpu_resource': gpu_resource, 'allocatable_by_product': products,
                'availability': 'Allocatable includes GPUs in use; taints, reservations and quotas still apply'}
    phases = {}
    requested = 0
    for item in items:
        phase = item.get('status', {}).get('phase', 'Unknown')
        phases[phase] = phases.get(phase, 0) + 1
        if phase in ('Pending', 'Running'):
            containers = item.get('spec', {}).get('containers', [])
            requested += sum(int(c.get('resources', {}).get('requests', {}).get(gpu_resource, 0)) for c in containers)
    return {'pods': len(items), 'phases': phases, 'gpu_requests': requested,
            'scope': 'Configured namespace only; other namespaces are not included'}


class KubernetesBackend:
    """The JobStore must be on storage mounted at the same path in runner pods."""
    name = 'kubernetes'

    def __init__(self, store: JobStore, config: dict, python: str | None = None):
        self.store, self.python = store, python
        self.config = config
        self.exclude_tasks: set[str] = set()
        self.settings = config.get('mcp', {}).get('kubernetes') or {}
        from msms_structure_elucidation.mcp.admission import Admission
        self.admission = Admission(store, config)
        from msms_structure_elucidation.mcp.backends import pipeline_limit
        from msms_structure_elucidation.mcp.cpu_pool import cpu_job_limit
        cpu_budget = cpu_job_limit(config.get('mcp', {}), self.capacity())
        self.max_live_pipelines = pipeline_limit(config.get('mcp', {}), cpu_default=cpu_budget)
        if not self.settings.get('shared_workspace'):
            raise ValueError('Kubernetes needs shared_workspace: true and a mounted persistent job root')

    @property
    def dispatcher(self) -> bool:
        owner = self.config.get('_mcp_dispatcher')
        return not owner or owner == {'host': socket.gethostname(), 'pid': os.getpid()}

    def capacity(self) -> int:
        """Bound worker prediction groups by the configured cluster GPU-job budget."""
        value = self.config.get('mcp',{}).get('max_concurrent_jobs','auto')
        return 1 if value in (None, 'auto') else max(1,int(value))

    def pump(self) -> None:
        """Only the MCP coordinator uses the API; CPU pods publish requests on shared storage."""
        if not self.dispatcher or self.settings.get('external_scheduler'):
            return
        with file_lock(self.store.root / 'kubernetes-scheduler.lock'):
            for row in self.store.active():
                if (row['task'] in WORKER_OWNED_TASKS or
                        self.store.launch(row['job_id']).get('backend') in VIRTUAL_BACKENDS):
                    continue
                status = self.store.status(row['job_id'])
                if status['state'] in ACTIVE:
                    parent = self.store.request(row['job_id'])['params'].get('_parent_job')
                    if status.get('cancel_requested') or (parent and self.store.status(parent)['state'] not in ACTIVE):
                        self.cancel(row['job_id'])
                    elif status['state'] == 'queued':
                        if row['task'] not in self.exclude_tasks:
                            self.submit(row['job_id'])
                    else:
                        self._poll(row['job_id'])
                elif status['state'] == 'done' and not status.get('scheduler_confirmed'):
                    self._poll(row['job_id'])

    def submit(self, job_id: str) -> None:
        if not self.dispatcher:
            return
        request = self.store.request(job_id)
        if request['task'] in WORKER_OWNED_TASKS:
            return
        if request.get('kind') != 'gpu':
            live = sum(r.get('kind') != 'gpu' and r['state'] in ('submitted', 'running', 'done')
                       and self.store.launch(r['job_id']).get('backend') not in ('feature-pipeline','feature-worker','cpu-worker')
                       for r in self.store.active())
            if live >= self.max_live_pipelines or not self.admission.admit(job_id):
                return
        from msms_structure_elucidation.mcp.backends import runner_command
        folder = self.store.path(job_id)
        name = 'msms-' + job_id.replace('_', '-')
        manifest = job_manifest(self.settings, name, runner_command(folder, self.settings.get('python', 'python')),
                                self.store.request(job_id).get('kind', 'gpu'))
        write_json(folder / 'kubernetes-job.json', manifest)
        # Record the deterministic name before submission, permitting recovery after connection loss.
        self.store.set_launch(job_id, backend=self.name, kubernetes_job=name)
        if self.settings.get('external_scheduler'):
            if self.config.get('mcp',{}).get('feature_worker'):
                marker = self.store.root/'external-scheduler'
                marker.mkdir(exist_ok=True)
                (marker/job_id).touch()
            self.store.set_status(job_id, 'submitted', kubernetes_job=name, submitted=now())
            return
        result = kubectl(self.settings, 'create', '-f', '-', input=json.dumps(manifest))
        if result.returncode and 'AlreadyExists' not in result.stderr:
            raise RuntimeError(f'Kubernetes submission failed: {result.stderr[-2000:]}')
        self.store.set_status(job_id, 'submitted', kubernetes_job=name, submitted=now(), admission_hold=None)

    def archive(self, job_id: str, require_logs: bool = True) -> dict:
        """Archive scheduler records and logs before any object deletion."""
        name = self.store.launch(job_id)['kubernetes_job']
        records = {}
        for label, args in {'job': ('get', 'job', name, '-o', 'json'),
                            'pods': ('get', 'pods', '-l', f'job-name={name}', '-o', 'json'),
                            'logs': ('logs', f'job/{name}', '--all-containers=true')}.items():
            result = kubectl(self.settings, *args)
            if result.returncode and (label != 'logs' or require_logs):
                raise RuntimeError(f'Cannot archive Kubernetes {label}: {result.stderr[-1000:]}')
            (self.store.path(job_id) / f'kubernetes-{label}.txt').write_text(result.stdout or result.stderr)
            records[label] = json.loads(result.stdout) if label != 'logs' else result.stdout
        return records

    def poll(self, job_id: str) -> dict:
        self.pump()
        return self._poll(job_id)

    def _poll(self, job_id: str) -> dict:
        status = self.store.status(job_id)
        if not self.dispatcher:
            if status['state'] == 'done' and not status.get('scheduler_confirmed'):
                return {**status, 'state': 'running', 'scheduler_state': 'Awaiting coordinator confirmation'}
            return status
        if status['state'] == 'done' and status.get('scheduler_confirmed'):
            return status
        if self.settings.get('external_scheduler'):
            return status
        name = self.store.launch(job_id).get('kubernetes_job')
        if not name or status['state'] in ('failed', 'cancelled'):
            return status
        result = kubectl(self.settings, 'get', 'job', name, '-o', 'json')
        if result.returncode:
            raise RuntimeError(f'Kubernetes status unavailable; retaining job: {result.stderr[-1000:]}')
        job = json.loads(result.stdout)
        conditions = {c['type'] for c in job.get('status', {}).get('conditions', []) if c['status'] == 'True'}
        if conditions & {'Complete', 'Failed'}:
            records = self.archive(job_id)
            pods = records['pods'].get('items', [])
            success = bool(pods) and all(p.get('status', {}).get('phase') == 'Succeeded' and
                bool(p['status'].get('containerStatuses')) and all(
                    c.get('state', {}).get('terminated', {}).get('exitCode') == 0
                    for c in p['status']['containerStatuses']) for p in pods)
            if 'Failed' in conditions or not success or status['state'] != 'done':
                return self.store.set_status(job_id, 'failed', finished=now(),
                    error='Kubernetes Job/Pod/exit code/result did not all confirm success')
            status = self.store.set_status(job_id, 'done', scheduler_confirmed=True)
            self.admission.record(job_id, 'done')
            return status
        # A runner result alone cannot establish Kubernetes completion.
        return {**status, 'state': 'running' if status['state'] == 'done' else status['state'],
                'scheduler_state': 'Running' if job.get('status', {}).get('active') else 'Pending'}

    def cleanup(self, job_id: str) -> None:
        """Called only after collection is acknowledged by the coordinator."""
        if self.settings.get('external_scheduler'):
            return
        self.archive(job_id)
        result = kubectl(self.settings, 'delete', 'job', self.store.launch(job_id)['kubernetes_job'], '--wait=true')
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])

    def cancel(self, job_id: str) -> dict:
        if not self.dispatcher:
            return self.store.set_status(job_id, self.store.status(job_id)['state'], cancel_requested=True)
        name = self.store.launch(job_id).get('kubernetes_job')
        if name and not self.settings.get('external_scheduler'):
            self.archive(job_id, require_logs=False)
            result = kubectl(self.settings, 'delete', 'job', name, '--wait=true', '--ignore-not-found=true')
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
        return self.store.set_status(job_id, 'cancelled', finished=now())

    def describe(self) -> dict:
        if self.settings.get('external_scheduler'):
            return {'backend': self.name, 'availability': 'scheduler-controlled'}
        inventory = {}
        for label, args in {'quota': ('get', 'resourcequota', '-o', 'json'),
                            'nodes': ('get', 'nodes', '-o', 'json'),
                            'pods': ('get', 'pods', '-o', 'json')}.items():
            result = kubectl(self.settings, *args)
            inventory[label] = inventory_summary(args[1], json.loads(result.stdout), self.settings.get(
                'gpu_resource', 'nvidia.com/gpu')) if not result.returncode else {'error': result.stderr[-1000:]}
        # Requested resources and node capacity are observations, never promises of idle capacity.
        return {'backend': self.name, 'inventory': inventory,
                'admission': self.admission.summary(),
                'availability': 'scheduler-controlled', 'context': self.settings.get('context'),
                'namespace': self.settings.get('namespace')}
