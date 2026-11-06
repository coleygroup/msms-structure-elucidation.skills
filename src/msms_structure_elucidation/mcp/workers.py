"""Whole-request workers keep preparation, prediction, caches and scoring on one server."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path

from msms_structure_elucidation.config import settings, overlay, asset
from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore, read_json, write_json
from msms_structure_elucidation.prediction_cache import digest, file_fingerprint, file_lock, source_fingerprint

REMOTE_TASKS = {'elucidate_feature', 'search_pubchem_formulas', 'search_pubchem_mass',
                'retrieve_atlas', 'predict_spectra', 'score_candidates', 'infer_formulas'}


def request_contract(params: dict, config: dict) -> dict:
    """Use local fingerprints or setup-verified remote identities on a dispatch-only host."""
    from msms_structure_elucidation.mcp.tasks import _simulator_options
    model = params.get('model') or config.get('models',{}).get('simulator',{}).get('model','iceberg')
    options = _simulator_options(config,model,None)
    approved = config.get('mcp',{}).get('approved_model_identities',{}).get(model,{})
    keys = ('checkpoint',) if model=='glacier' else ('gen_checkpoint','inten_checkpoint')
    root = Path(config.get('mcp',{}).get('job_root','results/mcp_jobs')).expanduser().resolve()/'fingerprints'
    weights = {key:file_fingerprint(Path(options[key]),root) for key in keys
               if options.get(key) and Path(options[key]).is_file()}
    if len(weights)!=len(keys):
        weights = approved.get('weights',{})
    native = options.get('ms_pred_dir')
    return {'model':model,'config':config.get('_config_digest'),'weights':weights,
            'source':source_fingerprint(Path(native)/'src/ms_pred') if native and (Path(native)/'src/ms_pred').is_dir()
                     else approved.get('source'),
            'adapter':source_fingerprint(Path(__file__).parents[1]),'sparse_k':options.get('sparse_k',100),
            'stepped_aggregation':params.get('stepped_aggregation') or config.get('pipeline',{}).get(
                'stepped_aggregation','sum_then_base_peak_normalize')}


def worker_config(endpoint: dict, base: dict | None, model: str) -> dict:
    """Resolve scientific paths and bounded CPU/GPU queues in the worker's filesystem."""
    config = copy.deepcopy(base or settings(str(Path(__file__).parents[1] / 'default.yaml')))
    overlay(config, endpoint.get('feature_config', {}))
    profile = endpoint.get('simulator', {})
    profile = {**profile, **profile.get(model, {})}
    simulator = config.setdefault('models', {}).setdefault('simulator', {})
    for source, target in {'python':'python', 'ms_pred_dir':'ms_pred_src', 'checkpoint':'glacier_ckpt',
                           'gen_checkpoint':'gen_ckpt', 'inten_checkpoint':'inten_ckpt'}.items():
        if profile.get(source):
            simulator[target] = profile[source]
    for key in ('batch_size', 'num_cpu_workers', 'num_gpu_workers', 'cuda_devices', 'glacier'):
        if key in profile:
            simulator[key] = copy.deepcopy(profile[key])
    simulator['model'] = model
    simulator['prediction_cache'] = (simulator.get('prediction_cache') or
                                    str(Path(endpoint['job_root']) / 'prediction-cache'))
    for key in ('ms_pred_src','glacier_ckpt','gen_ckpt','inten_ckpt','prediction_cache'):
        if simulator.get(key):
            simulator[key] = asset(simulator[key],config)
    if simulator.get('python') and ('/' in simulator['python'] or '\\' in simulator['python']):
        simulator['python'] = asset(simulator['python'],config)
    retrieval = config.get('models',{}).get('retrieval',{})
    if retrieval.get('pubchem_formula_map'):
        retrieval['pubchem_formula_map'] = asset(retrieval['pubchem_formula_map'],config)
    config.setdefault('pipeline',{}).setdefault('stepped_aggregation','sum_then_base_peak_normalize')
    mcp = config.setdefault('mcp', {})
    overlay(mcp, endpoint.get('mcp', {}))
    mcp.update(backend=endpoint.get('backend', 'local'), job_root=endpoint['job_root'],
               feature_pipeline=True, feature_placement='coordinator', feature_worker=True,
               feature_cpu_output=True)
    mcp.setdefault('feature_cleanup_native_outputs',True)
    if mcp.get('max_concurrent_jobs','auto')=='auto' and endpoint.get('max_jobs','auto')!='auto':
        mcp['max_concurrent_jobs'] = int(endpoint['max_jobs'])
    mcp.pop('servers', None)
    mcp.pop('dispatcher_role', None)
    # The worker profile, rather than a submitter's process IDs, owns its queues.
    for key in ('_mcp_dispatcher', '_mcp_cpu_dispatcher', '_mcp_feature_driver', '_config_digest'):
        config.pop(key, None)
    config['_mcp_python'] = endpoint.get('python') or sys.executable
    config['_config_path'] = str(Path(endpoint['job_root']) / ('worker-' + model + '-config.json'))
    config['_config_digest'] = digest(config)
    Path(endpoint['job_root']).mkdir(parents=True,exist_ok=True)
    if read_json(Path(config['_config_path']),{}).get('_config_digest')!=config['_config_digest']:
        write_json(Path(config['_config_path']), config)
    return config


