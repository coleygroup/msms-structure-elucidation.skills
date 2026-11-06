"""Backend-owned CPU lists, durable preparation files and shared prediction groups.

The stdio client only publishes requests. A detached driver balances pending
features over bounded CPU list jobs, consumes ready manifests, groups compatible
candidate work, and schedules cache-only experimental scoring independently.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import math
from contextlib import closing
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, FINISHED, JobStore, now, read_json, write_json, timestamp
from msms_structure_elucidation.prediction_cache import digest, file_lock, cache_busy
from msms_structure_elucidation.mcp.network import NetworkQueue, Pending
from msms_structure_elucidation.mcp.conditions import snapshot_config, condition_config
from msms_structure_elucidation.mcp import batches

FEATURE_TASKS = {'elucidate_feature', 'search_pubchem_formulas', 'search_pubchem_mass', 'retrieve_atlas'}
STAGE_TASKS = {'prepare_feature_list', 'score_prepared_feature', 'publish_prediction_list', 'publish_prediction_group', batches.BUILD_TASK}


def balanced_lists(features: list[str], workers: int) -> list[list[str]]:
    """Split requests into stable lists whose lengths differ by at most one."""
    if workers < 1:
        raise ValueError('feature_cpu_workers must be positive')
    return [features[index::workers] for index in range(min(workers, len(features)))]


class FeatureBackend:
    """Keep feature requests virtual while real CPU/GPU stage jobs use the selected backend."""

    def __init__(self, core, store: JobStore, config: dict, python: str | None):
        self.core, self.store, self.config, self.python = core, store, config, python or sys.executable
        self.owner = bool(config.get('_mcp_feature_driver')) and config.get('_mcp_dispatcher') == {
            'host': socket.gethostname(), 'pid': os.getpid()}
        for backend in (core, getattr(core, 'cpu', None)):
            if backend is not None and hasattr(backend, 'exclude_tasks'):
                backend.exclude_tasks.update(FEATURE_TASKS | {'score_prepared_feature','publish_prediction_group',batches.BUILD_TASK})
        self.controller = Controller(self) if self.owner else None

    def __getattr__(self, name):
        return getattr(self.core, name)

    def _virtual(self, job_id: str) -> bool:
        return self.store.launch(job_id).get('backend') == 'feature-pipeline'

    def submit(self, job_id: str) -> None:
        request = self.store.request(job_id)
        if request['task'] in FEATURE_TASKS and not self.store.launch(job_id):
            self.store.set_launch(job_id, backend='feature-pipeline')
        if self.owner and not self._virtual(job_id):
            self.core.submit(job_id)
        self.pump()

    def pump(self) -> None:
        if self.owner:
            for row in self.store.active():
                if row.get('cancel_requested'):
                    self.cancel(row['job_id'])
            self.core.pump()
            self.controller.pump()
        elif not os.environ.get('MSMS_PIPELINE_JOB'):
            if self.store.has_active():
                ensure(self.store, self.config, self.python)

    def poll(self, job_id: str) -> dict:
        if self._virtual(job_id):
            self.store.status(job_id)  # Recover a missing cleanup index before checking owner liveness.
            self.pump()
            return self.store.status(job_id)
        if self.owner:
            return self.core.poll(job_id)
        self.pump()
        status = self.store.status(job_id)
        return {**status, 'state': 'running'} if self.store.scheduler_pending(job_id, status) else status

    def cancel(self, job_id: str) -> dict:
        if not self._virtual(job_id):
            if self.store.launch(job_id).get('backend')=='cpu-worker':
                return self.store.set_status(job_id,'cancelled',finished=now())
            if self.owner:
                return self.core.cancel(job_id)
            status = self.store.status(job_id)
            if status['state'] in ACTIVE:
                self.store.set_status(job_id, status['state'], cancel_requested=True)
                self.pump()
            return self.store.status(job_id)
        # Shared preparation and prediction groups also serve other features.
        # Cancellation removes this consumer; it never cancels another feature's work.
        status = self.store.set_status(job_id, 'cancelled', finished=now())
        for child in self.store.children(job_id):
            if self.store.request(child)['task'] == 'score_prepared_feature' and self.store.status(child)['state'] in ACTIVE:
                self.cancel(child)
        return status

    def describe(self) -> dict:
        from msms_structure_elucidation.mcp.cpu_pool import limits
        budgets = limits(self.config.get('mcp', {}))
        description = self.core.describe() if self.owner else read_json(self.store.root / 'feature-backend-info.json', {
            'backend': self.core.name, 'status': 'Backend inventory pending',
            'configured_servers': sorted(self.config.get('mcp', {}).get('servers', {}))})
        return {**description, 'feature_pipeline': {
            'enabled': True, 'driver': read_json(self.store.root / 'feature-service.json', {}),
            'cpu_workers': sum(budgets.values()),
            'preparation_workers': budgets.get('preparation', 0),
            'scoring_workers': budgets.get('scoring', 0),
            'shared_cpu_workers': budgets.get('shared', 0),
            'cpu_pools': budgets,
            'frontend': 'request and status only'}}

    def close(self) -> None:
        if self.controller:
            self.controller.close()
        if hasattr(self.core, 'close'):
            self.core.close()


class Controller:
    """Consume filesystem readiness records and persist cross-feature prediction ownership."""

    def __init__(self, backend: FeatureBackend):
        self.backend, self.store, self.config = backend.core, backend.store, backend.config
        self.root = self.store.root / 'feature-queue'
        self.root.mkdir(exist_ok=True)
        self.database = self.root / 'queue.sqlite3'
        self.leases = {}
        self.publications = NetworkQueue(1, 2) if self.config.get('mcp', {}).get('feature_async_publication', True) else None
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS features (id TEXT PRIMARY KEY, prepared TEXT, score_job TEXT);
                CREATE TABLE IF NOT EXISTS conditions (id TEXT PRIMARY KEY, payload TEXT, config TEXT);
                CREATE TABLE IF NOT EXISTS items (condition TEXT, smiles TEXT, job TEXT, prepared TEXT,
                    PRIMARY KEY(condition, smiles));
                CREATE TABLE IF NOT EXISTS consumers (feature TEXT, condition TEXT, smiles TEXT,
                    PRIMARY KEY(feature, condition, smiles));
                CREATE TABLE IF NOT EXISTS groups (job TEXT PRIMARY KEY, state TEXT);
                CREATE INDEX IF NOT EXISTS pending_items ON items(condition, job);
            ''')
            if 'prepared' not in {row[1] for row in db.execute('PRAGMA table_info(items)')}:
                db.execute('ALTER TABLE items ADD COLUMN prepared TEXT')
            db.commit()

    def connect(self) -> sqlite3.Connection:
        cache_mb = int(self.config.get('mcp', {}).get('feature_queue_cache_mb', 64))
        if cache_mb < 1:
            raise ValueError('feature_queue_cache_mb must be positive')
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute(f'PRAGMA cache_size={-cache_mb * 1024}')
        return connection

    def _status(self, job: str) -> dict:
        """Read the once-per-tick backend reconciliation; do not recursively pump the whole fleet."""
        status = self.store.status(job)
        return {**status, 'state': 'running'} if self.store.scheduler_pending(job, status) else status

    def _fail(self, feature: str, message: str) -> None:
        if self.store.status(feature)['state'] in ACTIVE:
            self.store.set_status(feature, 'failed', error=message, finished=now())
            from msms_structure_elucidation.mcp.admission import Admission
            Admission(self.store, self.config).record(feature, 'failed', message)

    def pump(self) -> None:
        """One owner progresses independent preparation, prediction and scoring stages."""
        with file_lock(self.root / 'owner-tick.lock', blocking=False) as acquired:
            if not acquired:
                return
            from msms_structure_elucidation.mcp.cpu_pool import recover
            recover(self)
            rows = self.store.active()
            features = [row for row in rows if self.store.launch(row['job_id']).get('backend') == 'feature-pipeline']
            self._reconcile_preparation(features)
            # Keep CPU capacity fed before shared-cache scans and bulk planning.
            self._prepare(rows, features)
            self._prepare_outputs(self.store.active())
            with closing(self.connect()) as db:
                ingested = 0
                limit = int(self.config.get('mcp', {}).get('feature_ingest_per_tick', 4))
                if limit < 1:
                    raise ValueError('feature_ingest_per_tick must be positive')
                for feature in features:
                    ingested += self._ingest(db, feature['job_id'])
                    if ingested >= limit:
                        break
                self._groups(db)
                # Newly planned builders can use CPU capacity immediately.
                self._prepare(self.store.active(), features)
                for index, feature in enumerate(features, 1):
                    self._score(db, feature['job_id'])
                    if index % 32 == 0:
                        # Long feature reconciliation must not hold assembled
                        # predictions behind the remaining feature checks.
                        db.commit()
                        self._advance_groups(db)
                db.commit()
            self._prepare(self.store.active(), features)
            self._prepare_outputs(self.store.active())

    def _assign(self, worker: str, features: list[str], scores: list[str], publications: list[str] | None = None) -> bool:
        """Append CPU work under the same lock a worker uses when retiring."""
        from msms_structure_elucidation.mcp.cpu_pool import validate_assignment
        validate_assignment(self.store,worker,features,scores)
        folder = self.store.path(worker)
        with file_lock(folder / 'assignments.lock'):
            progress = read_json(folder / 'worker-progress.json', {})
            if progress.get('accepting') is False or self.store.status(worker)['state'] not in ACTIVE:
                return False
            assigned = read_json(folder / 'assignments.json', self.store.request(worker)['params'])
            if assigned.get('retire'):
                return False
            assigned['features'] = list(dict.fromkeys(assigned.get('features', []) + features))
            assigned['scores'] = list(dict.fromkeys(assigned.get('scores', []) + scores))
            assigned['publications'] = list(dict.fromkeys(assigned.get('publications', []) + (publications or [])))
            write_json(folder / 'assignments.json', assigned)
            for feature in features:
                write_json(self.store.path(feature) / 'feature-stage.json', {'prepare_job':worker})
                self.store.set_status(feature,'running',phase='waiting_cpu',preparation_job=worker)
            for job in scores + (publications or []):
                self.store.set_launch(job,backend='cpu-worker',worker_job=worker)
            return True

    def _prepare_outputs(self, rows: list[dict]) -> None:
        """Start bounded CPU services when collected HDF5 is ready; reuse warm services."""
        pending = [r['job_id'] for r in rows if r['task']=='publish_prediction_group'
                   and r['state']=='queued' and not self.store.launch(r['job_id'])]
        if not pending:
            return
        runtime = lambda job: digest({key:read_json(self.store.path(job)/'config.json').get('models',{}).get(
            'simulator',{}).get(key) for key in ('python','ms_pred_src')})
        selected = runtime(pending[0])
        pending = [job for job in pending if runtime(job)==selected]
        capacity = self.backend.capacity() if hasattr(self.backend,'capacity') else getattr(self.backend,'max_jobs',1)
        from msms_structure_elucidation.mcp.cpu_pool import output_limit
        limit = output_limit(self.config.get('mcp', {}), capacity)
        all_workers = [r['job_id'] for r in rows if r['task']=='publish_prediction_list' and r['state'] in ACTIVE]
        workers = [job for job in all_workers if runtime(job)==selected]
        def load(worker):
            assigned = read_json(self.store.path(worker)/'assignments.json',self.store.request(worker)['params'])
            progress = read_json(self.store.path(worker)/'worker-progress.json',{})
            return max(0,len(assigned.get('publications',[]))-progress.get('published_groups',0))
        desired = min(limit,len(pending)+sum(load(worker) for worker in workers))
        if not workers and len(all_workers)>=limit:
            self._retire_idle(all_workers)
        for _ in range(min(max(0,desired-len(workers)),max(0,limit-len(all_workers)))):
            worker,_ = self.store.create('publish_prediction_list',{'features':[],'scores':[],'publications':[],
                'generation':uuid.uuid4().hex},kind='cpu',config=read_json(self.store.path(pending[0])/'config.json'))
            self._assign(worker,[],[])
            self.backend.submit(worker)
            workers.append(worker)
        available = [worker for worker in workers if
                     read_json(self.store.path(worker)/'worker-progress.json',{}).get('accepting',True)]
        for job in pending:
            if not available:
                break
            worker = min(available,key=load)
            if not self._assign(worker,[],[],[job]):
                available.remove(worker)

    def _retire_idle(self, workers: list[str]) -> None:
        """Release idle allocations so a different scientific runtime can use the bounded pool."""
        for job in workers:
            if self.store.status(job).get('phase')!='cpu_idle':
                continue
            with file_lock(self.store.path(job)/'assignments.lock'):
                assigned = read_json(self.store.path(job)/'assignments.json',self.store.request(job)['params'])
                assigned['retire'] = True
                write_json(self.store.path(job)/'assignments.json',assigned)

    def _prepare(self, rows: list[dict], features: list[dict]) -> None:
        from msms_structure_elucidation.mcp.cpu_pool import dispatch
        dispatch(self,rows,features)

    def _reconcile_preparation(self, rows: list[dict]) -> None:
        # A list failure affects only unfinished entries. Already published ready
        # manifests remain eligible for dispatch and completed entries are retained.
        for row in rows:
            feature = row['job_id']
            if row.get('scoring_job') or (self.store.root/feature/'retrieval/ready.json').is_file():
                continue
            stage = read_json(self.store.path(feature) / 'feature-stage.json', {})
            if not stage.get('prepare_job'):
                continue
            status = self._status(stage['prepare_job'])
            if status['state'] == 'done' and feature in (self.store.result(stage['prepare_job']) or {}).get('held_features',[]):
                write_json(self.store.path(feature)/'feature-stage.json',{})
                self.store.set_status(feature,'queued',phase='held_preparation')
                continue
            if status['state'] in ('failed', 'cancelled'):
                if not (self.store.path(feature) / 'retrieval/ready.json').is_file():
                    self._fail(feature, f'CPU preparation list {stage["prepare_job"]}: {status.get("error", status["state"])}')

    def _ingest(self, db: sqlite3.Connection, feature: str) -> int:
        if db.execute('SELECT 1 FROM features WHERE id=?', (feature,)).fetchone():
            return 0
        folder = self.store.path(feature)
        ready = read_json(folder / 'retrieval/ready.json')
        if not ready:
            return 0
        from msms_structure_elucidation.prediction_cache import file_fingerprint
        path = Path(ready['prepared'])
        if file_fingerprint(path, self.root / 'fingerprints') != ready['sha256']:
            self._fail(feature, 'Preparation manifest checksum changed')
            return 0
        prepared = read_json(path)
        db.execute('INSERT INTO features VALUES (?,?,NULL)', (feature, str(path)))
        for name in prepared['requests']:
            request = read_json(Path(name))
            key, smiles = request['condition_id'], request['smiles']
            db.execute('INSERT OR IGNORE INTO conditions VALUES (?,?,?)',
                (key, json.dumps({k:v for k,v in request.items() if k != 'smiles'}),
                 str(snapshot_config(self.root,read_json(folder/'config.json')))))
            db.executemany('INSERT OR IGNORE INTO items VALUES (?,?,NULL,?)', ((key, smi, request['native_prepared']) for smi in smiles))
            db.executemany('INSERT OR IGNORE INTO consumers VALUES (?,?,?)', ((feature, key, smi) for smi in smiles))
        self.store.set_phase(feature, 'waiting_predictions')
        return 1

    def _reserve(self, job: str, params: dict) -> bool:
        """Reserve shared-cache candidates without holding a CPU allocation or blocking the driver."""
        if job in self.leases:
            return True
        from msms_structure_elucidation.prediction_cache import candidate_guard
        folder = Path(params['raw_cache_root']) / digest(params['raw_cache_identity'])
        guard = candidate_guard(folder, batches.molecules(params), blocking=False)
        if not guard.__enter__():
            guard.__exit__(None, None, None)
            return False
        self.leases[job] = guard
        return True

    def _release(self, job: str) -> None:
        guard = self.leases.pop(job, None)
        if guard is not None:
            guard.__exit__(None, None, None)

    def _complete_group(self, job: str) -> bool:
        """Publish collected native records asynchronously while other GPUs consume their queues."""
        from msms_structure_elucidation.mcp.raw_outputs import publish
        params = self.store.request(job)['params']
        if not params.get('publish_raw_records'):
            return True  # completed compatibility pipeline already published its own cache
        if not self._reserve(job, params):
            return False
        folder = Path(self.store.result(job)['save_dir'])
        if params.get('defer_raw_publication'):
            stage_path = self.store.path(job)/'output-stage.json'
            publication = read_json(stage_path,{}).get('publication_job')
            if not publication:
                output_config = dict(self.config)
                output_config['models'] = read_json(self.store.path(job)/'config.json',self.config).get('models',{})
                publication,_ = self.store.create('publish_prediction_group',{
                    'folder':str(folder), 'raw_cache_root':params['raw_cache_root'],
                    'raw_cache_identity':params['raw_cache_identity'], 'native_source':params['native_source'],
                    'smiles':params['arguments']['smiles'], 'energies':params['arguments']['energies'],
                    **({'batch_plan':params['batch_plan'],'expected_spectra':params['expected_spectra']} if params.get('batch_plan') else {}),
                    '_parent_job':job},kind='cpu',config=output_config)
                write_json(stage_path,{'publication_job':publication})
            status = self._status(publication)
            worker = self.store.launch(publication).get('worker_job')
            if worker and status['state'] in ACTIVE and self._status(worker)['state'] in FINISHED:
                if publication in (self.store.result(worker) or {}).get('held_publications',[]):
                    self.store.set_launch(publication)
                    self.store.set_status(publication,'queued')
                    return False
                self.store.set_status(publication,'failed',error='CPU output worker ended before publication completed',finished=now())
                status = self.store.status(publication)
            if status['state'] in ('failed','cancelled'):
                raise RuntimeError('CPU output '+publication+': '+str(status.get('error',status['state'])))
            if status['state']!='done':
                return False
            write_json(self.store.path(job)/'cache-publication.json',self.store.result(publication))
            if self.config.get('mcp',{}).get('feature_worker') and self.config['mcp'].get('feature_cleanup_native_outputs',True):
                if folder.is_dir() and folder.name=='native_outputs' and folder.resolve().is_relative_to(self.store.path(job).resolve()):
                    import shutil
                    shutil.rmtree(folder)
            self._release(job)
            return True
        action = lambda: publish(folder, Path(params['raw_cache_root']), params['raw_cache_identity'],
                                 params['arguments']['smiles'], params['arguments']['energies'])
        try:
            receipt = self.publications.call(('publish', job), action, transfer=True) if self.publications else action()
        except Pending:
            return False
        write_json(self.store.path(job) / 'cache-publication.json', receipt)
        self._release(job)
        return True

    def _prepared_group(self, rows: list[tuple], model: str, energies: tuple, target: Path) -> None:
        """Crop CPU-prepared entries into a GPU group without repeating native entry preparation."""
        selected = {row[0] for row in rows}
        indices = {smi:i for i,smi in enumerate(sorted(selected))}
        entries, by_smiles, options = [], {}, None
        for path in dict.fromkeys(row[1] for row in rows):
            payload = read_json(Path(path))
            if model == 'glacier':
                options = options or payload['options']
                for entry in payload['entries']:
                    if entry[0] in selected and int(round(float(entry[2]))) in energies:
                        entry = list(entry)
                        entry[1] = 'pred_candidate_' + str(indices[entry[0]])
                        by_smiles[entry[0], float(entry[2])] = entry
            else:
                for row in payload['rows']:
                    if row['smiles'] in selected:
                        by_smiles[row['smiles']] = {**row, 'collision_energies':[str(e) for e in energies]}
        if model == 'glacier':
            entries = [entry for key,entry in sorted(by_smiles.items())]
            if len(entries) != len(selected) * len(energies) or {e[0] for e in entries} != selected:
                raise RuntimeError('Incomplete CPU-prepared GLACIER group')
            write_json(target, {'entries':entries, 'options':options})
        else:
            if set(by_smiles) != selected:
                raise RuntimeError('Incomplete CPU-prepared ICEBERG group')
            write_json(target, {'model':'iceberg','rows':sorted(by_smiles.values(),key=lambda row:row['inchikey'])})

    def _advance_groups(self, db: sqlite3.Connection) -> None:
        """Admit assembled GPU work and finish publication without planning new groups."""
        for job,state in db.execute("SELECT job,state FROM groups WHERE state NOT IN ('done','failed','cancelled')").fetchall():
            status = self._status(job)
            state = status['state']
            params = self.store.request(job)['params']
            if state in ACTIVE+('staging',) and params.get('publish_raw_records'):
                if not self._reserve(job, params):
                    continue
            if state=='staging' and params.get('batch_plan'):
                try:
                    state = batches.advance(self,job,params)
                except (OSError,ValueError,RuntimeError) as exc:
                    self.store.set_status(job,'failed',error='CPU batch assembly: '+str(exc),finished=now())
                    state = 'failed'
            if state == 'done':
                try:
                    state = 'done' if self._complete_group(job) else 'collecting'
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    write_json(self.store.path(job) / 'cache-publication-error.json', {'error':str(exc)})
                    state = 'failed'
            if state in ('failed','cancelled'):
                self._release(job)
            db.execute('UPDATE groups SET state=? WHERE job=?', (state,job))
        db.commit()

    def _groups(self, db: sqlite3.Connection) -> None:
        from msms_structure_elucidation.mcp.pipeline import group_size
        from msms_structure_elucidation.mcp.raw_outputs import missing_groups
        self._advance_groups(db)
        capacity = self.backend.capacity() if hasattr(self.backend,'capacity') else getattr(self.backend,'max_jobs',1)
        limit = self.config.get('mcp',{}).get('feature_prediction_groups','auto')
        limit = max(2,2*capacity) if limit == 'auto' else int(limit)
        if limit < 1:
            raise ValueError('feature_prediction_groups must be positive or auto')
        budget = int(self.config.get('mcp',{}).get('feature_group_builds_per_tick',2))
        if budget < 1:
            raise ValueError('feature_group_builds_per_tick must be positive')
        if self.config.get('mcp',{}).get('prediction_group_spectra') is not None:
            batches.plan(self,db,limit,budget)
            return
        built = 0
        live = db.execute("SELECT COUNT(*) FROM groups WHERE state NOT IN ('done','failed','cancelled')").fetchone()[0]
        for key,payload,config_path in db.execute('SELECT id,payload,config FROM conditions WHERE EXISTS '
                '(SELECT 1 FROM items WHERE condition=conditions.id AND job IS NULL) ORDER BY rowid').fetchall():
            request = json.loads(payload)
            try:
                config = condition_config(self.store,self.root,db,key,config_path)
            except FileNotFoundError as exc:
                for (feature,) in db.execute('SELECT DISTINCT feature FROM consumers WHERE condition=?',(key,)):
                    if self.store.status(feature)['state'] in ACTIVE:
                        self.store.set_phase(feature,'waiting_configuration',configuration_error=str(exc))
                continue
            size = group_size(config,request['conditions']['model'],len(request['conditions']['collision_energies']))
            while live < limit and built < budget:
                rows = db.execute('SELECT smiles,prepared FROM items WHERE condition=? AND job IS NULL ORDER BY smiles LIMIT ?', (key,size)).fetchall()
                if not rows:
                    break
                raw_root = Path(request['identity']['cache'])
                raw_identity = request['raw_cache_identity']
                try:
                    needed = missing_groups(raw_root,raw_identity,[r[0] for r in rows],
                                            request['conditions']['collision_energies'],timeout=0)
                except sqlite3.OperationalError as exc:
                    if not cache_busy(exc):
                        raise
                    # A busy cache is not a miss. Leave ownership unassigned and
                    # keep dispatching other conditions while its writer finishes.
                    for (feature,) in db.execute('SELECT DISTINCT feature FROM consumers WHERE condition=?',(key,)):
                        if self.store.status(feature)['state'] in ACTIVE:
                            self.store.set_phase(feature,'waiting_cache',cache_wait_error=str(exc))
                    break
                energies,molecules = next(iter(needed.items()))
                chosen = set(molecules)
                selected = [(smi,path) for smi,path in rows if smi in chosen]
                if not energies:
                    job,_ = self.store.create('cached_predictions',{'condition':key,'smiles':molecules},
                                             kind='cpu',config=config,initial_state='done')
                    write_json(self.store.path(job)/'result.json',{'cache_namespace':digest(raw_identity),'structures':len(molecules),'new_predictions':0})
                    self.store.set_status(job,'done',finished=now())
                else:
                    params = json.loads(json.dumps(request['inference']))
                    if self.config.get('mcp',{}).get('feature_cpu_output',True):
                        params['defer_raw_publication'] = True
                    params['arguments'].update(smiles=molecules, energies=list(energies))
                    params.update(raw_cache_root=str(raw_root),raw_cache_identity=raw_identity)
                    prepared = self.root / ('prepared-' + digest([key,molecules,energies]) + '.json')
                    try:
                        self._prepared_group(selected, request['conditions']['model'], energies, prepared)
                    except (OSError, ValueError, RuntimeError) as exc:
                        job,_ = self.store.create('preparation_failure',{'condition':key,'smiles':molecules,
                            'energies':list(energies),'error':str(exc)},kind='cpu',config=config,initial_state='failed')
                        self.store.set_status(job,'failed',error='CPU prediction-group preparation: '+str(exc),finished=now())
                        db.execute('INSERT OR IGNORE INTO groups VALUES (?,?)',(job,'failed'))
                        db.executemany('UPDATE items SET job=? WHERE condition=? AND smiles=?',
                                       ((job,key,smi) for smi in molecules))
                        db.commit()
                        built += 1
                        continue
                    params['prepared'] = str(prepared)
                    job = self.store.job_id('model_inference',{**params,'_inputs':{}})
                    if not self._reserve(job,params):
                        # Another request owns these candidates. Try other conditions;
                        # no GPU or CPU worker is reserved by this wait.
                        break
                    job,_ = self.store.create('model_inference',params,kind='gpu',config=config)
                db.execute('INSERT OR IGNORE INTO groups VALUES (?,?)',(job,self.store.status(job)['state']))
                db.executemany('UPDATE items SET job=? WHERE condition=? AND smiles=?',((job,key,smi) for smi in molecules))
                db.commit()
                built += 1
                if energies:
                    self.backend.submit(job)
                    live += 1
            if live >= limit or built >= budget:
                break

    def close(self) -> None:
        """Settle cache publication and release candidate leases on driver shutdown."""
        if self.publications:
            self.publications.close()
        for job in list(self.leases):
            self._release(job)

    def _score(self, db: sqlite3.Connection, feature: str) -> None:
        row = db.execute('SELECT prepared,score_job FROM features WHERE id=?', (feature,)).fetchone()
        if not row or self.store.status(feature)['state'] not in ACTIVE:
            return
        prepared, score_job = row
        if score_job:
            status = self._status(score_job)
            worker = self.store.launch(score_job).get('worker_job')
            if worker and status['state'] in ACTIVE and self._status(worker)['state'] in FINISHED:
                if score_job in (self.store.result(worker) or {}).get('held_scores',[]):
                    self.store.set_launch(score_job)
                    self.store.set_status(score_job,'queued')
                else:
                    self.store.set_status(score_job,'failed',error='CPU worker ended before scoring completed',finished=now())
                status = self.store.status(score_job)
            if status['state'] == 'done':
                write_json(self.store.path(feature) / 'result.json', self.store.result(score_job))
                self.store.set_status(feature, 'done', finished=now(), prediction_complete=True)
                from msms_structure_elucidation.mcp.admission import Admission
                Admission(self.store, self.config).record(feature, 'done')
            elif status['state'] in ('failed', 'cancelled'):
                self._fail(feature, f'Scoring {score_job}: {status.get("error", status["state"])}')
            return
        predictions = [job for (job,) in db.execute('''SELECT DISTINCT items.job FROM consumers
            JOIN items USING(condition,smiles) WHERE feature=? AND items.job IS NOT NULL''', (feature,))]
        for job in predictions:
            self.store._index_parent(job, feature)
        status = self.store.status(feature)
        if status.get('prediction_jobs') != sorted(predictions):
            self.store.set_phase(feature, status.get('phase', 'waiting_predictions'), prediction_jobs=sorted(predictions))
        outputs = sorted({stage['publication_job'] for job in predictions
                          if (stage := read_json(self.store.path(job)/'output-stage.json',{})).get('publication_job')})
        if outputs and any(self.store.status(job)['state'] in ACTIVE for job in outputs):
            self.store.set_phase(feature,'waiting_output',output_jobs=outputs)
        states = {state for (state,) in db.execute('''SELECT DISTINCT groups.state FROM consumers
            JOIN items USING(condition,smiles) LEFT JOIN groups ON items.job=groups.job WHERE feature=?''', (feature,))}
        if states & {'failed', 'cancelled'}:
            errors = []
            for job in predictions:
                errors.append(str(self.store.status(job).get('error') or ''))
                errors.append(self.store.log_tail(job))
                errors.append(str(read_json(self.store.path(job)/'cache-publication-error.json',{})))
            self._fail(feature, 'A required prediction group failed: ' + '\n'.join(errors)[-6000:])
            return
        if states - {'done'}:
            return
        config = read_json(self.store.path(feature) / 'config.json')
        score_job, _ = self.store.create('score_prepared_feature', {'prepared': prepared, '_parent_job': feature},
                                        kind='cpu', config=config)
        db.execute('UPDATE features SET score_job=? WHERE id=?', (score_job, feature))
        db.commit()
        self.store.set_phase(feature, 'waiting_cpu', scoring_job=score_job)


def _prepare_feature(store: JobStore, feature: str) -> str | None:
    """Prepare one feature within a persistent scientific worker."""
    from msms_structure_elucidation import cli
    folder = store.path(feature)
    if (folder/'retrieval/ready.json').is_file():
        return feature
    if store.status(feature)['state'] not in ACTIVE:
        return None
    store.set_phase(feature,'cpu_preparation')
    request = store.request(feature)
    params = request['params']
    config_path = folder / 'config.json'
    os.environ['MSMS_CONFIG'] = str(config_path)
    spectrum = folder / 'spectrum.ms' if (folder / 'spectrum.ms').is_file() else Path(params['spectrum_path'])
    formulas_file = None
    if params.get('formulas'):
        formulas_file = str(folder / 'formulas.txt')
        Path(formulas_file).write_text('\n'.join(params['formulas']) + '\n')
    args = argparse.Namespace(input=str(spectrum), collision_unit=params['collision_unit'],
        output_dir=str(folder / 'retrieval'), config=str(config_path), prepare_only=True,
        model=params.get('model', 'iceberg'), formula=params.get('formula'), formulas_file=formulas_file,
        mass_only=request['task'] == 'search_pubchem_mass', instrument=params.get('instrument'),
        top_k=params.get('top_k', 10), ms1_ppm=params.get('ppm'), ms2_ppm=None,
        formula_elements=params.get('elements'), no_report=True, score_no_report=False, atlas_mgf=None,
        ms_pred_python=sys.executable, no_pubchem=False, pubchem_fallback=False)
    try:
        cli.run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        store.set_status(feature, 'failed', error=str(exc)[-6000:], finished=now())
        from msms_structure_elucidation.mcp.admission import Admission
        Admission(store, read_json(config_path)).record(feature, 'failed', str(exc))
        return None
    return feature

def prepare_list(job_dir: Path, config: dict) -> dict:
    """Serve balanced preparation lists and ready scoring jobs in one bounded CPU allocation."""
    from msms_structure_elucidation.preparation import score
    from msms_structure_elucidation.mcp.raw_outputs import publish_native
    store,worker = JobStore(job_dir.parent),job_dir.name
    output_service = store.request(worker)['task']=='publish_prediction_list'
    cpu_role = store.request(worker)['params'].get('cpu_role','shared')
    idle_key = 'feature_output_idle_seconds' if output_service else 'feature_worker_idle_seconds'
    idle_seconds = float(config.get('mcp',{}).get(idle_key,config.get('mcp',{}).get('feature_worker_idle_seconds',60)))
    if not math.isfinite(idle_seconds) or idle_seconds < 0:
        raise ValueError('feature_worker_idle_seconds must be nonnegative')
    maximum = config.get('mcp',{}).get('feature_output_max_seconds' if output_service else 'feature_worker_max_seconds')
    if maximum is not None and (not math.isfinite(float(maximum)) or float(maximum)<=0):
        raise ValueError('CPU service maximum duration must be positive')
    retire_at = time.monotonic()+float(maximum) if maximum is not None else None
    done,scored,built,prepared_ids,published = set(),set(),set(),set(),set()
    if output_service:
        # Import the decoder once per CPU service and reuse it across ready groups.
        from ms_pred import common
    active_at = time.monotonic()
    local = config.get('mcp',{}).get('backend','local')=='local' or (
        config.get('mcp',{}).get('backend')=='pool' and config['mcp'].get('coordinator_backend','local')=='local')
    def progress(accepting=True):
        write_json(job_dir/'worker-progress.json',{'accepting':accepting,'heartbeat':time.time(),
            'processed_features':len(done),'prepared_features':len(prepared_ids),'scored_features':len(scored),
            'built_groups':len(built),'completed_cpu_tasks':len(scored)+len(built),
            'published_groups':len(published),'cpu_role':cpu_role})
    def acquire(phase):
        if local:
            store.set_phase(worker,'waiting_cpu',cpu_ready_at=time.time())
            while store.status(worker)['state'] in ACTIVE and store.status(worker).get('phase')=='waiting_cpu':
                time.sleep(.1)
        store.set_phase(worker,phase)
    progress()
    assigned = read_json(job_dir/'assignments.json',store.request(worker)['params'])
    while store.status(worker)['state'] in ACTIVE and not (store.root/'scheduler-stop.json').exists():
        assigned = read_json(job_dir/'assignments.json',store.request(worker)['params'])
        if retire_at is not None and time.monotonic()>=retire_at:
            # Finish each admitted task before returning unstarted assignments.
            # Reserve scheduler headroom for the longest expected individual task.
            break
        pending_scores = [job for job in assigned.get('scores',[]) if job not in scored|built]
        pending_scores.sort(key=lambda job:store.request(job)['task']!=batches.BUILD_TASK)
        pending_outputs = [job for job in assigned.get('publications',[]) if job not in published]
        pending = [job for job in assigned.get('features',[]) if job not in done]
        if pending_outputs:
            job = pending_outputs[0]
            folder = store.path(job)
            if store.status(job)['state'] not in ACTIVE:
                published.add(job)
                continue
            with file_lock(folder/'execution.lock',blocking=False) as acquired:
                if not acquired:
                    time.sleep(.1)
                    continue
                acquire('cpu_output')
                store.set_status(job,'running',host=socket.gethostname(),pid=os.getpid(),worker_job=worker)
                store.set_phase(job,'cpu_output')
                try:
                    result = publish_native(store.request(job)['params'],config)
                    write_json(folder/'result.json',result)
                    store.set_status(job,'done',finished=now(),exit_code=0)
                except Exception as exc:  # isolate a failed output task within the reusable service
                    import traceback
                    (folder/'execution-error.log').write_text(traceback.format_exc())
                    store.set_status(job,'failed',error=type(exc).__name__+': '+str(exc),finished=now(),exit_code=1)
            published.add(job)
            active_at = time.monotonic()
            progress()
        elif pending_scores:
            job = pending_scores[0]
            building = store.request(job)['task']==batches.BUILD_TASK
            completed = built if building else scored
            folder = store.path(job)
            if store.status(job)['state'] not in ACTIVE:
                completed.add(job)
                continue
            with file_lock(folder/'execution.lock',blocking=False) as acquired:
                if not acquired:
                    time.sleep(.1)
                    continue
                acquire('cpu_batching' if building else 'cpu_scoring')
                store.set_status(job,'running',host=socket.gethostname(),pid=os.getpid(),worker_job=worker)
                parent = store.request(job)['params'].get('_parent_job')
                if parent:
                    store.set_phase(parent,'cpu_scoring')
                os.environ['MSMS_CONFIG'] = str(folder/'config.json')
                try:
                    params = store.request(job)['params']
                    result = batches.build(params) if building else score(Path(params['prepared']),sys.executable)
                    write_json(folder/'result.json',result)
                    store.set_status(job,'done',finished=now(),exit_code=0)
                except (OSError,ValueError,RuntimeError) as exc:
                    store.set_status(job,'failed',error=str(exc),finished=now(),exit_code=1)
            completed.add(job)
            active_at = time.monotonic()
            progress()
        elif pending:
            feature = pending[0]
            acquire('cpu_preparation')
            if _prepare_feature(store,feature):
                prepared_ids.add(feature)
            done.add(feature)
            active_at = time.monotonic()
            progress()
        else:
            store.set_phase(worker,'cpu_idle')
            if assigned.get('retire') or time.monotonic()-active_at >= idle_seconds:
                with file_lock(job_dir/'assignments.lock'):
                    fresh = read_json(job_dir/'assignments.json',assigned)
                    if (set(fresh.get('features',[]))-done or set(fresh.get('scores',[]))-(scored|built) or
                            set(fresh.get('publications',[]))-published):
                        continue
                    progress(False)
                    break
            time.sleep(1)
    progress(False)
    return {'prepared_features':sorted(prepared_ids),'processed_features':sorted(done),'scored_jobs':sorted(scored),
            'built_groups':sorted(built),
            'published_jobs':sorted(published),'held_publications':sorted(set(assigned.get('publications',[]))-published),
            'held_features':sorted(set(assigned.get('features',[]))-done),
            'held_scores':sorted(set(assigned.get('scores',[]))-(scored|built))}


def ensure(store: JobStore, config: dict, python: str) -> None:
    """Start a detached backend owner once; new client requests do not run scheduling ticks."""
    from msms_structure_elucidation.mcp.scheduling import local_control
    control = local_control(store.root) / 'features'
    control.mkdir(exist_ok=True)
    if (store.root / 'scheduler-stop.json').exists():
        return
    with file_lock(control / 'startup.lock'):
        with file_lock(store.root / 'feature-owner.lock', blocking=False) as acquired:
            if not acquired:
                return
        snapshot = control / 'config.json'
        write_json(snapshot, config)
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')}
        # A driver owns dispatch; client observer environment must not demote it.
        env.pop('MSMS_PERSISTENT_CLIENT_CONTROL', None)
        env.pop('MSMS_MCP_ROLE', None)
        with (control / 'driver.log').open('ab') as log:
            process = subprocess.Popen([python, '-m', __name__, 'serve', '--root', str(store.root),
                '--config', str(snapshot)], env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        # Confirm ownership before another frontend process may attempt startup.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with file_lock(store.root / 'feature-owner.lock', blocking=False) as acquired:
                if not acquired:
                    return
            if process.poll() is not None:
                raise RuntimeError(f'Feature backend exited during startup; inspect {control / "driver.log"}')
            time.sleep(.05)
        raise RuntimeError(f'Feature backend startup timed out; inspect {control / "driver.log"}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'prepare', 'score'])
    parser.add_argument('--root')
    parser.add_argument('--config')
    parser.add_argument('--job')
    parser.add_argument('--prepared')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare_list(Path(args.job), read_json(Path(args.job) / 'config.json'))
        print('RESULT_JSON=' + json.dumps(result))
        return 0
    if args.action == 'score':
        from msms_structure_elucidation.preparation import score
        print('RESULT_JSON=' + json.dumps(score(Path(args.prepared), sys.executable)))
        return 0
    from msms_structure_elucidation.mcp.backends import make_backend
    store = JobStore(Path(args.root))
    with file_lock(store.root / 'feature-owner.lock', blocking=False) as acquired:
        if not acquired:
            return 0
        config = read_json(Path(args.config))
        config['_mcp_feature_driver'] = True
        config['_mcp_dispatcher'] = {'host': socket.gethostname(), 'pid': os.getpid()}
        config.pop('_mcp_cpu_dispatcher', None)
        config.setdefault('mcp', {})['dispatcher_role'] = 'owner'
        backend = make_backend(config['mcp']['backend'], store, config, sys.executable)
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        write_json(store.root / 'feature-service.json', {'state': 'running', 'pid': os.getpid(),
            'host': socket.gethostname(), 'heartbeat': time.time(), 'phase': 'starting'})
        idle = time.monotonic()
        inventory_at = 0.
        try:
            while not stopping and not (store.root / 'scheduler-stop.json').exists():
                backend.pump()
                if time.monotonic() - inventory_at >= float(config.get('mcp', {}).get('poll_seconds', 15)):
                    write_json(store.root / 'feature-backend-info.json', backend.describe())
                    inventory_at = time.monotonic()
                rows = store.active()
                if rows or store.has_active():
                    idle = time.monotonic()
                elif time.monotonic() - idle > 60:
                    break
                write_json(store.root / 'feature-service.json', {'state': 'running', 'pid': os.getpid(),
                    'host': socket.gethostname(), 'heartbeat': time.time(), 'active_jobs': len(rows)})
                time.sleep(.5)
        finally:
            backend.close() if hasattr(backend, 'close') else None
            write_json(store.root / 'feature-service.json', {'state': 'stopped', 'pid': os.getpid(),
                'host': socket.gethostname(), 'heartbeat': time.time()})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
