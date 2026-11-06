"""CPU-built GPU batches of exact, deduplicated structure/energy requests."""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from msms_structure_elucidation.mcp.conditions import condition_config
from msms_structure_elucidation.mcp.jobs import ACTIVE, FINISHED, now, read_json, write_json
from msms_structure_elucidation.prediction_cache import digest, file_fingerprint, cache_busy

BUILD_TASK = 'build_prediction_group'


@lru_cache(maxsize=8)
def _plan(path: str, modified: int, size: int) -> dict:
    return read_json(Path(path))


def read_plan(path: str) -> dict:
    """Reuse immutable plans without repeatedly parsing large scheduler requests."""
    target = Path(path)
    stat = target.stat()
    return _plan(str(target),stat.st_mtime_ns,stat.st_size)


def molecules(params: dict) -> list[str]:
    """Resolve the structures whose candidate leases protect a GPU batch."""
    return list(read_plan(params['batch_plan'])['expected_energies']) if params.get('batch_plan') else params['arguments']['smiles']


def compatibility(request: dict) -> str:
    """Coalesce energies while retaining identical scientific identity and runtime."""
    args = request['inference']['arguments']
    runtime = {key:args.get(key) for key in ('model','adduct','instrument','batch_size','num_cpu_workers',
        'num_gpu_workers','num_writers','num_h5_chunks','feat_cache_size','sparse_k')}
    return digest([request['raw_cache_identity'],request['identity']['cache'],
                   request['inference']['native_source'],request['inference'].get('adapter_source'),
                   request['inference'].get('python'),runtime])


def build(params: dict) -> dict:
    """Assemble and validate native entries on CPU before a GPU is allocated."""
    plan = read_plan(params['plan'])
    expected = {smi:set(values) for smi,values in plan['expected_energies'].items()}
    selected,options = {},None
    model = plan['model']
    references = defaultdict(lambda:defaultdict(set))
    for row in plan['parts']:
        for smi in row['smiles']:
            references[row['prepared']][smi].update(row['energies'])
    for filename,wanted in references.items():
        payload = read_json(Path(filename))
        if model=='glacier':
            # Native preparation records its private TSV and scratch directory;
            # run_shard consumes entries and replaces save_dir at GPU launch.
            relevant = lambda value:{key:item for key,item in value.items() if key not in ('save_dir','dataset_labels')}
            if options is not None and relevant(options)!=relevant(payload['options']):
                raise ValueError('Prepared GLACIER options differ within a prediction batch')
            options = options or payload['options']
            for entry in payload['entries']:
                smi,energy = entry[0],int(round(float(entry[2])))
                if smi not in wanted or energy not in wanted[smi]:
                    continue
                key = smi,energy
                if key in selected and selected[key][2:]!=entry[2:]:
                    raise ValueError('Prepared entries disagree for the same structure and collision energy')
                selected[key] = list(entry)
        else:
            for row in payload['rows']:
                smi = row['smiles']
                if smi in wanted:
                    energies = wanted[smi]
                    if not energies<=set(map(lambda x:int(round(float(x))),row['collision_energies'])):
                        raise ValueError('ICEBERG prepared row lacks a requested energy')
                    selected[smi] = {**row,'collision_energies':[str(e) for e in sorted(expected[smi])]}
    labels = {smi:index for index,smi in enumerate(sorted(expected))}
    if model=='glacier':
        pairs = {(smi,energy) for smi,values in expected.items() for energy in values}
        if set(selected)!=pairs:
            raise ValueError('CPU batch entries do not exactly cover requested structure/energy pairs')
        entries = []
        for key,entry in sorted(selected.items()):
            entry[1] = 'pred_candidate_'+str(labels[key[0]])
            entries.append(entry)
        prepared = {'entries':entries,'options':options}
    else:
        if set(selected)!=set(expected):
            raise ValueError('CPU batch rows do not cover requested structures')
        prepared = {'model':'iceberg','rows':[
            {**selected[smi],'spec':'pred_candidate_'+str(labels[smi])} for smi in sorted(expected)]}
    target = Path(params['prepared'])
    write_json(target,prepared)
    count = sum(map(len,expected.values()))
    if count!=plan['spectra']:
        raise ValueError('CPU batch spectrum count differs from its plan')
    return {'prepared':str(target),'prepared_sha256':file_fingerprint(target,target.parent/'fingerprints'),
            'spectra':count,'structures':len(expected),'conditions':len({r['condition'] for r in plan['parts']}),
            'batch_reason':plan['reason']}