def model_contract(config: dict, model: str) -> dict:
    """Fingerprint the actual configured worker model without importing its ML runtime."""
    from msms_structure_elucidation.mcp.tasks import _simulator_options
    options = _simulator_options(config, model, None)
    keys = ('checkpoint',) if model == 'glacier' else ('gen_checkpoint', 'inten_checkpoint')
    missing = [key for key in keys if not options.get(key) or not Path(options[key]).is_file()]
    native = options.get('ms_pred_dir')
    if not native or not (Path(native) / 'src/ms_pred').is_dir():
        missing.append('ms_pred_dir')
    if missing:
        return {'ready':False, 'missing':missing}
    root = Path(config['mcp']['job_root']) / 'fingerprints'
    return {'ready':True, 'model':model,
            'weights':{key:file_fingerprint(Path(options[key]),root) for key in keys},
            'source':source_fingerprint(Path(native)/'src/ms_pred'),
            'adapter':source_fingerprint(Path(__file__).parents[1]),
            'sparse_k':options.get('sparse_k',100)}


def validate_contract(expected: dict, actual: dict) -> None:
    """Reject missing confirmations and any scientific or package mismatch before execution."""
    if not actual.get('ready'):
        raise ValueError('Worker model is not configured: ' + ', '.join(actual.get('missing', [])))
    for key in ('weights', 'source', 'adapter'):
        if not expected.get(key) or expected[key] != actual[key]:
            raise ValueError(f'Worker {key} differs from the approved request; check setup and exact weights')
    if expected.get('sparse_k',100) != actual['sparse_k']:
        raise ValueError('Worker sparse_k differs from the approved request')


def _external_jobs(store: JobStore) -> list[dict]:
    """Expose only this worker's bounded Kubernetes jobs to its authenticated coordinator."""
    records = []
    for marker in (store.root/'external-scheduler').glob('*'):
        job = marker.name
        launch = store.launch(job)
        records.append({'job_id':job, 'name':launch['kubernetes_job'], 'status':store.status(job),
                        'manifest':read_json(store.path(job)/'kubernetes-job.json'),
                        'archived':(store.path(job)/'kubernetes-scheduler-records.json').is_file()})
    return records


def _execution_receipt(store: JobStore, job: str) -> None:
    """Archive stable per-feature stage receipts before computing its transfer manifest."""
    target = store.path(job)/'execution.json'
    if target.exists():
        return
    children = set(store.children(job))
    if store.status(job).get('preparation_job'):
        children.add(store.status(job)['preparation_job'])
    for child in list(children):
        children.update(store.children(child))
    stages = []
    for child in sorted(children):
        folder = store.path(child)
        row = {'job_id':child, 'task':store.request(child)['task'], 'status':store.status(child),
               'launch':store.launch(child)}
        if row['task']=='model_inference':
            params = store.request(child)['params']
            row['scientific_identity'] = {key:params.get(key) for key in ('weights','native_source','adapter_source')}
            row['conditions'] = {key:params['arguments'].get(key) for key in ('model','adduct','instrument','energies')}
            row['structures'] = params.get('structures',len(params['arguments'].get('smiles',[])))
            if params.get('expected_spectra') is not None:
                row['expected_spectra'] = params['expected_spectra']
                row['batch_plan'] = {'path':params['batch_plan'],
                    'sha256':file_fingerprint(Path(params['batch_plan']),store.root/'fingerprints')}
        for name in ('gpu-metrics.json','cache-publication.json','output-stage.json'):
            if (folder/name).is_file():
                row[name] = read_json(folder/name)
        if row['task']=='publish_prediction_group' and (folder/'result.json').is_file():
            row['publication'] = store.result(child)
        if row['task']=='build_prediction_group' and (folder/'result.json').is_file():
            row['batch_build'] = store.result(child)
        stages.append(row)
    write_json(target, {'feature':job,'host':__import__('socket').gethostname(), 'stages':stages,
                        'native_data_location':'worker filesystem and prediction cache', 'archived':time.time()})


