"""Resource isolation and CPU/GPU handoff tests without model weights or a CUDA device."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation import glacier
from msms_structure_elucidation.mcp import backends, pipeline, tasks
from msms_structure_elucidation.mcp.jobs import JobStore, write_json
from msms_structure_elucidation.prediction_cache import file_lock


class PipelineSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name))
        with patch.object(backends.hostprobe, 'gpus', return_value=[{'index': 0}]):
            self.backend = backends.LocalBackend(self.store, {'mcp': {'max_cpu_jobs': 1,
                'max_prepared_jobs': 1, 'min_free_memory_gb': 0}})
        self.launched = []
        def launch(job, devices):
            self.launched.append((job, devices))
            self.store.set_launch(job, backend='local', host=self.backend.host, pid=os.getpid(), devices=devices)
            self.store.set_status(job, 'running')
        self.backend._launch = launch
        self.gpus = patch.object(backends, 'free_gpus', return_value=[0])
        self.gpus.start()
        self.addCleanup(self.gpus.stop)

    def test_cpu_preparation_and_gpu_prediction_run_concurrently(self):
        cpu, _ = self.store.create('infer_formulas', {}, kind='cpu')
        gpu, _ = self.store.create('model_inference', {}, kind='gpu')
        self.backend.pump()
        self.assertIn((cpu, ''), self.launched)
        self.assertIn((gpu, '0'), self.launched)

    def test_waiting_parent_releases_cpu_and_gpu_but_ready_queue_bounds_new_preparation(self):
        parent, _ = self.store.create('score_candidates', {}, kind='pipeline')
        self.backend.pump()
        self.store.set_phase(parent, 'waiting_gpu')
        other, _ = self.store.create('infer_formulas', {}, kind='cpu')
        child, _ = self.store.create('model_inference', {'_parent_job': parent}, kind='gpu')
        self.backend.pump()
        self.assertNotIn((other, ''), self.launched)
        self.assertIn((child, '0'), self.launched)
        self.store.set_status(child, 'done')
        self.store.set_phase(parent, 'waiting_cpu')
        self.backend.pump()
        self.assertEqual(self.store.status(parent)['phase'], 'cpu_scoring')
        self.assertNotIn((other, ''), self.launched)
        self.store.set_status(parent, 'done')
        self.backend.pump()
        self.assertIn((other, ''), self.launched)

    def test_low_ram_delays_preparation_without_blocking_ready_gpu_work(self):
        self.backend.min_free_memory_gb = 4
        cpu, _ = self.store.create('infer_formulas', {}, kind='cpu')
        gpu, _ = self.store.create('model_inference', {}, kind='gpu')
        with patch.object(backends.hostprobe, 'ram_gb', return_value=(16., 1.)):
            self.backend.pump()
        self.assertNotIn((cpu, ''), self.launched)
        self.assertIn((gpu, '0'), self.launched)

    def test_finished_parent_prevents_orphan_gpu_launch(self):
        parent, _ = self.store.create('score_candidates', {}, kind='pipeline')
        self.store.set_status(parent, 'failed')
        child, _ = self.store.create('model_inference', {'_parent_job': parent}, kind='gpu')
        self.backend.pump()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.store.status(child)['state'], 'cancelled')

    def test_cache_waiter_cannot_block_owners_cpu_slot_for_publication(self):
        owner, _ = self.store.create('score_candidates', {'feature': 'owner'}, kind='pipeline')
        self.backend.pump()
        self.store.set_phase(owner, 'waiting_gpu')
        # Permit a second preparation while the owner waits for inference.
        self.backend.max_prepared_jobs = 2
        waiter, _ = self.store.create('score_candidates', {'feature': 'waiter'}, kind='pipeline')
        self.backend.pump()
        waiting, acquired = threading.Event(), threading.Event()
        errors = []
        def on_wait():
            self.store.set_phase(waiter, 'waiting_cache')
            waiting.set()
        def consume():
            try:
                with file_lock(self.store.root / 'prediction.lock', on_wait,
                        lambda: pipeline.resume_cpu(self.store, waiter, {}, self.backend)):
                    acquired.set()
            except Exception as exc:
                errors.append(exc)
        with file_lock(self.store.root / 'prediction.lock'):
            thread = threading.Thread(target=consume, daemon=True)
            thread.start()
            self.assertTrue(waiting.wait(2))
            self.store.set_phase(owner, 'waiting_cpu')
            self.backend.pump()
            self.assertEqual(self.store.status(owner)['phase'], 'cpu_scoring')
        self.store.set_status(owner, 'done')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())

    def test_cancel_propagates_to_queued_children_and_cannot_be_overwritten(self):
        parent, _ = self.store.create('score_candidates', {}, kind='pipeline')
        child, _ = self.store.create('model_inference', {'_parent_job': parent}, kind='gpu')
        self.backend.cancel(parent)
        self.store.set_status(parent, 'done')
        self.store.set_phase(parent, 'cpu_scoring')
        self.assertEqual(self.store.status(parent)['state'], 'cancelled')
        self.assertEqual(self.store.status(child)['state'], 'cancelled')


class StageBoundaryTests(unittest.TestCase):
    def test_large_request_consumes_separate_native_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root / 'jobs')
            config = {'mcp': {'prediction_group_size': 2, 'prepared_groups': 2}}
            parent, _ = store.create('score_candidates', {}, kind='pipeline', config=config)
            store.set_status(parent, 'running')
            store.set_phase(parent, 'cpu_preparation')
            class Backend:
                name = 'local'
                def submit(self, job):
                    folder = store.path(job) / 'native_outputs'
                    folder.mkdir()
                    (folder / 'preds.hdf5').write_text(json.dumps(store.request(job)['params']['smiles']))
                    write_json(store.path(job) / 'result.json', {'save_dir': str(folder)})
                    store.set_status(job, 'done')
                def poll(self, job):return store.status(job)
                def pump(self):pass
            def prepare(arguments, store, parent, config):
                return store.create('model_inference', {'smiles': arguments['smiles'], '_parent_job': parent}, kind='gpu')
            with patch.object(pipeline, 'context', return_value=(store, parent, config)), \
                 patch.object(pipeline, '_prepare_submission', side_effect=prepare), \
                 patch.object(pipeline, 'make_backend', return_value=Backend()), \
                 patch.object(pipeline, 'resume_cpu', side_effect=lambda *args: store.set_phase(parent, 'cpu_scoring')):
                folders = list(pipeline.run_groups({'model': 'iceberg', 'energies': [20],
                                                   'smiles': ['A', 'B', 'C', 'D', 'E']}))
            self.assertEqual([json.loads((folder / 'preds.hdf5').read_text()) for folder in folders],
                             [['A', 'B'], ['C', 'D'], ['E']])
            self.assertEqual(len(store.children(parent)), 3)
    def test_all_forward_prediction_routes_start_on_cpu(self):
        for task in ('predict_spectra', 'score_candidates', 'search_pubchem_mass',
                     'search_pubchem_formulas', 'retrieve_atlas'):
            self.assertEqual(tasks.KINDS[task], 'pipeline')
        self.assertEqual(tasks.KINDS['model_inference'], 'gpu')

    def test_real_detached_child_handoff_and_return_to_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / 'runtime' / 'src' / 'ms_pred'
            (fake / 'iceberg').mkdir(parents=True)
            (fake / '__init__.py').write_text('')
            (fake / 'iceberg' / '__init__.py').write_text('')
            (fake / 'iceberg' / 'iceberg_elucidation.py').write_text(
                'from pathlib import Path\nimport tempfile\n'
                'def iceberg_prediction(**kwargs):\n'
                '    folder=Path(tempfile.mkdtemp(dir=' + repr(str(root)) + '))\n'
                '    (folder/"preds.hdf5").write_bytes(b"test output")\n'
                '    (folder/"iceberg_run_successful").touch()\n'
                '    return folder, 47.0\n')
            package = types.ModuleType('ms_pred')
            package.__file__ = str(fake / '__init__.py')
            store = JobStore(root / 'jobs')
            parent, _ = store.create('score_candidates', {}, kind='pipeline')
            config = {'_mcp_python': sys.executable, '_config_path': str(root / 'config.yaml'),
                      'mcp': {'backend': 'local', 'max_cpu_jobs': 1, 'min_free_memory_gb': 0}}
            write_json(store.path(parent) / 'config.json', config)
            with patch.object(backends.hostprobe, 'gpus', return_value=[]):
                backend = backends.LocalBackend(store, config)
                store.set_launch(parent, backend='local', host=backend.host, pid=os.getpid(), devices='')
                store.set_status(parent, 'running')
                store.set_phase(parent, 'cpu_preparation')
                weights = root / 'weights'
                weights.touch()
                arguments = dict(smiles=['CCO'], energies=[20], nce=False, adduct='[M+H]+', model='iceberg',
                    checkpoint=None, gen_checkpoint=str(weights), inten_checkpoint=str(weights),
                    instrument='Orbitrap', cuda_devices=None, batch_size=128, num_cpu_workers=4,
                    num_gpu_workers=1, force_recompute=True, num_writers=2, num_h5_chunks=4,
                    feat_cache_size=256, sparse_k=100)
                with patch.dict(sys.modules, {'ms_pred': package}), \
                     patch.dict(os.environ, {pipeline.CONTEXT: str(store.path(parent)), 'CUDA_VISIBLE_DEVICES': ''}):
                    folder = pipeline.run_model(arguments)
            self.assertTrue((folder / 'preds.hdf5').is_file())
            child, = store.children(parent)
            self.assertEqual(store.status(child)['state'], 'done')
            self.assertEqual(store.status(parent)['phase'], 'cpu_scoring')
            self.assertIn('waiting_gpu', store.status(parent)['phase_seconds'])
            self.assertEqual(store.launch(parent)['devices'], '')
            self.assertEqual(store.request(child)['params']['arguments']['energies'], [20])

    def test_glacier_prepares_native_entries_before_running_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'candidates.tsv').write_text('smiles\nCCO\n')
            native = types.SimpleNamespace(get_args=lambda: types.SimpleNamespace(checkpoint='weights'),
                _prepare_entries_parallel=lambda rows, workers: [('CCO', 'candidate_0', 20, '[M+H]+', 'Orbitrap', 'key', None)],
                run_shard=lambda *args: self.fail('GPU inference ran during CPU preparation'))
            arguments = {'batch_size': 128, 'num_writers': 2, 'num_h5_chunks': 4, 'sparse_k': 100,
                         'num_cpu_workers': 4, 'energies': [20]}
            with patch.object(glacier, '_request', return_value=(folder, ['python', 'native.py'])), \
                 patch.object(glacier.importlib, 'import_module', return_value=native):
                manifest = glacier.prepare(**arguments)
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload['entries'][0][2], 20)
            self.assertEqual(payload['options']['checkpoint'], 'weights')

    def test_prepared_glacier_uses_every_allocated_gpu_and_keeps_energies_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scratch = root / 'scratch'
            scratch.mkdir()
            manifest = root / 'prepared.json'
            manifest.write_text(json.dumps({'entries': [['A', 'a', 20], ['A', 'a', 40],
                ['B', 'b', 20], ['B', 'b', 40]], 'options': {'checkpoint': 'weights',
                'debug': False, 'out_name': 'preds.hdf5'}}))
            def spawn(function, args, nprocs, join):
                shards, checkpoint, paths, options = args
                self.assertEqual(nprocs, 2)
                self.assertTrue(join)
                self.assertEqual({tuple(row[0] for row in shard) for shard in shards}, {('A', 'A'), ('B', 'B')})
                self.assertEqual(checkpoint, 'weights')
                for path in paths:
                    path.touch()
            native = types.SimpleNamespace(common=types.SimpleNamespace(setup_logger=lambda *a, **k: None),
                mp=types.SimpleNamespace(spawn=spawn), _shard_entrypoint=object())
            with patch.object(glacier.importlib, 'import_module', return_value=native), \
                 patch.object(glacier.tempfile, 'mkdtemp', return_value=str(scratch)):
                self.assertEqual(glacier.run_prepared(manifest, '4,5'), scratch)
            self.assertTrue((scratch / 'glacier_run_successful').is_file())


if __name__ == '__main__':
    unittest.main()
