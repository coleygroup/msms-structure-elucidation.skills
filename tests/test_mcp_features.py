"""Durable feature lists and shared prediction ownership independent of client polling."""
import json
import os
import socket
import sys
import tempfile
import sqlite3
import time
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp.features import FeatureBackend, balanced_lists, prepare_list
from msms_structure_elucidation.mcp.jobs import JobStore, read_json, write_json
from msms_structure_elucidation.prediction_cache import file_fingerprint
from msms_structure_elucidation import prediction_cache as cache


class Core:
    max_jobs = 1

    def __init__(self, store):
        self.store = store
        self.exclude_tasks = set()
        self.submitted = []

    def pump(self):
        pass

    def submit(self, job):
        self.submitted.append(job)
        self.store.set_status(job, 'running')

    def poll(self, job):
        return self.store.status(job)

    def finish(self, job, result=None):
        if self.store.request(job)['task'] == 'model_inference' and result is None:
            folder = self.store.path(job) / 'native_outputs'
            folder.mkdir(exist_ok=True)
            args = self.store.request(job)['params']['arguments']
            rows = [{'smiles':smi,'predicted_spectra':{str(e):[[50.,1.]] for e in args['energies']},
                     'predicted_fragment_ids':{str(e):['1'] for e in args['energies']}} for smi in args['smiles']]
            (folder / 'raw-predictions.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            result = {'save_dir':str(folder)}
        write_json(self.store.path(job) / 'result.json', result or {})
        self.store.set_status(job, 'done')


class FeatureTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / 'jobs')
        self.config = {'_mcp_feature_driver': True,
            '_mcp_dispatcher': {'host': socket.gethostname(), 'pid': os.getpid()},
            'mcp': {'backend': 'local', 'feature_cpu_workers': 2, 'route_canary': False,
                    'feature_cpu_output': False,
                    'prediction_group_size': 100, 'feature_prediction_groups': 2, 'feature_async_publication': False}}
        self.core = Core(self.store)
        self.backend = FeatureBackend(self.core, self.store, self.config, None)
        self.addCleanup(lambda: self.backend.close())

    def feature(self, index):
        job, _ = self.store.create('elucidate_feature', {'feature_id': str(index), 'model': 'glacier',
            'collision_unit': 'eV'}, kind='pipeline', config=self.config)
        self.store.set_launch(job, backend='feature-pipeline')
        return job

    def ready(self, job, smiles, condition='same'):
        folder = self.store.path(job) / 'retrieval'
        folder.mkdir(exist_ok=True)
        request = folder / 'request.json'
        native = folder / 'native.json'
        write_json(native, {'entries':[[smi,'pred_test',20,'[M+H]+','Orbitrap','ikey',None] for smi in smiles],
                            'options':{'num_cpu_workers':4}})
        write_json(request, {'formula': 'test', 'condition_id': condition, 'smiles': smiles,
            'conditions': {'model': 'glacier', 'adduct': '[M+H]+', 'collision_unit': 'eV',
                           'collision_energies': [20], 'instrument': 'Orbitrap'}, 'identity': {'test': condition, 'cache': str(self.root/'cache')},
            'raw_cache_identity': {'test':condition}, 'native_prepared':str(native),
            'inference': {'arguments':{'model':'glacier','energies':[20]},'publish_raw_records':True,
                          'native_source':'test-native-source'}})
        write_json(folder / 'prepared.json', {'requests': [str(request)]})
        write_json(folder / 'ready.json', {'prepared': str(folder / 'prepared.json'),
            'sha256': file_fingerprint(folder / 'prepared.json', folder / 'fingerprints')})

    def jobs(self, task):
        return [row['job_id'] for row in self.store.list(1000) if row['task'] == task]

    def test_balanced_lists_and_no_per_feature_allocations(self):
        features = [self.feature(i) for i in range(7)]
        self.backend.pump()
        jobs = self.jobs('prepare_feature_list')
        self.assertEqual(len(jobs), 2)
        assigned = [self.store.request(job)['params']['features'] for job in jobs]
        self.assertEqual(sorted(map(len, assigned)), [3,4])
        self.assertEqual({item for values in assigned for item in values}, set(features))
        self.assertTrue(all(self.store.launch(job)['backend'] == 'feature-pipeline' for job in features))
        self.assertEqual(balanced_lists([], 3), [])
        with self.assertRaises(ValueError):
            balanced_lists(features, 0)

    def test_queue_connections_apply_the_configured_memory_budget(self):
        self.config['mcp']['feature_queue_cache_mb'] = 128
        connection = self.backend.controller.connect()
        try:
            self.assertEqual(connection.execute('PRAGMA cache_size').fetchone()[0], -131072)
            self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='consumers'").fetchone())
        finally:
            connection.close()
        self.config['mcp']['feature_queue_cache_mb'] = 0
        with self.assertRaisesRegex(ValueError, 'feature_queue_cache_mb must be positive'):
            self.backend.controller.connect()

    def test_existing_score_job_does_not_rescan_prediction_consumers(self):
        feature=self.feature('scoring-waiter')
        score,_=self.store.create('score_prepared_feature',{},kind='cpu')
        db=self.backend.controller.connect();self.addCleanup(db.close)
        db.execute('INSERT INTO features VALUES (?,?,?)',(feature,'unused-after-score-admission',score));db.commit()
        queries=[];db.set_trace_callback(queries.append)
        self.backend.controller._score(db,feature)
        self.assertFalse(any('consumers' in query for query in queries))
        self.core.finish(score,{'scored_structures':3})
        self.backend.controller._score(db,feature)
        self.assertEqual(self.store.status(feature)['state'],'done')
        self.assertEqual(self.store.result(feature),{'scored_structures':3})

    def test_ready_feature_does_not_reinspect_its_preparation_service(self):
        feature=self.feature('ready');self.ready(feature,['CC'])
        write_json(self.store.path(feature)/'feature-stage.json',{'prepare_job':'old-service'})
        with patch.object(self.backend.controller,'_status',side_effect=AssertionError('Ready preparation was reinspected')):
            self.backend.controller._reconcile_preparation([self.store.status(feature)|{'job_id':feature}])

    def test_cleanup_only_queue_is_active_until_acknowledgements_finish(self):
        feature=self.feature('last-result');self.store.set_status(feature,'done')
        self.store.active()
        self.assertFalse(self.store.has_active())
        for name in ('worker-cleanup-pending','cleanup-pending'):
            folder=self.store.root/name;folder.mkdir(exist_ok=True);marker=folder/feature;marker.touch()
            self.assertTrue(self.store.has_active())
            marker.unlink();self.assertFalse(self.store.has_active())

    def test_observer_restarts_owner_to_drain_cleanup_without_model_work(self):
        feature=self.feature('cleanup-owner');self.store.set_status(feature,'done');self.store.active()
        pending=self.store.root/'worker-cleanup-pending';pending.mkdir();(pending/feature).touch()
        observer=FeatureBackend(Core(self.store),self.store,{'mcp':{}},None)
        with patch('msms_structure_elucidation.mcp.features.ensure') as ensure:
            observer.pump()
        ensure.assert_called_once()

    def test_closed_worker_request_recovers_a_missing_cleanup_marker(self):
        feature=self.feature('migrated-result')
        self.store.set_launch(feature,backend='feature-worker',server='host',remote_job_id='remote')
        self.store.set_status(feature,'done',remote_cleanup_pending=True)
        self.store.active()
        worker=self.store.root/'worker-cleanup-pending'/feature;worker.unlink(missing_ok=True)
        native=self.store.root/'cleanup-pending'/feature;native.unlink(missing_ok=True)
        self.store.status(feature)
        self.assertTrue(worker.exists());self.assertFalse(native.exists())
        self.assertTrue(self.store.has_active())

    def test_native_cleanup_recovers_its_own_index(self):
        feature=self.feature('native-cleanup')
        self.store.set_launch(feature,backend='pool',server='host',remote_job_id='remote')
        self.store.set_status(feature,'done',remote_cleanup_pending=True)
        self.store.status(feature)
        self.assertTrue((self.store.root/'cleanup-pending'/feature).exists())
        self.assertFalse((self.store.root/'worker-cleanup-pending'/feature).exists())

    def test_completed_cpu_batch_reaches_gpu_during_feature_reconciliation(self):
        from msms_structure_elucidation.mcp import batches
        controller=self.backend.controller
        prepared=self.root/'assembled.json'
        write_json(prepared,{'entries':[['C','candidate',20]],'options':{}})
        builder,_=self.store.create(batches.BUILD_TASK,{'fixture':True},kind='cpu',config=self.config)
        self.store.set_status(builder,'running')
        group,_=self.store.create('model_inference',{'batch_plan':'fixture-plan','prepared':str(prepared),
            'expected_spectra':1,'publish_raw_records':False,'arguments':{'model':'glacier'}},kind='gpu',
            config=self.config,initial_state='staging')
        write_json(self.store.path(group)/'batch-build.json',{'job_id':builder})
        with controller.connect() as db:
            db.execute('INSERT INTO groups VALUES (?,?)',(group,'preparing'))
        for index in range(65):self.feature('reconcile-'+str(index))
        checked=[]
        def score(db,feature):
            if not checked:
                self.core.finish(builder,{'spectra':1,'prepared_sha256':file_fingerprint(prepared,controller.root/'fingerprints')})
            if len(checked)==32:
                self.assertEqual(self.store.status(group)['state'],'running')
                self.assertIn(group,self.core.submitted)
            checked.append(feature)
        with patch.object(controller,'_prepare'),patch.object(controller,'_score',side_effect=score):
            self.backend.pump()
        self.assertEqual(len(checked),65)
        self.assertEqual(self.core.submitted.count(group),1)

    def test_incremental_arrival_uses_free_cpu_slot_before_busy_worker_queue(self):
        first=self.feature('first');self.backend.pump()
        second=self.feature('second');self.backend.pump()
        self.assertEqual(len(self.jobs('prepare_feature_list')),2)
        self.assertNotEqual(read_json(self.store.path(first)/'feature-stage.json')['prepare_job'],
                            read_json(self.store.path(second)/'feature-stage.json')['prepare_job'])

    def test_observer_poll_does_not_rescan_all_requests(self):
        job=self.feature('observer');self.store.active()
        config={k:v for k,v in self.config.items() if k!='_mcp_feature_driver'}
        observer=FeatureBackend(self.core,self.store,config,None)
        self.addCleanup(observer.close)
        with patch.object(self.store,'active',side_effect=AssertionError('Observer scanned all requests')), \
                patch('msms_structure_elucidation.mcp.features.ensure') as ensure:
            self.assertEqual(observer.poll(job)['state'],'queued')
        ensure.assert_called_once()

    def test_incremental_outputs_fill_cpu_pool_instead_of_one_long_serial_queue(self):
        self.config['mcp'].update(feature_cpu_output=True,feature_output_workers=2)
        first=self.feature('first-output');self.ready(first,['C'],'first-output');self.backend.pump()
        self.core.finish(self.jobs('model_inference')[0]);self.backend.pump()
        self.assertEqual(len(self.jobs('publish_prediction_list')),1)
        second=self.feature('second-output');self.ready(second,['CC'],'second-output');self.backend.pump()
        next_job=next(job for job in self.jobs('model_inference') if self.store.status(job)['state']!='done')
        self.core.finish(next_job);self.backend.pump()
        self.assertEqual(len(self.jobs('publish_prediction_list')),2)

    def test_shared_configuration_survives_first_feature_cleanup_and_restart(self):
        a=self.feature('first');self.backend.pump();self.ready(a,['CC']);self.backend.pump()
        self.store.set_status(a,'done');shutil.rmtree(self.store.path(a))
        b=self.feature('later');self.ready(b,['CCC'])
        self.backend.close();self.backend=FeatureBackend(self.core,self.store,self.config,None)
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)
        with self.backend.controller.connect() as db:
            config=Path(db.execute('SELECT config FROM conditions').fetchone()[0])
        self.assertEqual(config.parent,self.backend.controller.root/'condition-configs')
        self.assertTrue(config.is_file())

    def test_deleted_legacy_feature_config_recovers_from_recorded_native_job(self):
        a=self.feature('legacy');self.backend.pump();self.ready(a,['CC']);self.backend.pump()
        with self.backend.controller.connect() as db:
            db.execute('UPDATE conditions SET config=?',(str(self.store.path(a)/'config.json'),));db.commit()
        self.store.set_status(a,'done');shutil.rmtree(self.store.path(a))
        b=self.feature('new-consumer');self.ready(b,['CCC']);self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)
        with self.backend.controller.connect() as db:
            config=Path(db.execute('SELECT config FROM conditions').fetchone()[0])
        self.assertTrue(config.is_file())
        self.assertEqual(read_json(config),self.config)

    def test_overlap_grouped_once_then_scored_separately_after_restart(self):
        a,b = self.feature('a'),self.feature('b')
        self.backend.pump()
        self.ready(a, ['C','CC'])
        self.ready(b, ['CC','CCC'])
        self.backend.pump()
        groups = self.jobs('model_inference')
        self.assertEqual(len(groups), 1)
        self.assertEqual(self.store.request(groups[0])['params']['arguments']['smiles'], ['C','CC','CCC'])
        self.assertEqual(self.jobs('score_prepared_feature'), [])
        self.core.finish(groups[0])
        # A new driver consumes the same filesystem/SQLite ownership records.
        self.backend.close()
        self.backend = FeatureBackend(self.core, self.store, self.config, None)
        self.backend.pump()
        scores = self.jobs('score_prepared_feature')
        self.assertEqual(len(scores), 2)
        self.assertEqual(len(self.jobs('model_inference')), 1)
        for job in scores:
            self.core.finish(job, {'status': 'ranked', 'candidates': [{'smiles': 'CC'}]})
        self.backend.pump()
        self.assertEqual([self.store.status(job)['state'] for job in (a,b)], ['done','done'])
        self.assertEqual(self.store.result(a)['status'], 'ranked')

    def test_invalid_prepared_group_does_not_stop_another_condition(self):
        bad,good = self.feature('bad'),self.feature('good')
        self.backend.pump()
        self.ready(bad,['C'],'bad')
        self.ready(good,['CC'],'good')
        native = self.store.path(bad)/'retrieval/native.json'
        payload = read_json(native)
        payload['entries'] = []
        write_json(native,payload)
        self.backend.pump()
        self.assertEqual(self.store.status(bad)['state'],'failed')
        self.assertIn('Incomplete CPU-prepared',self.store.status(bad)['error'])
        groups = self.jobs('model_inference')
        self.assertEqual(len(groups),1)
        self.assertEqual(self.store.request(groups[0])['params']['arguments']['smiles'],['CC'])
        self.assertEqual(self.store.status(good)['state'],'running')
        self.assertEqual(len(self.jobs('preparation_failure')),1)

    def test_cpu_output_wait_does_not_hold_gpu_or_repeat_inference_after_restart(self):
        self.config['mcp'].update(feature_cpu_output=True,feature_output_workers=1)
        feature=self.feature('cpu-output')
        self.backend.pump()
        self.ready(feature,['C','CC'])
        self.backend.pump()
        group=self.jobs('model_inference')[0]
        self.assertTrue(self.store.request(group)['params']['defer_raw_publication'])
        self.assertEqual(self.jobs('publish_prediction_list'),[])
        self.core.finish(group)
        self.backend.pump()
        services=self.jobs('publish_prediction_list')
        self.assertEqual(len(services),1)
        self.assertEqual(self.store.request(services[0])['kind'],'cpu')
        output=self.jobs('publish_prediction_group')[0]
        self.assertEqual(self.store.status(group)['state'],'done')
        self.assertEqual(self.store.status(feature)['phase'],'waiting_output')
        self.assertEqual(self.store.launch(output)['backend'],'cpu-worker')
        self.assertEqual(self.jobs('score_prepared_feature'),[])
        other=self.feature('another')
        self.backend.pump()
        self.ready(other,['CCC'],'another-condition')
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)
        self.backend.close()
        self.backend=FeatureBackend(self.core,self.store,self.config,None)
        self.backend.pump()
        self.assertEqual(self.jobs('publish_prediction_group'),[output])
        self.core.finish(output,{'predicted_spectra':2,'input_format':'native-hdf5'})
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)
        self.assertEqual(read_json(self.store.path(group)/'cache-publication.json')['input_format'],'native-hdf5')
        self.assertEqual(len(self.jobs('score_prepared_feature')),1)

    def test_output_failure_is_recorded_once_without_retrying_the_gpu(self):
        self.config['mcp'].update(feature_cpu_output=True)
        feature=self.feature('broken-output')
        self.backend.pump()
        self.ready(feature,['C'])
        self.backend.pump()
        group=self.jobs('model_inference')[0]
        self.core.finish(group)
        self.backend.pump()
        output=self.jobs('publish_prediction_group')[0]
        attempt=self.store.request(output)['attempt']
        self.store.set_status(output,'failed',error='fixture incomplete HDF5')
        self.backend.pump()
        self.assertEqual(self.store.status(feature)['state'],'failed')
        self.assertIn('fixture incomplete HDF5',self.store.status(feature)['error'])
        self.backend.pump()
        self.assertEqual(self.store.request(output)['attempt'],attempt)
        self.assertEqual(len(self.jobs('model_inference')),1)

    def test_cpu_output_service_reuses_one_process_for_separate_publications(self):
        import types
        config={'mcp':{'backend':'slurm','feature_output_idle_seconds':0}}
        first,_=self.store.create('publish_prediction_group',{'number':1},kind='cpu',config=config)
        second,_=self.store.create('publish_prediction_group',{'number':2},kind='cpu',config=config)
        service,_=self.store.create('publish_prediction_list',{'features':[],'scores':[],
            'publications':[first,second]},kind='cpu',config=config)
        self.store.set_status(service,'running')
        seen=[]
        def publish(params,settings):
            seen.append(params['number'])
            self.assertEqual(self.store.status(service)['phase'],'cpu_output')
            return {'input_format':'native-hdf5'}
        with patch.dict(sys.modules,{'ms_pred':types.SimpleNamespace(common=object())}), \
                patch('msms_structure_elucidation.mcp.raw_outputs.publish_native',side_effect=publish):
            result=prepare_list(self.store.path(service),config)
        self.assertEqual(seen,[1,2])
        self.assertEqual(set(result['published_jobs']),{first,second})
        self.assertEqual([self.store.status(job)['state'] for job in (first,second)],['done','done'])
        self.assertEqual(read_json(self.store.path(service)/'worker-progress.json')['published_groups'],2)

    def test_group_construction_yields_before_filling_a_large_queue(self):
        self.config['mcp'].update(prediction_group_size=1,feature_prediction_groups=16,
                                  feature_group_builds_per_tick=2)
        feature = self.feature('many-groups')
        self.backend.pump()
        self.ready(feature,['C'*size for size in range(1,9)])
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),4)

    def test_new_requests_reuse_queued_work_and_different_conditions_do_not_mix(self):
        a = self.feature('a')
        self.backend.pump()
        self.ready(a, ['C','CC'])
        self.backend.pump()
        b,c = self.feature('b'),self.feature('c')
        self.ready(b, ['CC'])
        self.ready(c, ['CC'], 'different-energy')
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')), 2)
        with self.backend.controller.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 3)

    def test_cancel_consumer_retains_shared_prediction_and_other_feature(self):
        a,b = self.feature('a'),self.feature('b')
        self.backend.pump()
        self.ready(a, ['CC']); self.ready(b, ['CC'])
        self.backend.pump()
        self.backend.cancel(a)
        group = self.jobs('model_inference')[0]
        self.assertEqual(self.store.status(group)['state'], 'running')
        self.core.finish(group)
        self.backend.pump()
        self.assertEqual(len(self.jobs('score_prepared_feature')), 1)
        self.assertEqual(self.store.status(a)['state'], 'cancelled')

    def test_failed_cpu_list_retains_published_feature_and_accounts_unfinished_feature(self):
        features = [self.feature(i) for i in range(4)]
        self.backend.pump()
        job = self.jobs('prepare_feature_list')[0]
        a,b = self.store.request(job)['params']['features']
        self.ready(a, ['C'])
        self.store.set_status(job, 'failed', error='lost CPU node')
        self.backend.pump()
        self.assertEqual(self.store.status(b)['state'], 'failed')
        self.assertEqual(self.store.status(a)['state'], 'running')
        self.assertEqual(len(self.jobs('model_inference')), 1)

    def test_preparation_runs_multiple_features_without_waiting_for_predictions(self):
        values = [self.feature(i) for i in range(3)]
        for job in values:
            (self.store.path(job) / 'spectrum.ms').write_text('test')
        job, _ = self.store.create('prepare_feature_list', {'features': values}, kind='cpu', config=self.config)

        def prepare(args):
            folder = Path(args.output_dir)
            folder.mkdir(exist_ok=True)
            write_json(folder / 'ready.json', {'test': True})

        with patch('msms_structure_elucidation.cli.run', side_effect=prepare) as run, patch.dict(os.environ):
            result = prepare_list(self.store.path(job), {'mcp':{'backend':'slurm','feature_worker_idle_seconds':0}})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(set(result['prepared_features']), set(values))
        self.assertEqual(self.jobs('model_inference'), [])

    def test_cpu_service_retires_between_features_with_unstarted_assignments(self):
        values=[self.feature(i) for i in range(2)]
        for job in values:(self.store.path(job)/'spectrum.ms').write_text('test')
        job,_=self.store.create('prepare_feature_list',{'features':values},kind='cpu',config=self.config)
        def prepare(args):
            folder=Path(args.output_dir);folder.mkdir(exist_ok=True)
            write_json(folder/'ready.json',{'test':True});time.sleep(.03)
        with patch('msms_structure_elucidation.cli.run',side_effect=prepare),patch.dict(os.environ):
            result=prepare_list(self.store.path(job),{'mcp':{'backend':'slurm','feature_worker_max_seconds':.01}})
        self.assertEqual(result['processed_features'],values[:1])
        self.assertEqual(result['held_features'],values[1:])

    def test_frontend_only_publishes_and_reads_while_driver_owns_scheduling(self):
        config = {**self.config, '_mcp_feature_driver': False}
        frontend = FeatureBackend(self.core, self.store, config, None)
        job, _ = self.store.create('predict_spectra', {'smiles': ['CC']}, kind='pipeline', config=config)
        with patch('msms_structure_elucidation.mcp.features.ensure') as ensure, \
                patch.object(self.core, 'poll', side_effect=AssertionError('frontend must not schedule')):
            frontend.submit(job)
            self.assertEqual(frontend.poll(job)['state'], 'queued')
            self.assertTrue(ensure.called)
            self.assertEqual(self.core.submitted, [])

    def test_one_backend_reconciliation_per_tick_without_recursive_fleet_polls(self):
        features = [self.feature(i) for i in range(4)]
        self.backend.pump()
        for feature in features:
            self.ready(feature, ['CC'])
        with patch.object(self.core, 'pump', wraps=self.core.pump) as pump, \
                patch.object(self.core, 'poll', side_effect=AssertionError('recursive backend poll')):
            self.backend.pump()
            self.backend.pump()
        self.assertEqual(pump.call_count, 2)

    def test_warm_cache_skips_gpu_and_partial_cache_predicts_only_missing_energies(self):
        from msms_structure_elucidation.mcp.raw_outputs import missing_groups
        a = self.feature('partial')
        self.backend.pump()
        self.ready(a, ['CC'])
        folder = self.store.path(a) / 'retrieval'
        request = read_json(folder/'request.json')
        request['conditions']['collision_energies'] = [20,40]
        write_json(folder/'request.json', request)
        native = read_json(folder/'native.json')
        native['entries'].append(['CC','pred_test',40,'[M+H]+','Orbitrap','ikey',None])
        write_json(folder/'native.json',native)
        root, identity = self.root/'cache',{'test':'same'}
        cache.predictions(root,identity,['CC'],[20],lambda molecules,energies:[
            {'smiles':'CC','predicted_spectra':{'20':[[50.,1.]]},'predicted_fragment_ids':{'20':['1']}}])
        self.backend.pump()
        group = self.jobs('model_inference')[0]
        self.assertEqual(self.store.request(group)['params']['arguments']['energies'], [40])
        self.assertEqual(read_json(Path(self.store.request(group)['params']['prepared']))['entries'][0][2],40)
        self.core.finish(group)
        self.backend.pump()
        self.assertEqual(missing_groups(root,identity,['CC'],[20,40]), {():['CC']})
        b = self.feature('warm')
        self.ready(b, ['CC'], 'warm')
        request_path = self.store.path(b)/'retrieval/request.json'
        request = read_json(request_path)
        request['raw_cache_identity'] = identity
        write_json(request_path,request)
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')), 1)
        self.assertEqual(len(self.jobs('cached_predictions')), 1)

    def test_locked_cache_defers_only_its_condition_without_reprediction(self):
        a,b=self.feature('busy'),self.feature('available')
        self.backend.pump();self.ready(a,['CC'],'busy');self.ready(b,['CCC'],'available')
        identity={'test':'busy'};root=self.root/'cache'
        cache.predictions(root,identity,['CC'],[20],lambda molecules,energies:[
            {'smiles':'CC','predicted_spectra':{'20':[[50.,1.]]},'predicted_fragment_ids':{'20':['1']}}])
        connection=sqlite3.connect(root/cache.digest(identity)/'spectra.sqlite3')
        try:
            connection.execute('BEGIN EXCLUSIVE')
            started=time.monotonic();self.backend.pump()
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual(self.store.status(a)['phase'],'waiting_cache')
            groups=self.jobs('model_inference')
            self.assertEqual(len(groups),1)
            self.assertEqual(self.store.request(groups[0])['params']['arguments']['smiles'],['CCC'])
        finally:
            connection.rollback();connection.close()
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),1)
        self.assertEqual(len(self.jobs('cached_predictions')),1)

    def test_cache_lease_wait_does_not_reserve_gpu_or_block_other_conditions(self):
        a,b = self.feature('leased'),self.feature('independent')
        self.backend.pump()
        self.ready(a,['C']); self.ready(b,['CC'],'other')
        folder = self.root/'cache'/cache.digest({'test':'same'})
        with cache.candidate_guard(folder,['C']), patch.object(cache.time,'sleep',side_effect=AssertionError('blocking cache wait')):
            self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),1)
        self.backend.pump()
        self.assertEqual(len(self.jobs('model_inference')),2)

    def test_gpu_groups_are_submitted_directly_without_cpu_prediction_parents(self):
        a,b = self.feature('a'),self.feature('b')
        self.backend.pump()
        self.ready(a,['C']); self.ready(b,['CC'])
        self.backend.pump()
        group = self.jobs('model_inference')[0]
        self.assertEqual(self.store.request(group)['kind'],'gpu')
        self.assertEqual(self.jobs('predict_spectra'),[])
        self.assertTrue(self.store.request(group)['params']['prepared'])

    def test_later_features_and_scores_reuse_the_existing_cpu_allocations(self):
        a,b = self.feature('a'),self.feature('b')
        self.backend.pump()
        workers = set(self.jobs('prepare_feature_list'))
        self.ready(a,['C']); self.ready(b,['CC'])
        self.backend.pump()
        self.core.finish(self.jobs('model_inference')[0])
        self.backend.pump()
        scores = self.jobs('score_prepared_feature')
        self.assertTrue(scores)
        self.assertTrue(all(self.store.launch(job)['worker_job'] in workers for job in scores))
        self.assertFalse(any(job in self.core.submitted for job in scores))
        later = self.feature('later')
        self.backend.pump()
        self.assertEqual(set(self.jobs('prepare_feature_list')),workers)
        self.assertIn(self.store.status(later)['preparation_job'],workers)

    def test_cache_only_scoring_stays_in_the_persistent_scientific_process(self):
        from msms_structure_elucidation import cli
        listing = self.root/'smiles.json'; listing.write_text('["CC"]')
        with patch.dict(os.environ,{'MSMS_PREDICTION_CACHE_ONLY':'1','MSMS_FEATURE_PREPARATION':'1'}), \
                patch('msms_structure_elucidation.worker.simulate',return_value=['scored']) as score, \
                patch.object(cli.subprocess,'run',side_effect=AssertionError('new scientific process')):
            self.assertEqual(cli.worker(sys.executable,'simulate',smiles_json=listing,formula='C2H6',
                limit_images=True,top_k=10),['scored'])
        self.assertEqual(score.call_args.kwargs['smiles'],['CC'])


if __name__ == '__main__':
    unittest.main()
