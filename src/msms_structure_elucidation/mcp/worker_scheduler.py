"""Relay worker-owned Kubernetes jobs without giving gateway pods API credentials."""
from __future__ import annotations

import json

from msms_structure_elucidation.mcp.kubernetes import kubectl


def sync(endpoint, response: dict, model: str) -> dict:
    """Submit, confirm and archive bounded CPU/GPU jobs from one worker workspace."""
    records = response.pop('scheduler_jobs',[])
    if not records:
        return response
    settings = endpoint.settings['mcp']['kubernetes']
    label = 'app.kubernetes.io/managed-by=msms-mcp'
    def run(*args, **kwargs):
        result = kubectl(settings,*args,**kwargs)
        if result.returncode:
            raise RuntimeError(f'{endpoint.name} scheduler: {result.stderr[-2000:]}')
        return result.stdout
    jobs = {item['metadata']['name']:item for item in json.loads(run('get','jobs','-l',label,'-o','json'))['items']}
    pods = json.loads(run('get','pods','-l',label,'-o','json'))['items']
    manifests = []
    updates,deletions = [],[]
    for record in records:
        name,status = record['name'],record['status']
        if name!='msms-'+record['job_id'].replace('_','-') or record['manifest']['metadata']['name']!=name:
            raise ValueError('Worker scheduler manifest has an unexpected identity')
        manifest = record['manifest']
        if manifest.get('kind')!='Job' or int(manifest.get('spec',{}).get('activeDeadlineSeconds',0))<1:
            raise ValueError('Worker scheduler requires a bounded Kubernetes Job')
        update = {'job_id':record['job_id'],'name':name}
        if record['archived'] and status.get('scheduler_confirmed'):
            if name in jobs:
                deletions.append(name)
            updates.append({**update,'deleted':True})
            continue
        cancelled = status.get('cancel_requested') or status['state']=='cancelled'
        job = jobs.get(name)
        if not job:
            if not cancelled:
                manifests.append(manifest)
            continue
        matching = [pod for pod in pods if pod.get('metadata',{}).get('labels',{}).get('job-name')==name]
        conditions = {c['type'] for c in job.get('status',{}).get('conditions',[]) if c['status']=='True'}
        if cancelled or conditions & {'Complete','Failed'}:
            logs = kubectl(settings,'logs','job/'+name,'--all-containers=true')
            if logs.returncode and not cancelled:
                raise RuntimeError('Cannot archive worker job logs before confirmation')
            success = 'Complete' in conditions and bool(matching) and all(
                pod.get('status',{}).get('phase')=='Succeeded' and pod['status'].get('containerStatuses') and all(
                    item.get('state',{}).get('terminated',{}).get('exitCode')==0
                    for item in pod['status']['containerStatuses']) for pod in matching)
            updates.append({**update,'terminal':True,'success':success,
                'scheduler_state':'Cancelled' if cancelled else ('Complete' if success else 'Failed'),
                'records':{'job':job,'pods':{'items':matching},'logs':logs.stdout or logs.stderr}})
        else:
            updates.append({**update,'scheduler_state':','.join(p.get('status',{}).get('phase','Pending')
                                                                  for p in matching) or 'Pending'})
    if manifests:
        result = kubectl(settings,'create','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':manifests}))
        if result.returncode and 'AlreadyExists' not in result.stderr:
            raise RuntimeError('Worker Kubernetes submission failed: '+result.stderr[-2000:])
    # Scheduler evidence reaches durable storage before its Kubernetes objects are removed.
    first = [update for update in updates if not update.get('deleted')]
    if first:
        endpoint.call('worker-scheduler-update',{'model':model,'updates':first})
    for name in deletions:
        run('delete','job',name,'--ignore-not-found=true','--wait=true')
    last = [update for update in updates if update.get('deleted')]
    if last:
        endpoint.call('worker-scheduler-update',{'model':model,'updates':last})
    return response