def result_inventory(store: JobStore, job: str) -> dict:
    """Describe final feature artifacts, excluding shared HDF5 files and prediction caches."""
    folder = store.path(job)
    manifest = folder/'worker-collection.json'
    with file_lock(folder/'worker-collection.lock'):
        if manifest.exists():
            return read_json(manifest)
        _execution_receipt(store,job)
        files = {}
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(folder).as_posix()
            final_names = ('result.json','retrieval.json','spectrum.ms','execution.json','log.txt',
                           'viewer_structures.json','review_notes.json','evidence_review.json')
            if any(part in ('model_shards','preparation','fingerprints','native_outputs') for part in path.relative_to(folder).parts):
                continue
            candidate_manifest = relative.startswith('retrieval/pubchem_candidates/') and path.suffix=='.json' and not path.name.endswith('.input.json')
            if not (relative in final_names or relative in ('retrieval/'+name for name in final_names) or candidate_manifest or
                    path.suffix.lower() in ('.svg','.png','.html','.js','.css','.woff','.woff2')):
                continue
            if path.suffix.lower() in ('.hdf5','.h5','.sqlite3','.ckpt') or 'native_outputs' in path.parts:
                continue
            if path.is_symlink():
                raise ValueError('Worker result must not contain symlinks')
            if path.is_file():
                hasher = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda:stream.read(4*1024*1024),b''):
                        hasher.update(block)
                files[relative] = {'size':path.stat().st_size,'sha256':hasher.hexdigest()}
        if 'result.json' not in files:
            raise RuntimeError('Completed worker request has no result')
        write_json(manifest,files)
        return files


