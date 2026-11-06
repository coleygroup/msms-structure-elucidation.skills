"""Regression checks for FIFO, bounded resident jobs, detached dispatch and route holds."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp import backends, pipeline
from msms_structure_elucidation.mcp.admission import Admission, request_window
from msms_structure_elucidation.mcp.jobs import JobStore, write_json


class BackendIsolationTests(unittest.TestCase):
    def test_automatic_cpu_budget_resolves_explicit_stages_on_each_backend(self):
        from msms_structure_elucidation.mcp.cpu_pool import stage_defaults
        gpu_info = [{'index': i} for i in range(8)]
        with tempfile.TemporaryDirectory() as tmp, patch.object(backends.hostprobe, 'gpus', return_value=gpu_info):
            for name in ('local', 'slurm', 'kubernetes'):
                with self.subTest(backend=name):
                    config = {'mcp': {'backend': name, 'max_concurrent_jobs': 8, 'feature_pipeline': True,
                        'feature_cpu_workers': 1, **stage_defaults(8), 'kubernetes': {'shared_workspace': True}}}
                    backend = backends.make_backend(name, JobStore(Path(tmp)/name), config)
                    self.assertGreaterEqual(backend.core.max_live_pipelines, 28)
                    if name == 'local':
                        self.assertEqual(backend.core.max_cpu_jobs, 28)
                        fields = backend.describe()['feature_pipeline']
                        self.assertEqual((fields['preparation_workers'], fields['scoring_workers'],
                                          fields['cpu_workers']), (16,4,20))
                    backend.close()

    def test_single_backend_does_not_import_fleet_or_kubernetes(self):
        unavailable = {f'msms_structure_elucidation.mcp.{name}': None
                       for name in ('fleet', 'kubernetes')}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, unavailable), \
                patch.object(backends.hostprobe, 'gpus', return_value=[]):
            for name in ('local', 'slurm'):
                config = {'mcp': {'backend': name, 'feature_pipeline': False}}
                store = JobStore(Path(tmp) / name)
                backend = backends.make_backend(name, store, config)
                self.assertEqual(backend.name, name)
                self.assertFalse(hasattr(backend, 'network'))

    def test_local_feature_pipeline_needs_no_pool_adapter(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                'msms_structure_elucidation.mcp.fleet': None,
                'msms_structure_elucidation.mcp.kubernetes': None}), \
                patch.object(backends.hostprobe, 'gpus', return_value=[]):
            config = {'mcp': {'backend': 'local', 'feature_pipeline': True}}
            backend = backends.make_backend('local', JobStore(Path(tmp)), config)
            self.assertEqual(backend.core.name, 'local')
            backend.close()


class RequestWindowTests(unittest.TestCase):
    def test_completed_delivery_does_not_occupy_execution_capacity(self):
        rows = [{'state':'done', 'remote_cleanup_pending':True} for _ in range(600)]
        rows += [{'state':'running', 'phase':'cpu_scoring'} for _ in range(300)]
        window = request_window(rows, 1024, 2048)
        self.assertEqual(window['execution_pending'], 0)
        self.assertEqual(window['postprediction_pending'], 300)
        self.assertEqual(window['delivery_pending'], 600)
        self.assertEqual(window['admission_slots'], 1024)
        self.assertIsNone(window['hold'])

    def test_delivery_has_its_own_bounded_backlog(self):
        window = request_window([{'state':'done'}] * 4, 8, 4)
        self.assertEqual(window['admission_slots'], 0)
        self.assertEqual(window['hold'], 'delivery_backlog_limit')

    def test_scoring_waiters_and_output_work_have_their_own_budget(self):
        rows = [{'state':'running','phase':'waiting_cpu','worker_status':{'scoring_job':'score-1'}}] * 600
        rows += [{'state':'running','phase':'waiting_output'}] * 40
        rows += [{'state':'running','phase':'waiting_cpu'}] * 100
        window = request_window(rows,1024,2048)
        self.assertEqual(window['execution_pending'],100)
        self.assertEqual(window['postprediction_pending'],640)
        self.assertEqual(window['admission_slots'],924)
        held = request_window(rows,1024,2048,postprediction_limit=640)
        self.assertEqual(held['admission_slots'],0)
        self.assertEqual(held['hold'],'postprediction_backlog_limit')

    def test_confirmation_and_verification_remain_execution_requirements(self):
        rows = [{'state':'done', 'awaiting_scheduler_confirmation':True},
                {'state':'running', 'phase':'result_collection', 'worker_status':{'state':'done'}},
                {'state':'running', 'phase':'result_collection', 'worker_status':None},
                {'state':'failed'}, {'state':'done', 'published':True}]
        window = request_window(rows, 3, 8)
        self.assertEqual(window['execution_pending'], 2)
        self.assertEqual(window['delivery_pending'], 2)
        self.assertEqual(window['admission_slots'], 1)
        with self.assertRaises(ValueError):
            request_window([], 0, 8)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JobStore(Path(self.tmp.name) / 'jobs')
        self.config = {'mcp': {'max_cpu_jobs': 1, 'max_prepared_jobs': 1, 'min_free_memory_gb': 0}}
        with patch.object(backends.hostprobe, 'gpus', return_value=[]):
            self.backend = backends.LocalBackend(self.store, self.config)
        self.launched = []
        def launch(job, devices):
            self.launched.append(job)
            self.store.set_launch(job, backend='local', host=socket.gethostname(), pid=os.getpid(), devices='')
            self.store.set_status(job, 'running')
            self.store.set_phase(job, 'cpu_preparation')
        self.backend._launch = launch

    def parent(self, n):
        return self.store.create('score_candidates', {'feature': n}, kind='pipeline')[0]

    def test_raw_local_scheduler_never_launches_cpu_service_children(self):
        for task in ('publish_prediction_group','score_prepared_feature'):
            self.store.create(task,{},kind='cpu')
        self.backend.pump()
        self.assertEqual(self.launched,[])

    def waiting(self, n, since):
        jid = self.parent(n)
        self.backend._launch(jid, '')
        self.store.set_phase(jid, 'waiting_cpu')
        status = self.store.status(jid)
        write_json(self.store.path(jid) / 'status.json', {**status, 'cpu_ready_at': since,
                   'updated': f'2026-11-02T09:00:0{n}'})
        return jid

    def test_oldest_waiter_gets_slot_even_when_new_waiter_has_latest_update(self):
        jobs = [self.waiting(i, i + 1) for i in range(3)]
        self.backend.pump()
        self.assertEqual([self.store.status(j)['phase'] for j in jobs],
                         ['cpu_scoring', 'waiting_cpu', 'waiting_cpu'])
        self.store.set_status(jobs[0], 'done')
        self.backend.pump()
        self.assertEqual(self.store.status(jobs[1])['phase'], 'cpu_scoring')

    def test_waiters_do_not_open_more_and_more_parent_processes(self):
        a, b, c = [self.parent(i) for i in range(3)]
        self.backend.pump()
        first = self.launched[0]
        self.store.set_phase(first, 'waiting_cache')
        self.backend.pump()
        second = self.launched[1]
        self.store.set_phase(second, 'waiting_cpu')
        self.backend.pump()
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(sum(self.store.status(j)['state'] == 'queued' for j in (a, b, c)), 1)

    def test_worker_resume_is_passive(self):
        jid = self.waiting(0, 1)
        config = {'_mcp_dispatcher': {'host': socket.gethostname(), 'pid': os.getpid() + 100000},
                  'mcp': {'scheduler_process': True}}
        with patch.object(backends.hostprobe, 'gpus', return_value=[]):
            passive = backends.LocalBackend(self.store, config)
        passive.pump = lambda: self.fail('A waiting worker tried to drive the scheduler')
        thread = threading.Thread(target=pipeline.resume_cpu, args=(self.store, jid, config, passive))
        thread.start()
        time.sleep(.1)
        self.store.set_phase(jid, 'cpu_scoring')
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_scheduler_does_not_rescan_closed_history_or_scan_for_each_queued_job(self):
        for n in range(100):
            jid = self.parent(n)
            self.store.set_status(jid, 'done')
        self.store.active()  # one-time import of a legacy store
        for n in range(100, 140):
            self.parent(n)
        with patch.object(self.store, 'list', side_effect=AssertionError('closed history scanned')), \
             patch.object(self.store, 'active', wraps=self.store.active) as active:
            self.backend.pump()
        self.assertEqual(active.call_count, 1)
        self.assertEqual(len(self.launched), 1)

    def test_resource_scheduler_skips_virtual_files_but_keeps_owned_legacy_jobs(self):
        self.backend.exclude_tasks.add('elucidate_feature')
        virtual=[]
        for index in range(40):
            job,_=self.store.create('elucidate_feature',{'index':index},kind='pipeline')
            self.store.set_launch(job,backend='feature-pipeline');virtual.append(job)
        legacy,_=self.store.create('elucidate_feature',{'index':'legacy'},kind='pipeline')
        self.backend._launch(legacy,'')
        self.backend.pump()
        self.store.set_phase(legacy,'waiting_cpu')
        original=self.store.request
        def request(job):
            self.assertNotIn(job,virtual,'Virtual requests must be filtered before their files are read')
            return original(job)
        with patch.object(self.store,'request',side_effect=request):
            self.backend.pump()
        self.assertEqual(self.store.status(legacy)['phase'],'cpu_scoring')
        self.assertEqual([row['job_id'] for row in self.backend.queue_snapshot],[legacy])

    def test_mixed_legacy_timestamps_can_be_listed(self):
        a, b = self.parent(1), self.parent(2)
        write_json(self.store.path(a) / 'status.json', {'state': 'queued', 'updated': 1793632000.0})
        write_json(self.store.path(b) / 'status.json', {'state': 'queued', 'updated': '2026-11-02T10:00:00'})
        self.assertEqual(len(self.store.list()), 2)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JobStore(Path(self.tmp.name))
        self.admission = Admission(self.store, {})

    def create(self, n):
        return self.store.create('search_pubchem_formulas', {'n': n, 'model': 'glacier'}, kind='pipeline')[0]

    def test_old_generation_completion_does_not_verify_the_new_configuration(self):
        old, _ = self.store.create('search_pubchem_formulas', {'n': 1}, kind='pipeline',
                                   config={'_config_digest': 'old-environment'})
        new, _ = self.store.create('search_pubchem_formulas', {'n': 2}, kind='pipeline',
                                   config={'_config_digest': 'new-environment'})
        self.store.set_status(old, 'done')
        admission = Admission(self.store, {'_config_digest': 'new-environment'})
        admission.record(old, 'done')
        self.assertTrue(admission.admit(new))
        scope = admission.scope(self.store.request(new), new)
        self.assertNotIn('verified', admission.status()[scope])
        self.assertEqual(admission.status()[scope]['canary'], new)

    def test_one_real_canary_precedes_bulk_admission(self):
        a, b = self.create(1), self.create(2)
        self.assertTrue(self.admission.admit(a))
        self.assertFalse(self.admission.admit(b))
        self.store.set_status(a, 'done')
        self.admission.record(a, 'done')
        self.assertTrue(self.admission.admit(b))

    def test_first_canary_tls_error_holds_all_new_requests_until_explicit_resume(self):
        a, b = self.create(1), self.create(2)
        self.admission.admit(a)
        self.store.set_status(a, 'failed')
        self.admission.record(a, 'failed', 'SSL: CERTIFICATE_VERIFY_FAILED')
        reconnected = Admission(self.store, {})
        self.assertFalse(reconnected.admit(b))
        scope = reconnected.scope(self.store.request(a))
        self.assertEqual(reconnected.status()[scope]['hold']['signature'], 'tls_certificate')
        reconnected.resume(scope, 'Verified the configured CA store in the CPU environment')
        self.assertTrue(reconnected.admit(b))

    def test_runner_result_does_not_open_route_before_scheduler_confirmation(self):
        a, b = self.create(1), self.create(2)
        self.admission.admit(a)
        self.store.set_launch(a, backend='slurm', slurm_job_id='12345')
        self.store.set_status(a, 'done')
        self.admission.record(a, 'done')
        self.assertIn(a, [r['job_id'] for r in self.store.active()])
        self.assertFalse(self.admission.admit(b))
        self.store.set_status(a, 'done', scheduler_confirmed=True)
        self.admission.record(a, 'done')
        self.assertNotIn(a, [r['job_id'] for r in self.store.active()])
        self.assertTrue(self.admission.admit(b))

    def test_duplicate_failure_record_does_not_count_twice_and_input_errors_do_not_hold(self):
        admission = Admission(self.store, {'mcp': {'route_canary': False, 'environment_failure_limit': 2}})
        a, b, c = [self.create(i) for i in range(3)]
        admission.record(a, 'failed', 'No module named pubchempy')
        admission.record(a, 'failed', 'No module named pubchempy')
        self.assertTrue(admission.admit(b))
        admission.record(b, 'failed', 'invalid spectrum')
        self.assertTrue(admission.admit(c))
        admission.record(c, 'failed', 'No module named pubchempy')
        self.assertFalse(admission.admit(self.create(4)))

    def test_storage_exhaustion_holds_a_route_until_repair_is_recorded(self):
        for index,message in enumerate(('Disk quota exceeded', '[Errno 28] No space left on device')):
            with self.subTest(message=message):
                admission = Admission(self.store, {'_config_digest': str(index)})
                failed,_ = self.store.create('search_pubchem_formulas', {'storage_case': index}, kind='pipeline',
                                            config={'_config_digest': str(index)})
                waiting,_ = self.store.create('search_pubchem_formulas', {'storage_case': index+10}, kind='pipeline',
                                             config={'_config_digest': str(index)})
                self.assertTrue(admission.admit(failed))
                admission.record(failed, 'failed', message)
                self.assertFalse(admission.admit(waiting))
                scope = admission.scope(self.store.request(failed), failed)
                self.assertEqual(admission.status()[scope]['hold']['signature'], 'storage_capacity')
                admission.resume(scope, 'Verified writable storage after cleanup')
                self.assertTrue(admission.admit(waiting))

    def test_bulk_slurm_submission_stops_at_failed_production_canary(self):
        config = {'mcp': {'backend': 'slurm'}}
        backend = backends.SlurmBackend(self.store, config)
        jobs = [self.create(n) for n in range(132)]
        with patch.object(backends.subprocess, 'run', return_value=type('Run', (), {
                'returncode': 0, 'stdout': '12345\n', 'stderr': ''})()) as submit:
            for job in jobs:
                backend.submit(job)
            self.assertEqual(submit.call_count, 1)
            self.store.set_status(jobs[0], 'failed')
            backend.admission.record(jobs[0], 'failed', 'CERTIFICATE_VERIFY_FAILED')
            for job in jobs[1:]:
                backend.submit(job)
            self.assertEqual(submit.call_count, 1)
        self.assertTrue(all(self.store.status(job)['state'] == 'queued' for job in jobs[1:]))


class PubChemTrustTests(unittest.TestCase):
    def test_certificate_failure_is_not_retried_with_the_same_trust_store(self):
        import ssl
        import urllib.error
        from msms_structure_elucidation import pubchem
        error = urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))
        with patch.object(pubchem.urllib.request, 'urlopen', side_effect=error) as request, \
             patch.object(pubchem, '_rate_limit'):
            with self.assertRaises(urllib.error.URLError):
                pubchem._get('compound/name/ethanol/cids/JSON')
        self.assertEqual(request.call_count, 1)


class BackendBoundaryTests(unittest.TestCase):
    def test_cpu_slurm_overrides_include_setup_arguments_and_requeue(self):
        options, script = backends.sbatch_script({'gpus': 1, 'extra_args': ['--gpu-option'],
            'setup': ['GPU_SETUP'], 'requeue': False, 'cpu': {'setup': ['CPU_SETUP'],
            'extra_args': ['--cpu-option'], 'requeue': True}}, 'pipeline', ['python', 'runner'])
        self.assertIn('--cpu-option', options)
        self.assertNotIn('--gpu-option', options)
        self.assertIn('--requeue', options)
        self.assertFalse(any(o.startswith('--gpus') for o in options))
        self.assertIn('CPU_SETUP', script)
        self.assertNotIn('GPU_SETUP', script)

    def test_reconnecting_threads_submit_a_slurm_request_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job, _ = store.create('model_inference', {}, kind='gpu')
            backend = backends.SlurmBackend(store, {'mcp': {}})
            def sbatch(*args, **kwargs):
                time.sleep(.05)
                return type('Run', (), {'returncode': 0, 'stdout': '12345\n', 'stderr': ''})()
            with patch.object(backends.subprocess, 'run', side_effect=sbatch) as submit, ThreadPoolExecutor(2) as pool:
                list(pool.map(backend.submit, [job, job]))
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(store.launch(job)['slurm_job_id'], '12345')

    def test_kubernetes_inventory_excludes_large_annotations_and_pod_environments(self):
        from msms_structure_elucidation.mcp.kubernetes import inventory_summary
        nodes = {'items': [{'metadata': {'labels': {'nvidia.com/gpu.product': 'fixture-GPU'},
            'annotations': {'large': 'x' * 100000}}, 'status': {'allocatable': {'nvidia.com/gpu': '4'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
        compact = inventory_summary('nodes', nodes)
        self.assertEqual(compact['allocatable_by_product'], {'fixture-GPU': 4})
        self.assertLess(len(json.dumps(compact)), 1000)
        pods = {'items': [{'status': {'phase': 'Running'}, 'spec': {'containers': [{
            'env': [{'name': 'PRIVATE_TEST_VALUE', 'value': 'omit-me'}],
            'resources': {'requests': {'nvidia.com/gpu': '1'}}}]}}]}
        compact = inventory_summary('pods', pods)
        self.assertEqual(compact['gpu_requests'], 1)
        self.assertNotIn('omit-me', json.dumps(compact))


class DetachedSchedulerTests(unittest.TestCase):
    def test_local_queue_continues_after_submitting_client_exits(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'jobs'
            config = {'mcp': {'backend': 'local', 'scheduler_process': True,
                             'max_cpu_jobs': 2, 'max_live_pipelines': 3, 'min_free_memory_gb': 0}}
            script = ('import json,pathlib,sys\n'
                      'from msms_structure_elucidation.mcp.jobs import JobStore\n'
                      'from msms_structure_elucidation.mcp.backends import LocalBackend\n'
                      f'store=JobStore(pathlib.Path({str(root)!r}))\n'
                      f'config=json.loads({json.dumps(config)!r})\n'
                      'jobs=[store.create("missing_fixture_task", {"n":i},kind="pipeline",config=config)[0] for i in range(8)]\n'
                      'LocalBackend(store,config,sys.executable).submit(jobs[0])\n')
            done = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr)
            store = JobStore(root)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and len([r for r in store.list(20) if r['state'] == 'failed']) < 8:
                    time.sleep(.2)
                rows = store.list(20)
                self.assertEqual(len(rows), 8)
                self.assertTrue(all(r['state'] == 'failed' for r in rows), rows)
                receipt = json.loads((root / 'scheduler-service.json').read_text())
                self.assertEqual(receipt['state'], 'running')
                self.assertNotEqual(receipt['pid'], os.getpid())
            finally:
                write_json(root / 'scheduler-stop.json', {'reason': 'test complete'})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if json.loads((root / 'scheduler-service.json').read_text()).get('state') == 'stopped':
                        break
                    time.sleep(.1)


if __name__ == '__main__':
    unittest.main()
