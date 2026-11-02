"""Distributed ownership, transfer validation and Kubernetes lifecycle contracts."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation.mcp import backends, endpoint, fleet, kubernetes, pipeline
from msms_structure_elucidation.mcp.jobs import JobStore, write_json
from msms_structure_elucidation.prediction_cache import source_fingerprint


class CollectionTests(unittest.TestCase):
    def test_streamed_collection_verifies_each_file_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'native_outputs').mkdir(parents=True)
            (source / 'native_outputs/preds.hdf5').write_bytes(b'spectra')
            inventory = endpoint.inventory(source)
            class Remote:
                def call(self, operation, payload, output):
                    with tarfile.open(output, 'w') as archive:
                        archive.add(source / 'native_outputs/preds.hdf5', arcname='native_outputs/preds.hdf5')
            fleet.collect(Remote(), 'remote', inventory, root / 'collected')
            self.assertEqual((root / 'collected/native_outputs/preds.hdf5').read_bytes(), b'spectra')
            self.assertEqual(json.loads((root / 'collected/verified-inventory.json').read_text()), inventory)

    def test_corruption_missing_files_and_path_traversal_are_not_published(self):
        for name, contents, expected in [('native_outputs/preds.hdf5', b'bad', b'good'),
                                          ('../escaped', b'good', b'good'),
                                          ('different', b'good', b'good')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                class Remote:
                    def call(self, operation, payload, output):
                        with tarfile.open(output, 'w') as archive:
                            member = tarfile.TarInfo(name)
                            member.size = len(contents)
                            archive.addfile(member, io.BytesIO(contents))
                inventory = {'native_outputs/preds.hdf5': {'size': len(expected),
                              'sha256': hashlib.sha256(expected).hexdigest()}}
                with self.assertRaises(ValueError):
                    fleet.collect(Remote(), 'remote', inventory, Path(tmp) / 'collected')
                self.assertFalse((Path(tmp) / 'collected').exists())


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / 'coordinator')
        config = {'mcp': {'backend': 'pool', 'min_free_memory_gb': 0, 'servers': {
            'busy': {'transport': 'local', 'job_root': str(self.root / 'busy')},
            'free': {'transport': 'local', 'job_root': str(self.root / 'free')}}}}
        with patch.object(backends.hostprobe, 'gpus', return_value=[]):
            self.backend = fleet.FleetBackend(self.store, config)
        self.backend.cpu.pump = lambda: None
        self.calls = []
        def call(remote, operation, payload=None, output=None):
            self.calls.append((remote.name, operation))
            if operation == 'probe':
                return {'free_gpus': [0] if remote.name == 'free' else [], 'active_jobs': 0}
            if operation == 'submit':
                return {'job_id': 'model_inference-' + 'a' * 16, 'status': {'state': 'submitted'}}
            if operation == 'poll':
                return {'status': {'state': 'running'}}
        self.remote = patch.object(fleet.Endpoint, 'call', new=call)
        self.remote.start()
        self.addCleanup(self.remote.stop)

    def child(self, name='feature'):
        return self.store.create('model_inference', {'arguments': {'model': 'glacier'}, 'feature': name}, kind='gpu')[0]

    def test_idle_endpoint_is_selected_and_assignment_is_not_duplicated(self):
        job = self.child()
        self.backend.pump()
        self.assertEqual(self.store.launch(job)['server'], 'free')
        self.backend.pump()
        self.assertEqual(self.calls.count(('free', 'submit')), 1)
        self.assertEqual(self.calls.count(('busy', 'submit')), 0)

    def test_endpoint_limit_is_respected_for_queued_group(self):
        first, second = self.child('first'), self.child('second')
        self.backend.pump()
        self.assertEqual(sorted([self.store.status(first)['state'], self.store.status(second)['state']]),
                         ['queued', 'submitted'])

    def test_lost_submission_reply_retains_ownership_and_retries_same_endpoint(self):
        job = self.child()
        calls = []
        def call(remote, operation, payload=None, output=None):
            if operation == 'probe':
                return {'free_gpus': [0]}
            if operation == 'submit':
                calls.append(remote.name)
                if len(calls) == 1:
                    raise RuntimeError('SSH disconnected after submit')
                return {'job_id': 'model_inference-' + 'a' * 16}
            return {'status': {'state': 'running'}}
        with patch.object(fleet.Endpoint, 'call', new=call):
            self.backend.pump()
            owner = self.store.launch(job)['server']
            self.backend.pump()
        self.assertEqual(calls, [owner, owner])
        self.assertEqual(self.store.status(job)['state'], 'running')

    def test_collection_failure_never_requests_remote_cleanup(self):
        job = self.child()
        self.backend.pump()
        operations = []
        def call(remote, operation, payload=None, output=None):
            operations.append(operation)
            return {'status': {'state': 'done'}, 'inventory': {'some-file': {}}}
        with patch.object(fleet.Endpoint, 'call', new=call), \
             patch.object(fleet, 'collect', side_effect=RuntimeError('connection lost during transfer')):
            self.backend.pump()
        self.assertNotIn('cleanup', operations)
        self.assertNotEqual(self.store.status(job)['state'], 'done')
        self.assertIn('transfer', self.store.status(job)['transport_error'])

    def test_cancel_routes_to_owner(self):
        job = self.child()
        self.backend.pump()
        self.backend.cancel(job)
        self.assertIn(('free', 'cancel'), self.calls)
        self.assertEqual(self.store.status(job)['state'], 'cancelled')

    def test_cpu_dispatch_cannot_launch_remote_inference_locally(self):
        self.assertIn('model_inference', self.backend.cpu.exclude_tasks)

    def test_slurm_cpu_worker_never_uses_endpoint_credentials(self):
        self.backend.config['_mcp_dispatcher'] = {'host': 'coordinator-host', 'pid': -1}
        job = self.child()
        self.backend.submit(job)
        self.backend.pump()
        self.assertEqual(self.backend.poll(job)['state'], 'queued')
        self.assertEqual(self.backend.capacity(), 2)
        self.backend.cancel(job)
        self.assertTrue(self.store.status(job)['cancel_requested'])
        self.assertEqual(self.calls, [])

    def test_broker_honors_cancellation_before_dispatch(self):
        job = self.child()
        self.store.set_status(job, 'queued', cancel_requested=True)
        self.backend.pump()
        self.assertEqual(self.store.status(job)['state'], 'cancelled')
        self.assertNotIn(('free', 'submit'), self.calls)


class EndpointTests(unittest.TestCase):
    def test_real_agent_submission_reconnect_collection_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / 'native/src/ms_pred'
            (native / 'iceberg').mkdir(parents=True)
            (native / '__init__.py').write_text('')
            (native / 'iceberg/__init__.py').write_text('')
            calls = root / 'inference-calls'
            (native / 'iceberg/iceberg_elucidation.py').write_text(
                'from pathlib import Path\nimport tempfile\n'
                'def iceberg_prediction(**kwargs):\n'
                f'    with Path({str(calls)!r}).open("a") as stream: stream.write("call\\n")\n'
                f'    folder=Path(tempfile.mkdtemp(dir={str(root)!r}))\n'
                '    (folder/"preds.hdf5").write_bytes(b"test spectra")\n'
                '    (folder/"iceberg_run_successful").touch()\n'
                '    return folder,0\n')
            weight = root / 'weights'
            weight.write_bytes(b'approved')
            settings = {'transport': 'local', 'python': sys.executable, 'job_root': str(root / 'remote'),
                        'backend': 'local', 'mcp': {'max_concurrent_jobs': 1}}
            config = {'mcp': {'backend': 'pool', 'servers': {'test': settings}}}
            store = JobStore(root / 'client')
            with patch.object(backends.hostprobe, 'gpus', return_value=[]):
                backend = fleet.FleetBackend(store, config)
            arguments = dict(smiles=['CCO'], energies=[20], nce=False, adduct='[M+H]+', model='iceberg',
                checkpoint=None, gen_checkpoint=str(weight), inten_checkpoint=str(weight), instrument='Orbitrap',
                cuda_devices=None, batch_size=128, num_cpu_workers=4, num_gpu_workers=2,
                force_recompute=True, num_writers=2, num_h5_chunks=4, feat_cache_size=256, sparse_k=100)
            params = {'arguments': arguments, 'python': sys.executable, 'ms_pred_dir': str(root / 'native'),
                      'weights': {str(weight): hashlib.sha256(b'approved').hexdigest()},
                      'native_source': source_fingerprint(native), 'prepared': None}
            job, _ = store.create('model_inference', params, kind='gpu', config=config)
            remote = backend.endpoints['test']
            # Simulate a dropped response after the real detached job was submitted.
            original = remote.call
            submitted = []
            def call(operation, payload=None, output=None):
                result = original(operation, payload, output)
                if operation == 'submit':
                    submitted.append(result['job_id'])
                    if len(submitted) == 1:
                        raise RuntimeError('connection dropped after successful submission')
                return result
            with patch.object(backend, '_probe', return_value={'free_gpus': [0]}), \
                 patch.object(remote, 'call', side_effect=call):
                backend.submit(job)
                deadline = time.monotonic() + 30
                while backend.poll(job)['state'] in ('queued', 'submitted', 'running') and time.monotonic() < deadline:
                    time.sleep(.05)
            self.assertEqual(store.status(job)['state'], 'done', store.status(job))
            self.assertEqual(submitted[0], submitted[1])
            self.assertEqual(calls.read_text().splitlines(), ['call'])
            self.assertEqual((Path(store.result(job)['save_dir']) / 'preds.hdf5').read_bytes(), b'test spectra')
            self.assertFalse((root / 'remote' / submitted[0]).exists())
            self.assertIsNone(store.status(job)['remote_cleanup_error'])

    def test_paths_and_weights_can_differ_only_when_content_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'native/src/ms_pred'
            source.mkdir(parents=True)
            (source / '__init__.py').write_text('')
            weight = root / 'new.ckpt'
            weight.write_bytes(b'approved')
            params = {'arguments': {'model': 'glacier', 'checkpoint': '/old.ckpt', 'sparse_k': 100,
                       'batch_size': 1, 'num_cpu_workers': 1, 'num_writers': 1, 'num_h5_chunks': 1,
                       'feat_cache_size': 1, 'num_gpu_workers': 1},
                      'weights': {'/old.ckpt': hashlib.sha256(b'approved').hexdigest()},
                      'ms_pred_dir': str(root / 'native'), 'python': sys.executable,
                      'native_source': source_fingerprint(source)}
            settings = {'simulator': {'checkpoint': str(weight), 'glacier': {'batch_size': 256}}}
            mapped = endpoint.remap(params, settings, root, {'entries': [], 'options': {}})
            self.assertEqual(mapped['arguments']['batch_size'], 256)
            self.assertEqual(mapped['arguments']['checkpoint'], str(weight))
            self.assertEqual(json.loads((root / 'prepared.json').read_text())['options']['checkpoint'], str(weight))
            weight.write_bytes(b'other')
            with self.assertRaisesRegex(ValueError, 'approved weights'):
                endpoint.remap(params, settings, root, None)

    def test_cleanup_requires_exact_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job, _ = store.create('model_inference', {})
            store.set_status(job, 'done')
            write_json(store.path(job) / 'collection.json', {'native_outputs/preds.hdf5': {'sha256': 'abc'}})
            with patch.object(backends.hostprobe, 'gpus', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'acknowledgement'):
                    endpoint.handle('cleanup', {'endpoint': {'job_root': tmp}, 'remote_job_id': job,
                                               'acknowledged_inventory': {}})
            self.assertTrue(store.path(job).exists())

    def test_ssh_and_kubernetes_commands_use_explicit_targets_and_no_shell_interpolation(self):
        remote = fleet.Endpoint('x', {'job_root': '/jobs', 'host': 'example', 'repo': '/repo with spaces',
                                     'python': '/venv/bin/python'})
        self.assertIn('BatchMode=yes', remote.command('probe'))
        self.assertIn("cd '/repo with spaces'", remote.command('probe')[-1])
        remote = fleet.Endpoint('k', {'transport': 'kubernetes', 'job_root': '/jobs',
            'gateway': {'context': 'chosen', 'namespace': 'project', 'pod': 'gateway'},
            'mcp': {'kubernetes': {'context': 'chosen', 'namespace': 'project'}}})
        self.assertEqual(remote.command('probe')[:6], ['kubectl', '--context', 'chosen', '-n', 'project', 'exec'])


class KubernetesTests(unittest.TestCase):
    def test_cpu_pods_publish_gpu_requests_without_api_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job, _ = store.create('model_inference', {})
            backend = kubernetes.KubernetesBackend(store, {'_mcp_dispatcher': {'host': 'coordinator', 'pid': -1},
                'mcp': {'kubernetes': {'shared_workspace': True}}})
            with patch.object(kubernetes, 'kubectl', side_effect=AssertionError('CPU pod used Kubernetes credentials')):
                backend.submit(job)
                backend.pump()
                self.assertEqual(backend.poll(job)['state'], 'queued')
                self.assertTrue(backend.cancel(job)['cancel_requested'])
                store.set_status(job, 'done')
                self.assertEqual(backend.poll(job)['state'], 'running')
    def test_manifest_has_finite_deadline_and_cpu_jobs_request_no_gpu(self):
        settings = {'image': 'example/runtime@sha256:abc', 'gpu_resource': 'vendor.example/gpu',
                    'affinity': {'nodeAffinity': {}}, 'resources': {'requests': {'cpu': '4'}, 'limits': {'cpu': '4'}}}
        manifest = kubernetes.job_manifest(settings, 'job', ['python', 'task'])
        self.assertEqual(manifest['spec']['backoffLimit'], 0)
        self.assertGreater(manifest['spec']['activeDeadlineSeconds'], 0)
        self.assertNotIn('ttlSecondsAfterFinished', manifest['spec'])  # collection owns deletion
        self.assertEqual(manifest['spec']['template']['spec']['containers'][0]['resources']['limits']['vendor.example/gpu'], 1)
        cpu = kubernetes.job_manifest(settings, 'cpu', ['python', 'task'], 'cpu')
        self.assertNotIn('vendor.example/gpu', cpu['spec']['template']['spec']['containers'][0]['resources']['limits'])
        self.assertEqual(settings['resources']['limits'], {'cpu': '4'})

    def test_context_and_namespace_are_required(self):
        with self.assertRaisesRegex(ValueError, 'context'):
            kubernetes.kubectl({}, 'get', 'jobs')

    def test_runner_result_is_not_completion_until_job_and_pod_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job, _ = store.create('model_inference', {})
            store.set_launch(job, kubernetes_job='job')
            store.set_status(job, 'done')
            backend = kubernetes.KubernetesBackend(store, {'mcp': {'kubernetes': {'shared_workspace': True}}})
            pending = subprocess.CompletedProcess([], 0, json.dumps({'status': {'active': 1}}), '')
            with patch.object(kubernetes, 'kubectl', return_value=pending):
                self.assertEqual(backend.poll(job)['state'], 'running')
            complete = subprocess.CompletedProcess([], 0, json.dumps({'status': {'conditions': [
                {'type': 'Complete', 'status': 'True'}]}}), '')
            pods = {'pods': {'items': [{'status': {'phase': 'Succeeded', 'containerStatuses': [
                {'state': {'terminated': {'exitCode': 0}}}]}}]}}
            with patch.object(kubernetes, 'kubectl', return_value=complete), patch.object(backend, 'archive', return_value=pods):
                self.assertTrue(backend.poll(job)['scheduler_confirmed'])
            with patch.object(kubernetes, 'kubectl', side_effect=AssertionError('terminal receipt was queried again')):
                self.assertEqual(backend.poll(job)['state'], 'done')


class GroupingTests(unittest.TestCase):
    def test_measured_rate_targets_duration_and_gpu_batch_is_independent(self):
        config = {'mcp': {'prediction_group_size': 'auto', 'target_prediction_seconds': 600,
                           'max_group_candidates': 100000},
                  'models': {'simulator': {'batch_size': 128, 'spectra_per_second': 500}}}
        with patch('msms_structure_elucidation.hostprobe.ram_gb', return_value=(128., 128.)):
            self.assertEqual(pipeline.group_size(config, 'iceberg', 5), 60000)
            self.assertEqual(pipeline.group_size({}, 'glacier'), 4096)
        config['mcp']['prediction_group_size'] = 512
        self.assertEqual(pipeline.group_size(config, 'iceberg'), 512)

    def test_invalid_group_size_is_rejected(self):
        with self.assertRaises(ValueError):
            pipeline.group_size({'mcp': {'prediction_group_size': 0}}, 'iceberg')

    def test_auto_group_is_bounded_by_available_memory(self):
        with patch('msms_structure_elucidation.hostprobe.ram_gb', return_value=(2., .5)):
            self.assertLess(pipeline.group_size({}, 'glacier', 5), 1000)

    def test_automatic_pool_capacity_uses_observed_gpu_slots(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(backends.hostprobe, 'gpus', return_value=[]):
            backend = fleet.FleetBackend(JobStore(Path(tmp)), {'mcp': {'servers': {
                'local': {'transport': 'local', 'job_root': str(Path(tmp) / 'endpoint')}}}})
            with patch.object(backend, '_probe', return_value={'max_concurrent_jobs': 3}):
                self.assertEqual(backend.capacity(), 3)


if __name__ == '__main__':
    unittest.main()