def advance(controller, job: str, params: dict) -> str:
    """Admit a staged GPU job only after its independent CPU builder succeeds."""
    store = controller.store
    stage = read_json(store.path(job)/'batch-build.json')
    builder = stage['job_id']
    status = controller._status(builder)
    worker = store.launch(builder).get('worker_job')
    if worker and status['state'] in ACTIVE and controller._status(worker)['state'] in FINISHED:
        if builder in (store.result(worker) or {}).get('held_scores',[]):
            store.set_launch(builder)
            store.set_status(builder,'queued')
            return 'preparing'
        store.set_status(builder,'failed',finished=now(),error='CPU batch service ended before assembly completed')
        status = store.status(builder)
    if status['state'] in ('failed','cancelled'):
        store.set_status(job,'failed',finished=now(),error='CPU batch assembly: '+str(status.get('error',status['state'])))
        controller._release(job)
        return 'failed'
    if status['state']!='done':
        return 'preparing'
    receipt = store.result(builder)
    if (receipt['spectra']!=params['expected_spectra'] or
            file_fingerprint(Path(params['prepared']),controller.root/'fingerprints')!=receipt['prepared_sha256']):
        raise ValueError('Prepared GPU batch changed after CPU assembly')
    store.set_status(job,'queued',phase='gpu_queue',prepared_spectra=receipt['spectra'])
    controller.backend.submit(job)
    return store.status(job)['state']


