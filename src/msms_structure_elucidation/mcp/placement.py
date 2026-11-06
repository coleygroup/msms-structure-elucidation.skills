"""Place whole requests on scientific workers; collect final results without central preparation."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import shutil
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from msms_structure_elucidation.mcp.features import FeatureBackend, FEATURE_TASKS
from msms_structure_elucidation.mcp.fleet import IncompatibleEndpoint, collect
from msms_structure_elucidation.mcp.jobs import ACTIVE, read_json, write_json, now, timestamp
from msms_structure_elucidation.mcp.network import Pending
from msms_structure_elucidation.mcp.workers import REMOTE_TASKS, request_contract, validate_contract
from msms_structure_elucidation.prediction_cache import digest
from msms_structure_elucidation.spectrum import inspect_ms


class InvalidInput(ValueError):
    """An input changed after admission and requires a new request."""


def _relocate(value, origin: str, destination: Path):
    """Relocate result-owned file references while retaining remote model/cache provenance."""
    if isinstance(value,dict):
        return {key:_relocate(item,origin,destination) for key,item in value.items()}
    if isinstance(value,list):
        return [_relocate(item,origin,destination) for item in value]
    if value==origin:
        return str(destination)
    if isinstance(value,str) and value.startswith(origin.rstrip('/')+'/'):
        return str(destination/value[len(origin.rstrip('/'))+1:])
    return value


class WorkerFeatureBackend(FeatureBackend):
    """Keep the coordinator in charge of placement and legacy draining, not scientific stages."""
    feature_launch_name = 'feature-worker'

    def __init__(self, core, store, config, python):
        super().__init__(core,store,config,python)
        core.cpu.exclude_tasks.update(REMOTE_TASKS)
        self.placements = Placement(self) if self.owner else None

    def _virtual(self, job_id: str) -> bool:
        return self.store.launch(job_id).get('backend') in ('feature-pipeline','feature-worker')

    def submit(self, job_id: str) -> None:
        if self.store.request(job_id)['task'] in REMOTE_TASKS and not self.store.launch(job_id):
            self.store.set_launch(job_id,backend=self.feature_launch_name)
        super().submit(job_id)

    def pump(self) -> None:
        if not self.owner:
            super().pump()
            return
        for row in self.store.active():
            if row.get('cancel_requested') and self.store.launch(row['job_id']).get('backend')!='feature-worker':
                self.cancel(row['job_id'])
        self.core.pump()
        self.placements.pump()
        self.controller.pump()  # Existing prepared work retains its cache and ownership.

    def cancel(self, job_id: str) -> dict:
        if self.store.launch(job_id).get('backend')!='feature-worker':
            return super().cancel(job_id)
        status = self.store.status(job_id)
        if status['state'] in ACTIVE:
            self.store.set_status(job_id,status['state'],cancel_requested=True)
            if self.placements:
                self.placements.pump()
            else:
                self.pump()
        return self.store.status(job_id)

    def describe(self) -> dict:
        description = super().describe()
        return {**description,'feature_placement':'workers',
                'coordinator_role':'Placement, monitoring and final artifact collection; existing jobs drain in place',
                'worker_execution':self.placements.describe() if self.placements else description.get('worker_execution',{})}


class Placement:
    """Persist affinity and assignments before asynchronous worker operations."""

    def __init__(self, backend: WorkerFeatureBackend):
        self.backend,self.core,self.store,self.config = backend,backend.core,backend.store,backend.config
        self.database = self.store.root/'feature-placements.sqlite3'
        self.snapshots = {}
        self.poll_at = {}
        self.contracts = {}
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS affinity (key TEXT PRIMARY KEY, server TEXT);'
                'CREATE TABLE IF NOT EXISTS mass_affinity (context TEXT, mass REAL, ppm REAL, server TEXT);'
                'CREATE INDEX IF NOT EXISTS affinity_mass ON mass_affinity(context,mass);')
            db.commit()
        seed = self.config.get('mcp',{}).get('feature_affinity_file')
        if seed:
            with closing(sqlite3.connect(self.database)) as db:
                for row in read_json(Path(seed),[]):
                    db.execute('INSERT OR IGNORE INTO affinity VALUES (?,?)',(row['key'],row['server']))
                db.commit()

    def describe(self) -> dict:
        return {name:{k:v for k,v in snapshot.items() if k!='scheduler_jobs'}
                for (name,_),(observed,snapshot) in self.snapshots.items()}

    def _endpoint(self, name: str):
        if name not in self.core.endpoints:
            raise RuntimeError(f'Assigned worker {name} is no longer configured; restore it to recover its owned jobs')
        return self.core.endpoints[name]

    def _call(self, endpoint, operation: str, payload: dict, job: str):
        return self.core._call(endpoint,operation,payload,job)

    def _spectrum(self, job: str) -> Path:
        """Freeze a queued path input before placement or transfer."""
        target = self.store.path(job)/'spectrum.ms'
        params = self.store.request(job)['params']
        if not target.exists() and params.get('spectrum_path'):
            data = Path(params['spectrum_path']).read_bytes()
            if params.get('spectrum_sha256') and hashlib.sha256(data).hexdigest()!=params['spectrum_sha256']:
                raise InvalidInput('Spectrum changed after submission; submit the updated input as a new request')
            temporary = target.with_suffix('.transfer.tmp')
            temporary.write_text(data.decode('utf-8'))
            temporary.replace(target)
        return target

    def _probe(self, name: str, model: str) -> dict:
        key = (name,model)
        previous = self.snapshots.get(key)
        if previous and time.monotonic()-previous[0]<float(self.config.get('mcp',{}).get('poll_seconds',15)):
            return previous[1]
        result = self._call(self.core.endpoints[name],'worker-probe',{'model':model},model)
        self.snapshots[key] = time.monotonic(),result
        return result

    def _affinity(self, job: str, contract: dict) -> tuple[list[str], tuple | None]:
        request = self.store.request(job)
        params = request['params']
        spectrum = self._spectrum(job)
        data = inspect_ms(spectrum,params.get('collision_unit','eV')) if spectrum.is_file() else {}
        from msms_structure_elucidation.cli import _instrument
        context = digest({'model':contract.get('model'),'weights':contract.get('weights'),
                          'source':contract.get('source'),'adduct':data.get('adduct',params.get('adduct')),
                          'instrument':_instrument(params.get('instrument') or data.get('metadata',{}).get('instrumentation'))})
        formulas = params.get('formulas') or [params.get('formula') or data.get('metadata',{}).get('formula')]
        if request['task']=='search_pubchem_mass':
            formulas = []
        keys = [digest([context,formula]) for formula in sorted(set(f for f in formulas if f))]
        mass = (context,float(data['parentmass']),max(1.,float(params.get('ppm') or 10))) if data.get('parentmass') else None
        return keys,mass

    def _place(self, job: str, rows: list[dict], assigned: Counter | None = None,
               stages: dict | None = None) -> None:
        from msms_structure_elucidation.mcp.admission import request_stage
        from subprocess import SubprocessError
        request = self.store.request(job)
        params = request['params']
        model = params.get('model') or self.config.get('models',{}).get('simulator',{}).get('model','iceberg')
        if not params.get('_model_identity') and model not in self.contracts:
            self.contracts[model] = request_contract(params,self.config)
        contract = params.get('_model_identity') or self.contracts[model]
        model = params.get('model') or contract.get('model','glacier')
        contract = {**contract,'model':model,'stepped_aggregation':params.get('stepped_aggregation') or
                    contract.get('stepped_aggregation') or self.config.get('pipeline',{}).get(
                        'stepped_aggregation','sum_then_base_peak_normalize')}
        keys,mass = self._affinity(job,contract)
        with closing(sqlite3.connect(self.database)) as db:
            owners = [row[0] for key in keys for row in db.execute('SELECT server FROM affinity WHERE key=?',(key,))]
            if not owners and mass:
                context,value,ppm = mass
                window = value*ppm*2e-6
                owners = [row[0] for row in db.execute('SELECT server FROM mass_affinity WHERE context=? AND mass BETWEEN ? AND ? '
                    'AND ABS(mass-?)/mass*1e6 <= ppm+?',(context,value-window,value+window,value,ppm))]
        preferred = Counter(owners).most_common(1)[0][0] if owners else None
        if assigned is None:
            assigned = Counter(self.store.launch(row['job_id']).get('server') for row in rows)
        if stages is None:
            stages = {stage:Counter(self.store.launch(row['job_id']).get('server') for row in rows
                      if request_stage(row)==stage) for stage in ('execution','postprediction')}
        choices,errors = [],{}
        for name,endpoint in self.core.endpoints.items():
            if not endpoint.settings.get('features') or model not in endpoint.settings.get('models',[]):
                continue
            if preferred and name!=preferred:
                continue
            try:
                probe = self._probe(name,model)
                if 'whole-feature' not in probe.get('capabilities',[]):
                    raise IncompatibleEndpoint('Worker does not support whole-feature execution')
                validate_contract(contract,probe['contract'])
                if 'stage_backlog' in probe:
                    backlog = probe['stage_backlog']
                    count = max(stages['execution'][name],backlog['execution_pending'])
                    postprediction = max(stages['postprediction'][name],backlog['postprediction_pending'])
                    if postprediction>=probe['postprediction_slots']:
                        continue
                else:
                    count = max(assigned[name],probe.get('active_features',0))
                if count>=probe['feature_slots']:
                    continue
                cpu = probe.get('cpu',{}) if endpoint.settings.get('backend','local')=='local' else {}
                available = cpu.get('ram_available_gb')
                floor = endpoint.settings.get('mcp',{}).get('min_free_memory_gb',2)
                if available is not None and available<floor:
                    continue
                idle,threads = cpu.get('estimated_idle_cpu_threads'),cpu.get('cpu_threads')
                pressure = 1-min(1.,idle/threads) if idle is not None and threads else 0.
                choices.append(((0 if probe.get('free_gpus') and (idle is None or idle>=1) else 1,
                                 count/max(1,probe['feature_slots'])+pressure,
                                 -float(endpoint.settings.get('priority',0))),name))
            except Pending:
                continue
            except (OSError,RuntimeError,ValueError,SubprocessError) as exc:
                errors[name] = str(exc)[-2000:]
        if not choices:
            self.store.set_status(job,'queued',phase='waiting_worker',dispatch_errors=errors or
                {'placement':'Waiting for an eligible worker with capacity; configure features: true and confirm its model identity'})
            return
        _,name = min(choices)
        with closing(sqlite3.connect(self.database)) as db:
            db.executemany('INSERT OR IGNORE INTO affinity VALUES (?,?)',((key,name) for key in keys))
            if mass:
                db.execute('INSERT INTO mass_affinity VALUES (?,?,?,?)',(*mass,name))
            db.commit()
        self.store.set_launch(job,backend='feature-worker',server=name,remote_job_id=None)
        assigned[name] += 1
        stages['execution'][name] += 1
        write_json(self.store.path(job)/'worker-contract.json',contract)
        self.store.set_status(job,'submitted',server=name,phase='worker_submission',submitted=now(),dispatch_errors=None)

    def _submit(self, job: str) -> dict:
        request,launch = self.store.request(job),self.store.launch(job)
        endpoint = self._endpoint(launch['server'])
        params = dict(request['params'])
        spectrum = self._spectrum(job)
        inputs = {'spectrum.ms':spectrum.read_text()} if spectrum.is_file() else {}
        params.pop('spectrum_path',None)
        contract = read_json(self.store.path(job)/'worker-contract.json')
        result = self._call(endpoint,'worker-submit',{'coordinator':self.core.coordinator_id,'job_id':job,
            'attempt':request['attempt'],'task':request['task'],'params':params,'inputs':inputs,
            'contract':contract,'model':contract['model']},job)
        self.store.set_launch(job,**{**launch,'remote_job_id':result['job_id']})
        return result

    def _local_worker_status(self, job: str, endpoint, remote_job: str) -> dict | None:
        """Read colocated nonterminal state; retain RPC health polling and final verification."""
        if endpoint.settings.get('transport','ssh')!='local':
            return None
        from msms_structure_elucidation.mcp.jobs import JobStore
        if not hasattr(self,'local_poll_owners'):
            self.local_poll_owners = {}
        if self.local_poll_owners.setdefault(endpoint.name,job)==job:
            return None
        worker = JobStore(Path(endpoint.settings['job_root']))
        status = worker.status(remote_job)
        if status['state'] in ('done','failed','cancelled'):
            return None
        return {'status':status}

    def _poll(self, job: str) -> None:
        launch = self.store.launch(job)
        endpoint = self._endpoint(launch['server'])
        if not launch.get('remote_job_id'):
            self._submit(job)
            return
        model = read_json(self.store.path(job)/'worker-contract.json')['model']
        completion = self.store.path(job)/'worker-completion.json'
        response = read_json(completion) or self._local_worker_status(job,endpoint,launch['remote_job_id']) or self._call(endpoint,'worker-poll',
            {'remote_job_id':launch['remote_job_id'],'model':model},job)
        status = response['status']
        if status['state']=='done':
            if not completion.exists():
                write_json(completion,response)
            self.store.set_phase(job,'result_collection',worker_status=status)
            action = lambda: self._collect(job,endpoint,launch,response,model)
            # Reap the reply even when files already exist, so finalization and
            # bounded transfer capacity finish before acknowledgement.
            if self.core.network:
                self.core.network.call(('worker-collect',endpoint.name,job),action,transfer=True)
            else:
                action()
            if endpoint.settings.get('cleanup',True):
                pending = self.store.root/'worker-cleanup-pending'
                pending.mkdir(exist_ok=True)
                (pending/job).touch()
            self.store.set_status(job,'done',finished=now(),server=endpoint.name,transport_error=None,
                                  remote_cleanup_pending=endpoint.settings.get('cleanup',True))
            self._cleanup(job)
        elif status['state'] in ('failed','cancelled'):
            (self.store.path(job)/'worker-error.log').write_text(response.get('log_tail') or '')
            self.store.set_status(job,status['state'],error=status.get('error'),finished=now(),server=endpoint.name)
        else:
            current = self.store.status(job)
            update = {'state':status['state'],'phase':status.get('phase','worker_execution'),
                      'worker_status':status,'server':endpoint.name,'transport_error':None}
            if any(current.get(key)!=value for key,value in update.items()):
                self.store.set_status(job,**update)
            self.poll_at[job] = time.monotonic()+float(self.config.get('mcp',{}).get('poll_seconds',15))

    def _collect(self, job: str, endpoint, launch: dict, response: dict, model: str) -> None:
        """Verify and finalize reports on the bounded artifact workers."""
        destination = self.store.path(job)/'worker-output'
        if not destination.exists():
            collect(endpoint,launch['remote_job_id'],response['inventory'],destination,'worker-bundle')
        if read_json(destination/'verified-inventory.json')!=response['inventory']:
            raise ValueError('Worker final artifacts changed after collection')
        origin = response.get('result_root',str(Path(endpoint.settings.get('job_root',''))/launch['remote_job_id']))
        result = _relocate(read_json(destination/'result.json'),origin,destination)
        if (destination/'spectrum.ms').is_file():
            result['input'] = str(destination/'spectrum.ms')
        for key,name in [('retrieval_path','retrieval.json'),('report_path','report.html')]:
            if (destination/'retrieval'/name).is_file():
                result[key] = str(destination/'retrieval'/name)
        result['worker_execution'] = {'server':endpoint.name,'job_id':launch['remote_job_id'],
                                       'receipt':str(destination/'execution.json')}
        cache = destination/'retrieval/viewer_structures.json'
        if not cache.exists():
            cache = destination/'viewer_structures.json'
        if cache.exists():
            result['viewer_structures_path'] = str(cache)
        if 'candidates' in result and 'spectra' in result and 'input' in result:
            from msms_structure_elucidation.report import write_report
            output = self.store.path(job)/'retrieval'
            output.mkdir(exist_ok=True)
            result['retrieval_path'] = str(output/'retrieval.json')
            result['report_path'] = str(output/'report.html')
            if cache.exists():
                shutil.copy2(cache,output/'viewer_structures.json')
                cache = output/'viewer_structures.json'
                result['viewer_structures_path'] = str(cache)
            write_json(Path(result['retrieval_path']),result)
            write_report(result,Path(result['report_path']))
            if cache.exists():
                from msms_structure_elucidation.visualize import export_static
                page = output/'review_report.html'
                export_static([Path(result['retrieval_path'])],page,top_k=len(result['candidates']))
                result['report_path'] = str(page)
                write_json(Path(result['retrieval_path']),result)
        write_json(self.store.path(job)/'result.json',result)
        write_json(self.store.path(job)/'worker-receipt.json',{'server':endpoint.name,'remote_job_id':launch['remote_job_id'],
            'inventory':response['inventory'],'model':model})

    def _cleanup(self, job: str) -> None:
        if not self.store.status(job).get('remote_cleanup_pending'):
            return
        receipt = read_json(self.store.path(job)/'worker-receipt.json')
        endpoint = self._endpoint(receipt['server'])
        self._call(endpoint,'worker-cleanup',{'remote_job_id':receipt['remote_job_id'],'model':receipt['model'],
                    'acknowledged_inventory':receipt['inventory']},job)
        self.store.set_status(job,'done',remote_cleanup_pending=False,remote_cleanup_error=None)

    def pump(self) -> None:
        """Interleave admissions and oldest-due polls within bounded endpoint queues."""
        from itertools import zip_longest
        from subprocess import SubprocessError
        from msms_structure_elucidation.mcp.admission import request_stage
        rows = [row for row in self.store.active() if self.store.launch(row['job_id']).get('backend')=='feature-worker']
        launches = {row['job_id']:self.store.launch(row['job_id']) for row in rows}
        self.local_poll_owners = {name:job for name,job in getattr(self,'local_poll_owners',{}).items() if job in launches}
        assigned = Counter(launch.get('server') for launch in launches.values())
        stages = {stage:Counter(launches[row['job_id']].get('server') for row in rows
                  if request_stage(row)==stage) for stage in ('execution','postprediction')}
        ordered = sorted(rows,key=lambda r:(timestamp(r.get('created')),r['job_id']))
        admissions = [row for row in ordered if launches[row['job_id']].get('server') and
                      not launches[row['job_id']].get('remote_job_id')]
        polls = sorted((row for row in ordered if launches[row['job_id']].get('remote_job_id')),
                       key=lambda row:self.poll_at.get(row['job_id'],0))
        placements = [row for row in ordered if not launches[row['job_id']].get('server')]
        for row in (row for group in zip_longest(admissions,polls,placements) for row in group if row is not None):
            job = row['job_id']
            try:
                launch = launches[job]
                if row.get('cancel_requested'):
                    if launch.get('server') and not launch.get('remote_job_id'):
                        self._submit(job)  # Recover a lost submission reply before cancelling its owner.
                        continue
                    if launch.get('remote_job_id'):
                        contract = read_json(self.store.path(job)/'worker-contract.json')
                        response = self._call(self._endpoint(launch['server']),'worker-cancel',
                            {'remote_job_id':launch['remote_job_id'],'model':contract['model']},job)
                        if response['status']['state'] not in ('cancelled','done','failed'):
                            continue
                    self.store.set_status(job,'cancelled',finished=now())
                elif not launch.get('server'):
                    self._place(job,rows,assigned,stages)
                elif not launch.get('remote_job_id') or time.monotonic()>=self.poll_at.get(job,0):
                    self._poll(job)
            except Pending:
                continue
            except InvalidInput as exc:
                self.store.set_status(job,'failed',finished=now(),error=str(exc))
            except (OSError,RuntimeError,ValueError,SubprocessError) as exc:
                self.store.set_status(job,self.store.status(job)['state'],transport_error=str(exc)[-3000:])
        pending = self.store.root/'worker-cleanup-pending'
        for marker in pending.glob('*'):
            try:
                self._cleanup(marker.name)
                marker.unlink()
            except Pending:
                continue
            except (OSError,RuntimeError,ValueError,SubprocessError) as exc:
                self.store.set_status(marker.name,'done',remote_cleanup_error=str(exc)[-2000:])
