"""Reserve preparation and batch assembly processes within a bounded CPU pool."""
from __future__ import annotations

import time
import uuid
import math
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, FINISHED, read_json, timestamp, write_json
from msms_structure_elucidation.prediction_cache import digest, file_lock

PREPARATION = 'preparation'
SCORING = 'scoring'
SHARED = 'shared'
BUILD_TASK = 'build_prediction_group'


def service_identity(config: dict) -> str:
    """Match actual CPU runtimes while each task retains its own science config."""
    profile = {key:value for key,value in config.items() if key!='mcp' and not key.startswith('_')}
    # Relative scientific paths must still resolve from the same directory.
    profile['config_directory'] = str(Path(config['_config_path']).parent) if config.get('_config_path') else None
    settings = config.get('mcp',{})
    profile['mcp'] = {key:settings.get(key) for key in
        ('backend','coordinator_backend','worker_environment','slurm','task_slurm','kubernetes')}
    return digest(profile)


def limits(settings: dict) -> dict[str, int]:
    """Return disjoint service budgets, retaining explicit legacy shared operation."""
    scoring = settings.get('feature_scoring_workers')
    if scoring is not None:
        preparation = settings.get('feature_preparation_workers')
        if preparation in (None, 'auto') or int(preparation) < 1 or int(scoring) < 1:
            raise ValueError('Explicit feature_scoring_workers requires positive preparation and scoring counts')
        return {PREPARATION: int(preparation), SCORING: int(scoring)}
    total = int(settings.get('feature_cpu_workers', 2))
    value = settings.get('feature_preparation_workers', 0)
    reserved = (total+1)//2 if value=='auto' and total>1 else (0 if value=='auto' else int(value))
    if total<1 or reserved<0 or reserved>=total:
        raise ValueError('feature_preparation_workers must be auto or between 0 and feature_cpu_workers - 1')
    return {PREPARATION:reserved,SCORING:total-reserved} if reserved else {SHARED:total}


def stage_defaults(gpu_slots: int) -> dict:
    """Starting service counts per GPU: 2 preparation, 0.5 scoring, 1 output, rounded up."""
    if gpu_slots < 1:
        raise ValueError('Prediction GPU slots must be positive')
    preparation, scoring = 2 * gpu_slots, math.ceil(gpu_slots / 2)
    return {'feature_preparation_workers': preparation, 'feature_scoring_workers': scoring,
            'feature_output_workers': gpu_slots, 'max_cpu_jobs': 'auto'}


def output_limit(settings: dict, gpu_slots: int = 1) -> int:
    """Resolve the independent CPU output budget for this worker, not the whole fleet."""
    setting = settings.get('feature_output_workers', 'auto')
    count = min(sum(limits(settings).values()), max(1, gpu_slots)) if setting == 'auto' else int(setting)
    if count < 1:
        raise ValueError('feature_output_workers must be positive or auto')
    return count


def cpu_job_limit(settings: dict, gpu_slots: int = 1) -> int:
    """Derive CPU-service admission from stage budgets, preserving an optional explicit cap."""
    setting = settings.get('max_cpu_jobs', 'auto')
    if setting in (None, 'auto'):
        if not settings.get('feature_pipeline', False):
            return 2
        output = output_limit(settings, gpu_slots) if settings.get('feature_cpu_output', True) else 0
        return sum(limits(settings).values()) + output
    count = int(setting)
    if count < 1:
        raise ValueError('max_cpu_jobs must be positive or auto')
    return count


def role(store, worker: str) -> str:
    """Read the immutable role of a CPU service; older services are shared."""
    value = store.request(worker)['params'].get('cpu_role',SHARED)
    if value not in (PREPARATION,SCORING,SHARED):
        raise ValueError('Unknown feature CPU role: '+str(value))
    return value


def validate_assignment(store, worker: str, features: list[str], scores: list[str]) -> None:
    """Prevent scoring from consuming a reserved preparation process."""
    selected = role(store,worker)
    if selected==PREPARATION and any(store.request(job)['task']!=BUILD_TASK for job in scores):
        raise ValueError('Reserved preparation workers accept only features and batch assembly')
    if selected==SCORING and (features or any(store.request(job)['task']==BUILD_TASK for job in scores)):
        raise ValueError('Scoring workers cannot accept preparation or batch assembly')


def retire(controller, worker: str) -> None:
    """Let a legacy service finish its current task and durably return its queued work."""
    folder = controller.store.path(worker)
    handoffs = controller.root/'cpu-role-handoffs'
    handoffs.mkdir(exist_ok=True)
    marker = handoffs/(worker+'.json')
    with file_lock(folder/'assignments.lock'):
        assigned = read_json(folder/'assignments.json',controller.store.request(worker)['params'])
        if not marker.exists():
            write_json(marker,{'worker':worker,'assignments':assigned,'created':time.time()})
        # An old service may already have selected one task. Recover only after
        # it exits, so that selected task can finish without duplicate execution.
        if not assigned.get('retire') or any(assigned.get(key) for key in ('features','scores','publications')):
            write_json(folder/'assignments.json',{**assigned,'features':[],'scores':[],'publications':[],'retire':True})