def plan(controller, db: sqlite3.Connection, limit: int, budget: int) -> None:
    """Collect compatible ready work into large batches, with prompt small remainders."""
    from msms_structure_elucidation.mcp.raw_outputs import missing_groups
    settings = controller.config.get('mcp',{})
    target = int(settings['prediction_group_spectra'])
    minimum = int(settings.get('prediction_group_min_spectra',100001))
    if min(target,minimum)<1 or target<minimum:
        raise ValueError('Prediction spectrum target must be at least its positive minimum')
    maximum = int(settings.get('max_group_candidates',131072))
    if maximum<1:
        raise ValueError('max_group_candidates must be positive')
    occupied = defaultdict(set)
    for (job,) in db.execute("SELECT job FROM groups WHERE state NOT IN ('done','failed','cancelled')"):
        params = controller.store.request(job)['params']
        if params.get('raw_cache_identity'):
            occupied[digest([params['raw_cache_root'],params['raw_cache_identity']])].update(molecules(params))
    buckets = {}
    rows = db.execute('SELECT id,payload,config FROM conditions WHERE EXISTS '
        '(SELECT 1 FROM items WHERE condition=conditions.id AND job IS NULL) ORDER BY rowid').fetchall()
    for key,payload,config_path in rows:
        request = json.loads(payload)
        try:
            config = condition_config(controller.store,controller.root,db,key,config_path)
        except FileNotFoundError:
            continue
        selected = db.execute('SELECT smiles,prepared FROM items WHERE condition=? AND job IS NULL '
                             'ORDER BY smiles LIMIT ?',(key,maximum)).fetchall()
        held = occupied[digest([request['identity']['cache'],request['raw_cache_identity']])]
        selected = [(smi,path) for smi,path in selected if smi not in held]
        if not selected:
            continue
        try:
            missing = missing_groups(Path(request['identity']['cache']),request['raw_cache_identity'],
                                     [smi for smi,_ in selected],request['conditions']['collision_energies'],timeout=0)
        except sqlite3.OperationalError as exc:
            if not cache_busy(exc):
                raise
            continue
        paths = dict(selected)
        for energies,smiles in missing.items():
            if not energies:
                job,_ = controller.store.create('cached_predictions',{'condition':key,'smiles':smiles},
                    kind='cpu',config=config,initial_state='done')
                write_json(controller.store.path(job)/'result.json',{'cache_namespace':digest(request['raw_cache_identity']),
                    'structures':len(smiles),'new_predictions':0})
                db.executemany('UPDATE items SET job=? WHERE condition=? AND smiles=?',((job,key,smi) for smi in smiles))
                db.execute('INSERT OR IGNORE INTO groups VALUES (?,?)',(job,'done'))
                continue
            bucket = buckets.setdefault(compatibility(request),{'request':request,'config':config,'parts':[]})
            by_path = defaultdict(list)
            for smi in smiles:
                by_path[paths[smi]].append(smi)
            bucket['parts'].extend({'condition':key,'prepared':prepared,'energies':list(energies),
                                   'smiles':values} for prepared,values in by_path.items())
    db.commit()
    live = db.execute("SELECT COUNT(*) FROM groups WHERE state NOT IN ('done','failed','cancelled')").fetchone()[0]
    preparing = any(controller.store.launch(row['job_id']).get('backend')=='feature-pipeline' and
                    not (controller.store.path(row['job_id'])/'retrieval/ready.json').is_file()
                    for row in controller.store.active())
    holds = controller.root/'batch-coalescing.json'
    first_seen = read_json(holds,{})
    built = 0
    for identity,bucket in buckets.items():
        parts = bucket['parts']
        while parts and live<limit and built<budget:
            expected,chosen,remainder = {},[],[]
            count = 0
            for part in parts:
                accepted,deferred = [],[]
                for smi in part['smiles']:
                    if count>=target or (smi not in expected and len(expected)>=maximum):
                        deferred.append(smi)
                        continue
                    values = expected.setdefault(smi,set())
                    count += len(set(part['energies'])-values)
                    values.update(part['energies']);accepted.append(smi)
                if accepted:chosen.append({**part,'smiles':accepted})
                if deferred:remainder.append({**part,'smiles':deferred})
            if not chosen:
                break
            now_value = time.time()
            hold = first_seen.setdefault(identity,{'changed':now_value,'count':count,'first':now_value})
            if hold['count']!=count:
                hold.update(changed=now_value,count=count)
            small = count<=int(settings.get('prediction_small_job_spectra',10000)) and not preparing
            expired = (now_value-hold['changed']>=float(settings.get('prediction_coalesce_seconds',60)) or
                       now_value-hold['first']>=float(settings.get('prediction_coalesce_max_seconds',300)))
            # No outstanding preparation means this is the actual remaining workload.
            remainder_ready = not preparing
            if count<minimum and not small and not remainder_ready and not expired:
                write_json(controller.root/'batch-wait.json',{'spectra':count,'target':target,
                    'minimum':minimum,'reason':'collecting_compatible_work','updated':now_value})
                break
            reason = 'bulk' if count>=minimum else ('small_workload' if small else 'compatible_remainder')
            expected = {smi:sorted(values) for smi,values in sorted(expected.items())}
            plan_value = {'model':bucket['request']['conditions']['model'],'expected_energies':expected,
                          'spectra':count,'parts':chosen,'reason':reason}
            token = digest(plan_value)
            plan_path = controller.root/('batch-plan-'+token+'.json')
            if not plan_path.exists():write_json(plan_path,plan_value)
            prepared = controller.root/('batch-prepared-'+token+'.json')
            params = json.loads(json.dumps(bucket['request']['inference']))
            params.update(batch_plan=str(plan_path),expected_spectra=count,structures=len(expected),
                          prepared=str(prepared),raw_cache_root=bucket['request']['identity']['cache'],
                          raw_cache_identity=bucket['request']['raw_cache_identity'],defer_raw_publication=True)
            params['arguments'].update(smiles=[],energies=sorted({e for values in expected.values() for e in values}))
            job = controller.store.job_id('model_inference',{**params,'_inputs':{}})
            if not controller._reserve(job,params):
                break
            job,_ = controller.store.create('model_inference',params,kind='gpu',config=bucket['config'],initial_state='staging')
            builder,_ = controller.store.create(BUILD_TASK,{'plan':str(plan_path),'prepared':str(prepared),'build_for':job},
                kind='cpu',config=bucket['config'])
            controller.store._index_parent(builder,job)
            write_json(controller.store.path(job)/'batch-build.json',{'job_id':builder})
            db.execute('INSERT OR IGNORE INTO groups VALUES (?,?)',(job,'preparing'))
            for part in chosen:
                db.executemany('UPDATE items SET job=? WHERE condition=? AND smiles=?',
                               ((job,part['condition'],smi) for smi in part['smiles']))
            db.commit();live+=1;built+=1;parts=remainder
            # Candidate leases cover all energies; leave remaining energies for
            # these structures unassigned until this batch has published them.
            for part in parts:
                part['smiles'] = [smi for smi in part['smiles'] if smi not in expected]
            parts = [part for part in parts if part['smiles']]
            first_seen[identity] = {'changed':now_value,'count':0,'first':now_value}
    write_json(holds,{key:value for key,value in first_seen.items() if key in buckets})
