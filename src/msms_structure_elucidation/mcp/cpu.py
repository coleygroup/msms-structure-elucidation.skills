"""Resource budgets and phase accounting for independent CPU feature jobs."""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
import math


def plan_concurrency(cpu_budget: float, memory_budget_gb: float, cpus_per_job: float,
                     memory_per_job_gb: float, max_jobs: int | None = None) -> dict:
    """Calculate a conservative capacity ceiling from an explicit CPU-parent budget."""
    values = {'cpu_budget': cpu_budget, 'memory_budget_gb': memory_budget_gb,
              'cpus_per_job': cpus_per_job, 'memory_per_job_gb': memory_per_job_gb}
    if any(isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in values.values()):
        raise ValueError('CPU budgets and per-job resources must be positive finite numbers')
    if max_jobs is not None and (isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs < 1):
        raise ValueError('max_jobs must be a positive integer')
    # Resource quantities are decimal inputs; avoid losing a slot to float division rounding.
    cpu_per_parent, memory_per_parent = Fraction(str(cpus_per_job)), Fraction(str(memory_per_job_gb))
    cpu_bound = Fraction(str(cpu_budget)) // cpu_per_parent
    memory_bound = Fraction(str(memory_budget_gb)) // memory_per_parent
    ceiling = min(cpu_bound, memory_bound)
    if max_jobs is not None:
        ceiling = min(ceiling, max_jobs)
    return {'scope': 'CPU feature parents, including resident waiters; model data-loader workers are separate',
            'inputs': {**values, 'max_jobs': max_jobs}, 'cpu_bound_jobs': cpu_bound,
            'memory_bound_jobs': memory_bound, 'capacity_ceiling_jobs': ceiling,
            'reserved_at_ceiling': {'cpus': float(ceiling * cpu_per_parent),
                                    'memory_gb': float(ceiling * memory_per_parent)},
            'mcp_settings': {'max_cpu_jobs': ceiling, 'max_live_pipelines': ceiling} if ceiling else None,
            'status': 'capacity_plan' if ceiling else 'insufficient_budget',
            'notes': ['Reserve shared-host, GPU-worker and operating-system headroom before supplying budgets.',
                      'Use a measured per-parent memory peak with headroom, including scientific subprocesses.',
                      'This is a capacity ceiling, not a measured throughput optimum; benchmark before applying.',
                      'Slurm CPU parents retain allocations while waiting for GPU or cached predictions.']}


def workload(rows: list[dict], config: dict, coordinator) -> dict:
    """Distinguish useful CPU work from scheduler queues and resident prediction waiters."""
    from msms_structure_elucidation.mcp.admission import request_backlog
    virtual = [row for row in rows if row.get('execution_backend') in ('feature-pipeline','feature-worker')]
    pending = request_backlog(virtual)
    logical = [row for row in rows if row.get('execution_backend') == 'cpu-worker']
    parents = [row for row in rows if row.get('kind', 'gpu') != 'gpu' and row not in virtual and row not in logical]
    states = Counter(row['state'] for row in parents)
    phases = Counter(row.get('phase') or 'unreported' for row in parents if row['state'] == 'running')
    settings = config.get('mcp', {})
    slurm = settings.get('slurm') or {}
    resources = {**slurm, **{key: value for key, value in (slurm.get('cpu') or {}).items()
                             if value not in (None, '')}}
    model = config.get('models', {}).get('simulator', {})
    from msms_structure_elucidation.mcp.cpu_pool import limits
    budgets = limits(settings) if settings.get('feature_pipeline', False) else {}
    return {'scope': 'Durable job states on this coordinator; running jobs are not CPU utilization samples',
            'feature_requests': len(virtual), 'feature_request_phases': dict(Counter(row.get('phase', 'queued') for row in virtual)),
            'feature_execution_pending': pending['execution_pending'],
            'feature_postprediction_pending': pending['postprediction_pending'],
            'feature_delivery_pending': pending['delivery_pending'],
            'cpu_pool_tasks': len(logical),
            'coordinator_backend': coordinator.name,
            'queued_jobs': states['queued'], 'submitted_jobs': states['submitted'],
            'running_jobs': states['running'], 'awaiting_scheduler_confirmation_jobs': states['done'],
            'running_phases': dict(phases),
            'working_jobs': phases['cpu_preparation'] + phases['cpu_scoring'] + phases['cpu_output'],
            'output_jobs': phases['cpu_output'],
            'idle_cpu_workers': phases['cpu_idle'],
            'waiting_prediction_jobs': phases['waiting_gpu'] + phases['waiting_cache'],
            'waiting_cpu_jobs': phases['waiting_cpu'],
            'configured_max_cpu_jobs': settings.get('max_cpu_jobs', 'auto'),
            'preparation_worker_limit': budgets.get('preparation', 0),
            'scoring_worker_limit': budgets.get('scoring', 0),
            'shared_cpu_worker_limit': budgets.get('shared', 0),
            'active_cpu_job_limit': getattr(coordinator, 'max_cpu_jobs', None),
            'prepared_waiter_limit': getattr(coordinator, 'max_prepared_jobs', None),
            'resident_pipeline_limit': coordinator.max_live_pipelines,
            'new_slurm_cpu_job_resources': {key: resources.get(key) for key in ('cpus_per_task', 'mem')}
                if coordinator.name == 'slurm' else None,
            'model_cpu_workers_per_prediction_process': {
                'iceberg': model.get('num_cpu_workers', 1),
                'glacier': model.get('glacier', {}).get('num_cpu_workers', 4)}}