def handle(operation: str, data: dict, endpoint: dict, base: dict | None = None):
    """Run the authenticated worker protocol; all model data remain on this worker."""
    from msms_structure_elucidation.mcp.backends import make_backend
    from msms_structure_elucidation.mcp.features import FEATURE_TASKS
    from msms_structure_elucidation.mcp import tasks
    model = data.get('model','glacier')
    config = worker_config(endpoint,base,model)
    if operation=='worker-submit':
        aggregation = data['contract'].get('stepped_aggregation','sum_then_base_peak_normalize')
        if aggregation not in ('sum_then_base_peak_normalize','arithmetic_mean'):
            raise ValueError('Unsupported stepped collision aggregation')
        config.setdefault('pipeline',{})['stepped_aggregation'] = aggregation
        config.pop('_config_digest',None)
        config['_config_digest'] = digest(config)
    store = JobStore(Path(endpoint['job_root']))
    backend = make_backend(config['mcp']['backend'],store,config,config['_mcp_python'])
    if operation=='worker-probe':
        from msms_structure_elucidation import hostprobe
        from msms_structure_elucidation.mcp.cpu_pool import limits, output_limit
        # Probe the core resource scheduler; the detached owner drives scientific stages.
        description = backend.core.describe()
        rows = store.active()
        budgets = limits(config['mcp'])
        capacity = backend.core.capacity() if hasattr(backend.core, 'capacity') else getattr(backend.core, 'max_jobs', 1)
        features = [row for row in rows if row['task'] in REMOTE_TASKS]
        limit = endpoint.get('feature_slots')
        if limit is None:
            slots = description.get('max_concurrent_jobs',endpoint.get('max_jobs',1))
            slots = 1 if slots=='auto' else int(slots)
            limit = max(2,2*sum(budgets.values()),
                        2*slots)
        if int(limit)<1:
            raise ValueError('feature_slots must be positive')
        from msms_structure_elucidation.mcp.admission import request_backlog
        postprediction = int(endpoint.get('postprediction_slots',2*int(limit)))
        if postprediction<1:
            raise ValueError('postprediction_slots must be positive')
        return {**description,'capabilities':['whole-feature'],'contract':model_contract(config,model),
                'preparation_workers':budgets.get('preparation',0),'scoring_workers':budgets.get('scoring',0),
                'shared_cpu_workers':budgets.get('shared',0),'cpu_pools':budgets,
                'output_workers':output_limit(config['mcp'],capacity),
                'cpu':hostprobe.probe(),'feature_slots':int(limit),'active_features':len(features),
                'stage_backlog':request_backlog(features),'postprediction_slots':postprediction,
                'phases':dict(Counter(row.get('phase','queued') for row in features)),
                'scheduler_jobs':_external_jobs(store)}
    if operation=='worker-submit':
        task,params,inputs = data['task'],copy.deepcopy(data['params']),data.get('inputs',{})
        if task not in REMOTE_TASKS or set(inputs)-{'spectrum.ms'}:
            raise ValueError('Unsupported whole-worker request')
        validate_contract(data['contract'],model_contract(config,model))
        token = {key:data.get(key) for key in ('coordinator','job_id','attempt')}
        expected = store.job_id(task,{**token,'_inputs':inputs})
        with file_lock(store.root/(expected+'.submission.lock')):
            cleaned = read_json(store.root/'worker-cleaned'/(expected+'.json'))
            if cleaned:
                raise RuntimeError('Result was already acknowledged; recover its coordinator archive')
            if (store.root/expected/'worker-ready.json').is_file():
                job = expected
            else:
                job,_ = store.create(task,token,inputs,tasks.KINDS[task],config,initial_state='staging')
                request = store.request(job)
                params.pop('spectrum_path',None)
                write_json(store.path(job)/'request.json',{**request,'params':params,'worker_origin':token})
                write_json(store.path(job)/'worker-ready.json',{'origin':token,'contract':data['contract']})
                store.set_status(job,'queued')
                backend.submit(job)
        return {'job_id':job,'status':backend.poll(job),'scheduler_jobs':_external_jobs(store)}
    if operation=='worker-scheduler-update':
        for update in data['updates']:
            job = update['job_id']
            marker = store.root/'external-scheduler'/job
            if not marker.is_file() or store.launch(job).get('kubernetes_job')!=update['name']:
                raise ValueError('Scheduler update does not identify a worker-owned Kubernetes Job')
            if update.get('records'):
                write_json(store.path(job)/'kubernetes-scheduler-records.json',update['records'])
            if update.get('deleted'):
                marker.unlink()
                continue
            current = store.status(job)
            fields = {'scheduler_state':update.get('scheduler_state')}
            state = current['state']
            if update.get('terminal'):
                success = update.get('success') and state=='done' and (store.path(job)/'result.json').is_file()
                state = 'cancelled' if current.get('cancel_requested') or state=='cancelled' else ('done' if success else 'failed')
                fields.update(scheduler_confirmed=True,error=None if success else update.get('error','Kubernetes completion did not verify runner result'))
            store.set_status(job,state,**fields)
        return {'updated':True}
    job = data['remote_job_id']
    if Path(job).name!=job:
        raise ValueError('Invalid worker request ID')
    cleaned = store.root/'worker-cleaned'/(job+'.json')
    if operation=='worker-cleanup' and cleaned.exists():
        if read_json(cleaned)!=data['acknowledged_inventory']:
            raise ValueError('Worker cleanup acknowledgement changed')
        return {'cleaned':True}
    if operation=='worker-poll':
        status = backend.poll(job)
        return {'status':status,'inventory':result_inventory(store,job) if status['state']=='done' else None,
                'result_root':str(store.path(job)),
                'log_tail':store.log_tail(job) if status['state']=='failed' else None,
                'scheduler_jobs':_external_jobs(store)}
    if operation=='worker-cancel':
        return {'status':backend.cancel(job),'scheduler_jobs':_external_jobs(store)}
    if operation=='worker-bundle':
        inventory = read_json(store.path(job)/'worker-collection.json')
        if not inventory:
            raise ValueError('Worker result has not been declared complete')
        with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
            for name in inventory:
                archive.add(store.path(job)/name,arcname=name,recursive=False)
        return None
    if operation=='worker-cleanup':
        inventory = read_json(store.path(job)/'worker-collection.json')
        if inventory!=data['acknowledged_inventory']:
            raise ValueError('Worker cleanup requires the verified result manifest')
        cleaned.parent.mkdir(exist_ok=True)
        write_json(cleaned,inventory)
        shutil.rmtree(store.path(job))
        return {'cleaned':True}
    raise ValueError('Unknown worker operation')
