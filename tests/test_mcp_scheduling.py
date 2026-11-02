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
from msms_structure_elucidation.mcp.admission import Admission
from msms_structure_elucidation.mcp.jobs import JobStore, write_json


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
