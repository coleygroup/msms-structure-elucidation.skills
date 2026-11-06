"""Whole-worker placement, local execution boundaries and final-only collection."""
import hashlib
import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp import workers
from msms_structure_elucidation.mcp.jobs import JobStore,read_json,write_json
from msms_structure_elucidation.mcp.placement import WorkerFeatureBackend
from msms_structure_elucidation.mcp.network import NetworkQueue

CONTRACT={'ready':True,'model':'glacier','weights':{'checkpoint':'confirmed'},'source':'native',
          'adapter':'adapter','sparse_k':100,'stepped_aggregation':'arithmetic_mean'}
SPECTRUM='>compound unknown\n>parentmass 128.1\n>ionization [M+H]+\n>instrumentation Orbitrap\n>collision 20\n31.0184 1\n'


class Remote:
    def __init__(self,name,root):
        self.name,self.root=name,root
        self.settings={'features':True,'models':['glacier'],'backend':'local','cleanup':True}
        self.requests={};self.calls=[];self.lost_reply=False

    def call(self,operation,payload=None,output=None):
        self.calls.append((operation,payload))
        if operation=='worker-probe':
            return {'capabilities':['whole-feature'],'contract':CONTRACT,'feature_slots':8,
                    'active_features':sum(r['status']['state']=='running' for r in self.requests.values()),'free_gpus':[0]}
        if operation=='worker-submit':
            job=JobStore.job_id(payload['task'],{key:payload[key] for key in ('coordinator','job_id','attempt')})
            self.requests.setdefault(job,{'payload':payload,'status':{'state':'running','phase':'cpu_preparation'}})
            if self.lost_reply:
                self.lost_reply=False
                raise RuntimeError('lost submission reply')
            return {'job_id':job,'status':self.requests[job]['status']}
        job=payload['remote_job_id'];request=self.requests[job]
        if operation=='worker-poll':return {'status':request['status'],'inventory':request.get('inventory')}
        if operation=='worker-cancel':
            request['status']={'state':'cancelled'}
            return {'status':request['status']}
        if operation=='worker-bundle':
            with tarfile.open(output,'w') as archive:
                for name in request['inventory']:
                    archive.add(self.root/job/name,arcname=name)
            return
        if operation=='worker-cleanup':
            self.assertion=payload['acknowledged_inventory']==request['inventory']
            return {'cleaned':True}
        raise AssertionError(operation)

    def finish(self,job):
        folder=self.root/job;folder.mkdir(parents=True)
        (folder/'retrieval').mkdir()
        write_json(folder/'result.json',{'status':'ranked','scored_structures':2,'candidates':[{'smiles':'CCO'}],
                                        'input':str(folder/'spectrum.ms'),'spectra':{'20':[[31.,1.]]}})
        (folder/'retrieval/report.html').write_text('final report')
        (folder/'spectrum.ms').write_text(SPECTRUM)
        write_json(folder/'execution.json',{'native_data_location':'worker'})
        inventory={p.relative_to(folder).as_posix():{'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in folder.rglob('*') if p.is_file()}
        self.requests[job].update(status={'state':'done'},inventory=inventory)


class Core:
    name='pool'
    def __init__(self,endpoints):
        self.endpoints={node.name:node for node in endpoints}
        self.cpu=types.SimpleNamespace(exclude_tasks=set())
        self.exclude_tasks=set();self.network=None;self.coordinator_id='a'*64
    def pump(self):pass
    def capacity(self):return len(self.endpoints)
    def _call(self,endpoint,operation,payload,identity):return endpoint.call(operation,payload)
    def submit(self,job):raise AssertionError('The coordinator must not launch scientific CPU/GPU work')
    def describe(self):return {'backend':'pool'}
    def close(self):pass


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=JobStore(self.root/'central');self.a=Remote('a',self.root/'a');self.b=Remote('b',self.root/'b')
        self.config={'_mcp_feature_driver':True,'_mcp_dispatcher':{'host':socket.gethostname(),'pid':os.getpid()},
            'mcp':{'backend':'pool','feature_pipeline':True,'feature_placement':'workers','feature_async_publication':False,'poll_seconds':0}}
        self.backend=WorkerFeatureBackend(Core([self.a,self.b]),self.store,self.config,None)
        self.addCleanup(self.backend.close)

    def feature(self,feature,formula='C6H9NO2',spectrum=SPECTRUM):
        params={'feature_id':feature,'model':'glacier','collision_unit':'eV','instrument':'Orbitrap','_model_identity':CONTRACT}
        if formula:params['formula']=formula
        job,_=self.store.create('elucidate_feature',params,{'spectrum.ms':spectrum},'pipeline',self.config)
        self.backend.submit(job);return job

    def test_dispatch_only_and_formula_affinity_with_final_artifact_collection(self):
        first=self.feature('one');second=self.feature('two')
        self.backend.pump();self.backend.pump()
        self.assertEqual(self.store.launch(first)['server'],self.store.launch(second)['server'])
        self.assertEqual(len(self.a.requests),2)
        self.assertEqual(len(self.b.requests),0)
        self.assertEqual([row['task'] for row in self.store.active()],['elucidate_feature','elucidate_feature'])
        for remote,request in self.a.requests.items():
            self.assertEqual(set(request['payload']['inputs']),{'spectrum.ms'})
            self.assertNotIn('prepared',request['payload'])
            self.assertEqual(request['payload']['contract']['stepped_aggregation'],'arithmetic_mean')
            self.a.finish(remote)
        self.backend.pump()
        for job in (first,second):
            self.assertEqual(self.store.status(job)['state'],'done')
            result=self.store.result(job)
            self.assertTrue(Path(result['report_path']).is_file())
            self.assertEqual(result['scored_structures'],2)
            self.assertEqual(result['worker_execution']['server'],'a')
            self.assertEqual(Path(result['retrieval_path']),self.store.path(job)/'retrieval/retrieval.json')
        self.assertTrue(self.a.assertion)

    def test_bulk_placement_reads_assignments_linearly_and_preserves_slot_limits(self):
        jobs=[]
        for index in range(40):
            params={'feature_id':str(index),'model':'glacier','collision_unit':'eV',
                    'formula':f'C{index+1}H{index+2}','_model_identity':CONTRACT}
            spectrum=SPECTRUM.replace('128.1',str(128.1+index*10))
            job,_=self.store.create('elucidate_feature',params,{'spectrum.ms':spectrum},'pipeline',self.config)
            self.store.set_launch(job,backend='feature-worker');jobs.append(job)
        with patch.object(self.store,'launch',wraps=self.store.launch) as reads:
            self.backend.pump()
        self.assertLess(reads.call_count,10*len(jobs))
        owners=[self.store.launch(job).get('server') for job in jobs]
        self.assertEqual(owners.count('a'),8)
        self.assertEqual(owners.count('b'),8)

    def test_scoring_backlog_preserves_worker_prediction_capacity_and_its_own_bound(self):
        self.b.settings['features']=False
        for index in range(8):
            job,_=self.store.create('elucidate_feature',{'feature_id':str(index)},kind='pipeline')
            self.store.set_launch(job,backend='feature-worker',server='a',remote_job_id='old')
            self.store.set_status(job,'running',phase='waiting_cpu',worker_status={'scoring_job':'score-'+str(index)})
        job,_=self.store.create('elucidate_feature',{'feature_id':'new','model':'glacier',
            '_model_identity':CONTRACT},{'spectrum.ms':SPECTRUM},kind='pipeline')
        self.store.set_launch(job,backend='feature-worker')
        probe={'capabilities':['whole-feature'],'contract':CONTRACT,'feature_slots':8,'free_gpus':[0],
               'active_features':8,'stage_backlog':{'execution_pending':0,'postprediction_pending':8,'delivery_pending':0},
               'postprediction_slots':8}
        with patch.object(self.backend.placements,'_probe',return_value=probe),patch.object(self.backend.placements,'_poll'):
            self.backend.placements.pump()
            self.assertIsNone(self.store.launch(job).get('server'))
            probe['postprediction_slots']=16
            self.backend.placements.pump()
        self.assertEqual(self.store.launch(job)['server'],'a')

    def test_precursor_affinity_keeps_buddy_unknowns_together(self):
        first=self.feature('buddy-one',None)
        second=self.feature('buddy-two',None,SPECTRUM.replace('128.1','128.1004'))
        self.backend.pump()
        self.assertEqual(self.store.launch(first)['server'],self.store.launch(second)['server'])

    def test_colocated_status_reads_preserve_health_rpc_and_final_verification(self):
        first=self.feature('health');second=self.feature('local-status');self.backend.pump();self.backend.pump()
        self.a.settings.update(transport='local',job_root=str(self.a.root))
        remote=self.store.launch(second)['remote_job_id'];folder=self.a.root/remote;folder.mkdir(parents=True)
        write_json(folder/'request.json',{'task':'elucidate_feature','kind':'pipeline','params':{}})
        write_json(folder/'status.json',{'state':'running','phase':'waiting_cpu','scoring_job':'cache-only-score'})
        placement=self.backend.placements;placement.local_poll_owners={'a':first};self.a.calls.clear()
        placement._poll(second)
        self.assertEqual(self.store.status(second)['worker_status']['scoring_job'],'cache-only-score')
        self.assertEqual(self.a.calls,[])
        placement._poll(first)
        self.assertEqual([operation for operation,_ in self.a.calls],['worker-poll'])
        write_json(folder/'status.json',{'state':'done'})
        placement._poll(second)
        self.assertEqual(self.a.calls[-1][0],'worker-poll')
        self.assertNotEqual(self.store.status(second)['state'],'done','Final manifests still require the worker protocol')

    def test_finished_local_health_owner_is_replaced(self):
        first=self.feature('old-health');second=self.feature('next-health');self.backend.pump();self.backend.pump()
        self.backend.placements.local_poll_owners={'a':first}
        self.store.set_status(first,'done')
        self.backend.placements.pump()
        self.assertNotIn(first,self.backend.placements.local_poll_owners.values())

    def test_unchanged_status_does_not_rewrite_the_coordinator_record(self):
        job=self.feature('stable');self.backend.pump();self.backend.pump()
        with patch.object(self.store,'set_status',wraps=self.store.set_status) as writes:
            self.backend.placements._poll(job)
            self.assertEqual(writes.call_count,0)
        self.store.set_status(job,'running',transport_error='old connection failure')
        self.backend.placements._poll(job)
        self.assertIsNone(self.store.status(job)['transport_error'])

    def test_worker_timeout_preserves_ownership_without_exiting_the_dispatcher(self):
        job=self.feature('timeout');self.backend.pump()
        with patch.object(self.backend.placements,'_poll',side_effect=subprocess.TimeoutExpired('endpoint',20)):
            self.backend.placements.pump()
        self.assertEqual(self.store.launch(job)['server'],'a')
        self.assertIn('timed out',self.store.status(job)['transport_error'])
        self.assertEqual(self.store.status(job)['state'],'submitted')

    def test_new_submission_reaches_a_bounded_queue_with_many_old_polls(self):
        queue=NetworkQueue(1,1);queue.queue_limits['rpc']=2;self.addCleanup(queue.close)
        jobs=[]
        for index in range(7):
            job,_=self.store.create('elucidate_feature',{'feature_id':str(index)},kind='pipeline')
            self.store.set_launch(job,backend='feature-worker',server='a',remote_job_id='remote' if index<6 else None)
            self.store.set_status(job,'submitted',created=f'2026-01-01T00:00:{index:02d}')
            jobs.append(job)
        placement=self.backend.placements
        submitted=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def poll(job):
            def operation():
                if job==jobs[-1]:submitted.set()
                else:release.wait(3)
            queue.call(('worker-submit' if job==jobs[-1] else 'worker-poll','a',job),operation)
        with patch.object(placement,'_poll',side_effect=poll):
            placement.pump()
            self.assertTrue(submitted.wait(1),'Old polls must not fill the queue before new work enters it')

    def test_unpolled_features_precede_recently_polled_features(self):
        jobs=[]
        for index in range(3):
            job,_=self.store.create('elucidate_feature',{'feature_id':str(index)},kind='pipeline')
            self.store.set_launch(job,backend='feature-worker',server='a',remote_job_id='remote')
            self.store.set_status(job,'submitted',created=f'2026-01-01T00:00:{index:02d}')
            jobs.append(job)
        placement=self.backend.placements
        placement.poll_at[jobs[0]]=time.monotonic()-1
        visited=[]
        with patch.object(placement,'_poll',side_effect=visited.append):placement.pump()
        self.assertEqual(visited[-1],jobs[0])

    def test_completed_async_transfers_release_slots_before_next_feature(self):
        queue=NetworkQueue(1,1);queue.queue_limits['transfer']=1
        self.backend.core.network=queue;self.addCleanup(queue.close)
        for index in range(3):
            job=self.feature('async-'+str(index));self.backend.pump()
            remote=self.store.launch(job)['remote_job_id'];self.a.finish(remote)
            self.backend.pump()
            self.assertEqual(self.store.status(job)['phase'],'result_collection')
            deadline=time.monotonic()+3
            while not (self.store.path(job)/'worker-output').exists() and time.monotonic()<deadline:
                time.sleep(.005)
            self.assertTrue((self.store.path(job)/'worker-output').exists())
            while self.store.status(job)['state']!='done' and time.monotonic()<deadline:
                self.backend.pump();time.sleep(.005)
            self.assertEqual(self.store.status(job)['state'],'done')
            self.assertFalse(any(pool=='transfer' for pool,key in queue.futures))

    def test_cached_viewer_collection_releases_transfer_slot(self):
        queue=NetworkQueue(1,1);queue.queue_limits['transfer']=1
        self.backend.core.network=queue;self.addCleanup(queue.close)
        job=self.feature('cached-viewer');self.backend.pump()
        remote=self.store.launch(job)['remote_job_id'];self.a.finish(remote)
        cache=self.a.root/remote/'viewer_structures.json';write_json(cache,{'fixture':True})
        self.a.requests[remote]['inventory']['viewer_structures.json']={
            'size':cache.stat().st_size,'sha256':hashlib.sha256(cache.read_bytes()).hexdigest()}
        def render(results,page,**kwargs):page.write_text('cached viewer')
        deadline=time.monotonic()+3
        with patch('msms_structure_elucidation.visualize.export_static',side_effect=render):
            while self.store.status(job)['state']!='done' and time.monotonic()<deadline:
                self.backend.pump();time.sleep(.005)
        self.assertEqual(self.store.status(job)['state'],'done')
        self.assertEqual(read_json(Path(self.store.result(job)['viewer_structures_path'])),{'fixture':True})
        self.assertFalse(any(pool=='transfer' for pool,key in queue.futures))

    def test_slow_report_finalization_does_not_block_new_worker_submissions(self):
        queue=NetworkQueue(1,1);release=threading.Event();rendering=threading.Event()
        self.backend.core.network=queue
        self.addCleanup(queue.close);self.addCleanup(release.set)
        first=self.feature('slow-report');self.backend.pump()
        self.a.finish(self.store.launch(first)['remote_job_id'])
        def render(result,page):
            rendering.set();release.wait(3);page.write_text('report')
        with patch('msms_structure_elucidation.report.write_report',side_effect=render):
            self.backend.pump()
            self.assertTrue(rendering.wait(1))
            second=self.feature('next-request');self.backend.pump()
            self.assertIsNotNone(self.store.launch(second).get('remote_job_id'))
            self.assertEqual(self.store.status(first)['phase'],'result_collection')
            self.assertFalse(any(op=='worker-cleanup' for op,_ in self.a.calls))
            release.set();deadline=time.monotonic()+3
            while self.store.status(first)['state']!='done' and time.monotonic()<deadline:
                self.backend.pump();time.sleep(.005)
        self.assertEqual(self.store.status(first)['state'],'done')
        self.assertTrue(self.a.assertion)

    def test_lost_submission_reply_retains_owner_and_recovers_one_remote_job(self):
        self.a.lost_reply=True
        job=self.feature('lost');self.backend.pump()
        self.assertEqual(self.store.launch(job)['server'],'a')
        self.assertIsNone(self.store.launch(job)['remote_job_id'])
        self.assertEqual(len(self.a.requests),1)
        self.backend.pump()
        self.assertEqual(len(self.a.requests),1)
        self.assertEqual(len(self.b.requests),0)
        self.assertIsNotNone(self.store.launch(job)['remote_job_id'])

    def test_cancel_recovers_a_lost_submission_before_confirming_cancellation(self):
        self.a.lost_reply=True
        job=self.feature('cancel-lost');self.backend.pump()
        self.backend.cancel(job);self.backend.pump()
        self.assertEqual(self.store.status(job)['state'],'cancelled')
        self.assertEqual(len(self.a.requests),1)
        self.assertEqual(next(iter(self.a.requests.values()))['status']['state'],'cancelled')

    def test_wrong_weights_hold_placement_without_sending_work(self):
        original=self.a.call
        def probe(operation,payload=None,output=None):
            result=original(operation,payload,output)
            if operation=='worker-probe':result['contract']={**CONTRACT,'weights':{'checkpoint':'wrong'}}
            return result
        self.a.call=probe;self.b.call=probe
        job=self.feature('wrong')
        self.assertEqual(self.store.status(job)['state'],'queued')
        self.assertIn('approved',str(self.store.status(job)['dispatch_errors']))
        self.assertFalse(self.a.requests)

    def test_changed_path_input_fails_before_remote_admission(self):
        path=self.root/'input.ms';path.write_text(SPECTRUM)
        params={'feature_id':'changed','model':'glacier','collision_unit':'eV','_model_identity':CONTRACT,
                'spectrum_path':str(path),'spectrum_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        job,_=self.store.create('elucidate_feature',params,{},'pipeline',self.config)
        path.write_text(SPECTRUM+'40 1\n')
        self.backend.submit(job)
        self.assertEqual(self.store.status(job)['state'],'failed')
        self.assertIn('changed',self.store.status(job)['error'])
        self.assertFalse(self.a.requests)

    def test_removed_assigned_worker_keeps_ownership_and_reports_recovery(self):
        job=self.feature('removed')
        self.backend.core.endpoints.pop('a')
        self.backend.pump()
        self.assertEqual(self.store.launch(job)['server'],'a')
        self.assertIn('restore',self.store.status(job)['transport_error'])
        self.assertFalse(self.b.requests)


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.endpoint={'job_root':str(self.root/'worker'),'python':'worker-python',
                       'backend':'local','simulator':{'python':'scientific-python','ms_pred_dir':str(self.root/'native')}}
        self.submitted=[]
        test=self
        class Backend:
            core=types.SimpleNamespace(describe=lambda:{'max_concurrent_jobs':1,'free_gpus':[0]})
            def __init__(self,store):self.store=store
            def submit(self,job):test.submitted.append(job);self.store.set_status(job,'running')
            def poll(self,job):return self.store.status(job)
            def cancel(self,job):return self.store.set_status(job,'cancelled')
        self.patch=patch('msms_structure_elucidation.mcp.backends.make_backend',side_effect=lambda name,store,*args:Backend(store))
        self.patch.start();self.addCleanup(self.patch.stop)
        self.contract=patch.object(workers,'model_contract',return_value=CONTRACT)
        self.contract.start();self.addCleanup(self.contract.stop)

    def submit(self):
        payload={'model':'glacier','task':'elucidate_feature','coordinator':'a'*64,'job_id':'central','attempt':'same',
                 'params':{'model':'glacier','stepped_aggregation':'arithmetic_mean'},'contract':CONTRACT,
                 'inputs':{'spectrum.ms':SPECTRUM}}
        return payload,workers.handle('worker-submit',payload,self.endpoint)

    def test_worker_keeps_all_stage_configuration_and_cache_on_its_host(self):
        payload,response=self.submit();store=JobStore(self.root/'worker');job=response['job_id']
        config=read_json(store.path(job)/'config.json')
        self.assertEqual(config['mcp']['backend'],'local')
        self.assertTrue(config['mcp']['feature_pipeline'])
        self.assertEqual(config['models']['simulator']['python'],'scientific-python')
        self.assertEqual(config['models']['simulator']['prediction_cache'],str(self.root/'worker/prediction-cache'))
        self.assertEqual(config['pipeline']['stepped_aggregation'],'arithmetic_mean')
        workers.handle('worker-submit',payload,self.endpoint)
        self.assertEqual(self.submitted,[job])
        store.set_status(job,'failed',error='fixture failed')
        workers.handle('worker-submit',payload,self.endpoint)
        self.assertEqual(self.submitted,[job])
        self.assertEqual(store.status(job)['state'],'failed')

    def test_probe_exposes_postprediction_waiters_separately_from_model_demand(self):
        store=JobStore(Path(self.endpoint['job_root']))
        for index,phase in enumerate(('waiting_cpu','cpu_scoring','waiting_predictions')):
            job,_=store.create('elucidate_feature',{'index':index},kind='pipeline')
            store.set_status(job,'running',phase=phase,scoring_job='score' if index<2 else None)
        with patch('msms_structure_elucidation.hostprobe.probe',return_value={}):
            result=workers.handle('worker-probe',{'model':'glacier'},{**self.endpoint,'feature_slots':2})
        self.assertEqual(result['active_features'],3)
        self.assertEqual(result['stage_backlog'],{'execution_pending':1,'postprediction_pending':2,'delivery_pending':0})
        self.assertEqual(result['postprediction_slots'],4)

    def test_final_inventory_excludes_native_files_and_cleanup_preserves_cache(self):
        _,response=self.submit();store=JobStore(self.root/'worker');job=response['job_id'];folder=store.path(job)
        (folder/'retrieval').mkdir();(folder/'retrieval/report.html').write_text('report')
        (folder/'retrieval/preds.hdf5').write_bytes(b'stay on worker')
        (folder/'retrieval/model_shards').mkdir()
        (folder/'retrieval/model_shards/predictions.json').write_text('all raw candidate spectra')
        cache=self.root/'worker/prediction-cache';cache.mkdir();(cache/'shared.sqlite3').write_bytes(b'reuse')
        write_json(folder/'result.json',{'scored_structures':2});store.set_status(job,'done')
        inventory=workers.result_inventory(store,job)
        self.assertIn('retrieval/report.html',inventory)
        self.assertNotIn('retrieval/preds.hdf5',inventory)
        self.assertNotIn('retrieval/model_shards/predictions.json',inventory)
        self.assertFalse(any('prediction-cache' in name for name in inventory))
        request={'remote_job_id':job,'acknowledged_inventory':inventory}
        workers.handle('worker-cleanup',request,self.endpoint)
        self.assertTrue((cache/'shared.sqlite3').is_file())
        self.assertFalse(folder.exists())
        self.assertTrue(workers.handle('worker-cleanup',request,self.endpoint)['cleaned'])

    def test_input_paths_cannot_escape_the_worker_request(self):
        payload,_=self.submit();payload['inputs']={'../outside':'bad'}
        with self.assertRaises(ValueError):workers.handle('worker-submit',payload,self.endpoint)


if __name__=='__main__':unittest.main()