def recover(controller) -> None:
    """Requeue unfinished handoff tasks only after their former service has ended."""
    store = controller.store
    for path in (controller.root/'cpu-role-handoffs').glob('*.json'):
        saved = read_json(path)
        if saved.get('recovered') or controller._status(saved['worker'])['state'] not in FINISHED:
            continue
        returned_features,returned_scores,returned_publications = [],[],[]
        assigned = saved['assignments']
        for feature in assigned.get('features',[]):
            folder = store.root/feature
            if not (folder/'request.json').is_file() or (folder/'retrieval/ready.json').is_file():
                continue
            stage = read_json(folder/'feature-stage.json',{})
            if stage.get('prepare_job')==saved['worker'] and store.status(feature)['state'] in ACTIVE:
                write_json(folder/'feature-stage.json',{})
                store.set_status(feature,'queued',phase='waiting_preparation',preparation_job=None)
                returned_features.append(feature)
        for job in assigned.get('scores',[]):
            launch = store.launch(job)
            if launch.get('worker_job')==saved['worker'] and store.status(job)['state'] in ACTIVE:
                store.set_launch(job)
                store.set_status(job,'queued',worker_job=None)
                returned_scores.append(job)
        for job in assigned.get('publications',[]):
            launch = store.launch(job)
            if launch.get('worker_job')==saved['worker'] and store.status(job)['state'] in ACTIVE:
                store.set_launch(job)
                store.set_status(job,'queued',worker_job=None)
                returned_publications.append(job)
        write_json(path,{**saved,'recovered':time.time(),'returned_features':returned_features,
                         'returned_scores':returned_scores,'returned_publications':returned_publications})


def dispatch(controller, rows: list[dict], features: list[dict]) -> None:
    """Fill preparation and scoring roles independently without exceeding their budgets."""
    settings,store = controller.config.get('mcp',{}),controller.store
    budgets = limits(settings)
    total = sum(budgets.values())
    workers = [row['job_id'] for row in rows if row['task']=='prepare_feature_list' and row['state'] in ACTIVE]
    if SHARED not in budgets:
        for worker in workers:
            if role(store,worker)==SHARED:
                retire(controller,worker)
    waiting = []
    admission = getattr(getattr(controller.backend,'cpu',controller.backend),'admission',None)
    for row in sorted(features,key=lambda r:(timestamp(r.get('created')),r['job_id'])):
        feature = row['job_id']
        if row['state']=='queued' and not read_json(store.path(feature)/'feature-stage.json'):
            if admission is None or admission.admit(feature):
                waiting.append(feature)
    tasks = [row['job_id'] for row in store.active() if row['task'] in (BUILD_TASK,'score_prepared_feature')
             and row['state']=='queued' and not store.launch(row['job_id'])]
    tasks.sort(key=lambda job:store.request(job)['task']!=BUILD_TASK)
    builds = [job for job in tasks if store.request(job)['task']==BUILD_TASK]
    scores = [job for job in tasks if store.request(job)['task']!=BUILD_TASK]
    configs = {}
    identities = {}
    def config_for(job):
        if job not in configs:
            configs[job] = read_json(store.path(job)/'config.json')
        return configs[job]
    def identity(job):
        if job not in identities:
            identities[job] = service_identity(config_for(job))
        return identities[job]
    work = [(SHARED,waiting,tasks)] if SHARED in budgets else [(PREPARATION,waiting,builds),(SCORING,[],scores)]
    for selected,feature_jobs,task_jobs in work:
        if not feature_jobs and not task_jobs:
            continue
        first = task_jobs[0] if task_jobs else feature_jobs[0]
        config = config_for(first)
        selected_identity = identity(first)
        def matches(job):
            return identity(job)==selected_identity
        feature_jobs = [job for job in feature_jobs if matches(job)]
        task_jobs = [job for job in task_jobs if matches(job)]
        role_workers = [job for job in workers if role(store,job)==selected]
        available = [job for job in role_workers if matches(job) and
                     read_json(store.path(job)/'worker-progress.json',{}).get('accepting',True) and
                     not read_json(store.path(job)/'assignments.json',{}).get('retire')]
        def load(job):
            progress = read_json(store.path(job)/'worker-progress.json',{})
            assigned = read_json(store.path(job)/'assignments.json',store.request(job)['params'])
            return max(0,len(assigned.get('features',[]))-progress.get('processed_features',0)+
                       len(assigned.get('scores',[]))-progress.get('completed_cpu_tasks',progress.get('scored_features',0)))
        available.sort(key=lambda job:(load(job),job))
        slots = max(0,min(budgets[selected]-len(role_workers),total-len(workers)))
        pending = len(feature_jobs)+len(task_jobs)
        count = min(pending,len(available)+slots)
        if not count:
            controller._retire_idle([job for job in role_workers if not matches(job)])
            continue
        outstanding = sum(load(job) for job in available)
        new = min(count,slots,max(0,min(budgets[selected],outstanding+pending)-len(available)))
        available = available[:count-new]
        parts = [feature_jobs[index::count] for index in range(count)]
        task_parts = [[] for _ in range(count)]
        for index,job in enumerate(task_jobs):
            task_parts[(len(feature_jobs)+index)%count].append(job)
        for index in range(count):
            if index<len(available):
                controller._assign(available[index],parts[index],task_parts[index])
            else:
                worker,_ = store.create('prepare_feature_list',{'features':parts[index],'scores':task_parts[index],
                    'cpu_role':selected,'generation':uuid.uuid4().hex},kind='cpu',config=config)
                controller._assign(worker,parts[index],task_parts[index])
                controller.backend.submit(worker)
                workers.append(worker)
