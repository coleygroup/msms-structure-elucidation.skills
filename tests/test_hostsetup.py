import concurrent.futures
import json
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation import cli, config, hostprobe, hostsetup


def gpu(index, total, free):
    return {'index': index, 'name': f'GPU{index}', 'memory_total_gb': total, 'memory_free_gb': free,
            'memory_used_gb': total - free}


def host(gpus, threads=16, ram=16.0):
    return {'hostname': 'box', 'platform': 'Linux', 'cpu_threads': threads, 'ram_total_gb': ram,
            'ram_available_gb': ram, 'gpus': gpus}


def remote_args(**overrides):
    values = dict(remote='gpu-box', remote_repo=None, remote_python=None,
                  remote_prefix='source ~/env/bin/activate ms-pred &&', ssh_option=['Port=2222'],
                  ms_pred_python=None, ms_pred_dir='~/ms-pred', gen_checkpoint='~/g.ckpt',
                  inten_checkpoint='~/i.ckpt', no_benchmark=False)
    values.update(overrides)
    return SimpleNamespace(**values)


class RecommendTest(unittest.TestCase):
    def test_measured_anchors_are_reproduced(self):
        self.assertEqual(hostprobe.anchor_batch_size(8.0), 16)
        self.assertEqual(hostprobe.anchor_batch_size(24.0), 128)
        self.assertEqual(hostprobe.anchor_batch_size(80.0), hostprobe.MAX_BATCH)

    def test_laptop_and_a5000_hosts(self):
        laptop = hostprobe.recommend(host([gpu(0, 8.0, 7.8)]))
        self.assertEqual((laptop['cuda_devices'], laptop['batch_size'], laptop['num_cpu_workers'],
                          laptop['num_gpu_workers']), ('0', 16, 16, 2))
        a5000 = hostprobe.recommend(host([gpu(0, 24.0, 2.0), gpu(1, 24.0, 23.7)]))
        self.assertEqual((a5000['cuda_devices'], a5000['batch_size'], a5000['num_gpu_workers']), ('1', 128, 2))
        self.assertEqual(a5000['shard_size'], 1024)

    def test_two_free_gpus_double_workers_and_cpu_only_bounds_ram(self):
        self.assertEqual(hostprobe.recommend(host([gpu(0, 24.0, 23.5), gpu(1, 24.0, 23.5)]))['num_gpu_workers'], 4)
        cpu = hostprobe.recommend(host([], threads=32, ram=16.0))
        self.assertEqual((cpu['cuda_devices'], cpu['num_cpu_workers']), (None, 4))


class BatchWorkersTest(unittest.TestCase):
    def test_concurrent_features_follow_cpu_and_ram(self):
        self.assertEqual(hostprobe.batch_workers(16, 15.3), 4)   # 8 GB-GPU laptop, RAM bound
        self.assertEqual(hostprobe.batch_workers(32, 64.0), 8)   # workstation, capped
        self.assertEqual(hostprobe.batch_workers(4, 64.0), 4)    # CPU bound
        self.assertEqual(hostprobe.batch_workers(4, 8.0), 1)     # never below one
        self.assertEqual(hostprobe.batch_workers(32, 12.0, max_model_jobs=2), 1)


class BatchConcurrencyTest(unittest.TestCase):
    def test_auto_runs_several_features_at_once(self):
        running, peak, lock = [0], [0], threading.Lock()

        def fake_run(opts):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.2)
            with lock:
                running[0] -= 1
            Path(opts.output_dir).mkdir(parents=True, exist_ok=True)
            (Path(opts.output_dir) / 'retrieval.json').write_text('{"status": "ranked"}')

        memory = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=64 * 1024 ** 3))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(6):
                (root / f'f{i}.ms').write_text('>parentmass 100\n50 1\n')
            manifest = root / 'manifest.csv'
            manifest.write_text('feature_id,ms_path,collision_unit\n'
                                + ''.join(f'f{i},f{i}.ms,eV\n' for i in range(6)))
            args = SimpleNamespace(manifest=str(manifest), output_dir=str(root / 'out'), config=None,
                collision_unit=None, formulas_file=None, ms_pred_python=None, ms_pred_dir=None,
                instrument=None, cuda_devices=None, model=None, checkpoint=None, gen_checkpoint=None,
                inten_checkpoint=None, model_batch_size=None, model_shard_size=None, model_cpu_workers=None,
                model_gpu_workers=None, atlas_url=None, top_k=None, no_report=True, max_workers=None,
                max_model_jobs=None, min_free_memory_gb=None, no_review_page=True, serve=False)
            # Threads stand in for the spawned worker processes so the patched run() is visible.
            def thread_pool(max_workers, mp_context, initializer, initargs):
                return concurrent.futures.ThreadPoolExecutor(max_workers, initializer=initializer, initargs=initargs)
            with patch.object(cli, 'settings', return_value={'_config_digest': 'x', 'models': {'batch': {'max_workers': 'auto'}}}), \
                    patch.object(cli.hostprobe, 'batch_workers', return_value=3), \
                    patch.object(cli.concurrent.futures, 'ProcessPoolExecutor', side_effect=thread_pool), \
                    patch.object(cli.multiprocessing, 'get_context',
                                 return_value=SimpleNamespace(BoundedSemaphore=threading.BoundedSemaphore)), \
                    patch.object(cli, 'run', side_effect=fake_run), \
                    patch.dict(sys.modules, {'psutil': memory}), patch('builtins.print'):
                self.assertEqual(cli.batch(args), 0)
        self.assertEqual(peak[0], 3)


class TuneTest(unittest.TestCase):
    def test_pool_setup_records_worker_ratio_and_retains_explicit_tuning(self):
        contract = {'ready': True, 'weights': {'checkpoint':'same'}, 'source':'native',
                    'adapter':'package', 'sparse_k':100}
        calls = []
        def probe(endpoint, operation, payload=None):
            calls.append((endpoint.name, operation, dict(endpoint.settings.get('feature_config', {}).get('mcp', {}))))
            return {'max_concurrent_jobs':8} if operation=='probe' else {'contract':contract}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'servers.json'
            base = {'transport':'local', 'job_root':str(Path(tmp)/'jobs'), 'features':True,
                    'models':['glacier'], 'max_jobs':'auto'}
            source.write_text(json.dumps({'new':base, 'tuned':{**base, 'feature_config':{
                'mcp':{'feature_preparation_workers':10,'feature_scoring_workers':3,'feature_output_workers':4}}}}))
            with patch('msms_structure_elucidation.mcp.fleet.Endpoint.call',new=probe), \
                    patch.object(hostsetup,'save_local') as save:
                hostsetup.setup_pool(source)
            nodes=save.call_args.args[0]['mcp']['servers']
            configured=nodes['new']['feature_config']['mcp']
            self.assertEqual((configured['feature_preparation_workers'],configured['feature_scoring_workers'],
                              configured['feature_output_workers'],configured['max_cpu_jobs']), (16,4,8,'auto'))
            self.assertEqual(nodes['tuned']['feature_config']['mcp']['feature_preparation_workers'],10)
            self.assertEqual(next(row[2] for row in calls if row[:2]==('new','worker-probe')),
                             configured)

    def test_setup_records_separate_eight_gpu_stage_counts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hostsetup, 'LOCAL_CONFIG', Path(tmp)/'local.yaml'), \
                patch.object(hostprobe, 'probe', return_value=host([gpu(i,24,24) for i in range(8)],52,256)), \
                patch.object(hostsetup, 'save_local') as save:
            args = SimpleNamespace(remote=None, scheduler=None, no_benchmark=True, model='glacier')
            report = hostsetup.setup(args, {'model':'glacier'}, lambda value:value)
            saved = save.call_args.args[0]
            self.assertEqual(saved['mcp']['feature_preparation_workers'], 16)
            self.assertEqual(saved['mcp']['feature_scoring_workers'], 4)
            self.assertEqual(saved['mcp']['feature_output_workers'], 8)
            self.assertEqual(saved['mcp']['max_cpu_jobs'], 'auto')
            self.assertNotIn('feature_cpu_workers', saved['mcp'])
            self.assertEqual(report['mcp_worker_settings'], saved['mcp'])
            self.assertFalse(saved['host_profile']['pipeline_workers']['measured'])

    def test_pool_worker_ratio_respects_allowed_physical_gpu_capacity(self):
        contract = {'ready': True, 'weights': {'checkpoint': 'same'}, 'source': 'native',
                    'adapter': 'package', 'sparse_k': 100}
        def probe(endpoint, operation, payload=None):
            if operation == 'probe':
                return {'max_concurrent_jobs': 8, 'gpus': [gpu(i, 24, 24) for i in range(3)]}
            return {'contract': contract}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'servers.json'
            base = {'transport': 'local', 'job_root': str(Path(tmp) / 'jobs'),
                    'features': True, 'models': ['glacier'], 'max_jobs': 8,
                    'mcp': {'gpu_devices': [1]}}
            source.write_text(json.dumps({'local': base, 'scheduled': {**base, 'backend': 'slurm'}}))
            with patch('msms_structure_elucidation.mcp.fleet.Endpoint.call', new=probe), \
                    patch.object(hostsetup, 'save_local') as save:
                hostsetup.setup_pool(source)
            nodes = save.call_args.args[0]['mcp']['servers']
            local = nodes['local']['feature_config']['mcp']
            scheduled = nodes['scheduled']['feature_config']['mcp']
            self.assertEqual((local['feature_preparation_workers'], local['feature_scoring_workers'],
                              local['feature_output_workers']), (2, 1, 1))
            self.assertEqual((scheduled['feature_preparation_workers'], scheduled['feature_scoring_workers'],
                              scheduled['feature_output_workers']), (16, 4, 8))

    def test_setup_handles_auto_preparation_and_explicit_legacy_shared_workers(self):
        machine = host([gpu(0, 24, 1), gpu(1, 24, 24)])
        counts, _ = hostsetup.stage_worker_settings(machine, {'feature_preparation_workers': 'auto'})
        self.assertEqual((counts['feature_preparation_workers'], counts['feature_scoring_workers'],
                          counts['feature_output_workers']), (4, 1, 2))
        counts, _ = hostsetup.stage_worker_settings(machine, {'feature_preparation_workers': 0})
        self.assertEqual(counts['feature_cpu_workers'], 2)
        self.assertEqual(counts['feature_preparation_workers'], 0)
        self.assertIsNone(counts['feature_scoring_workers'])

    def test_stage_ratio_counts_busy_gpus_and_preserves_saved_host_tuning(self):
        machine = host([gpu(0,24,1), gpu(1,24,24), gpu(2,24,24)])
        counts, profile = hostsetup.stage_worker_settings(machine, {})
        self.assertEqual((counts['feature_preparation_workers'], counts['feature_scoring_workers'],
                          counts['feature_output_workers']), (6,2,3))
        self.assertEqual(profile['gpu_slots'], 3)
        counts, _ = hostsetup.stage_worker_settings(machine, {'gpu_devices':[1]})
        self.assertEqual(counts['feature_preparation_workers'], 2)
        counts, _ = hostsetup.stage_worker_settings(machine, {'max_concurrent_jobs':2})
        self.assertEqual(counts['feature_scoring_workers'], 1)
        counts, profile = hostsetup.stage_worker_settings(machine, {'backend':'slurm'})
        self.assertEqual(profile['gpu_slots'], 1)
        counts, profile = hostsetup.stage_worker_settings(machine, {'backend':'slurm','max_concurrent_jobs':8})
        self.assertEqual(profile['gpu_slots'], 8)
        counts, _ = hostsetup.stage_worker_settings(machine, {'feature_cpu_workers':20,
            'feature_preparation_workers':16, 'feature_output_workers':8, 'max_cpu_jobs':24})
        self.assertEqual((counts['feature_preparation_workers'], counts['feature_scoring_workers'],
                          counts['feature_output_workers'], counts['max_cpu_jobs']), (16,4,8,24))

    def test_glacier_setup_tunes_its_own_batch_and_keeps_one_gpu_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'glacier.ckpt'
            checkpoint.touch()
            args = SimpleNamespace(remote=None, scheduler=None, no_benchmark=False,
                                   ms_pred_python=sys.executable, model='glacier', checkpoint=str(checkpoint))
            options = {'model': 'glacier', 'checkpoint': str(checkpoint), 'ms_pred_dir': tmp,
                       'batch_size': 128, 'num_gpu_workers': 1, 'num_cpu_workers': 4}
            tested = []
            def run(size, molecules=None):
                tested.append(size)
                return {'batch_size': size, 'gpu_workers': 1, 'ok': True,
                        'spectra_per_second': 100 if size == 128 else 104, 'peak_memory_fraction': .2}
            with patch.object(hostprobe, 'probe', return_value=host([gpu(0, 24., 24.)])), \
                 patch.object(hostsetup, 'benchmark_trial', return_value=run) as trial, \
                 patch.object(hostsetup, 'save_local') as save:
                report = hostsetup.setup(args, options, lambda value: value)
            self.assertEqual(tested, [128, 128, 256])
            self.assertEqual(trial.call_args.args[6], 'glacier')
            self.assertEqual(report['simulator']['glacier']['batch_size'], 128)
            self.assertEqual(report['simulator']['glacier']['num_gpu_workers'], 1)
            self.assertEqual(report['simulator']['glacier']['spectra_per_second'], 100)
            self.assertNotIn('batch_size', report['simulator'])
            self.assertNotIn('num_gpu_workers', report['simulator'])
            self.assertEqual(save.call_args.args[0]['models']['simulator']['model'], 'glacier')
    @staticmethod
    def trial(limit, rates):
        def run(size):
            fits = size <= limit
            return {'batch_size': size, 'ok': fits, 'spectra_per_second': rates.get(size) if fits else None,
                    'peak_memory_fraction': 0.5 if fits else None}
        return run

    def test_doubles_until_throughput_plateaus(self):
        best, trials = hostsetup.tune_batch_size(self.trial(512, {16: 10, 32: 18, 64: 18.5, 128: 30}), 16)
        self.assertEqual(best, 32)
        self.assertEqual([t['batch_size'] for t in trials], [16, 32, 64])

    def test_halves_after_out_of_memory_and_does_not_climb_back(self):
        best, trials = hostsetup.tune_batch_size(self.trial(32, {32: 10}), 128)
        self.assertEqual(best, 32)
        self.assertEqual([t['batch_size'] for t in trials], [128, 64, 32])

    def test_memory_ceiling_rejects_a_successful_run(self):
        run = lambda size: {'batch_size': size, 'ok': True, 'spectra_per_second': size,
                            'peak_memory_fraction': 0.5 if size <= 16 else 0.95}
        self.assertEqual(hostsetup.tune_batch_size(run, 16)[0], 16)


class LocalConfigTest(unittest.TestCase):
    def test_local_overlay_round_trip_keeps_hand_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'local.yaml'
            local.write_text('models:\n  simulator:\n    instrument: "QTOF"\n')
            with patch.object(config, 'LOCAL_CONFIG', local), patch.object(hostsetup, 'LOCAL_CONFIG', local):
                hostsetup.save_local({'models': {'simulator': {'cuda_devices': '0,1', 'batch_size': 64}},
                                      'host_profile': {'hostname': 'elsewhere', 'benchmark': {}}})
                self.assertEqual(config._simple_yaml(local.read_text())['models']['simulator']['cuda_devices'], '0,1')
                with patch('sys.stderr') as stderr:
                    merged = config.settings()
                simulator = merged['models']['simulator']
                self.assertEqual((simulator['batch_size'], simulator['instrument'], simulator['shard_size']),
                                 (64, 'QTOF', 256))
                self.assertIn('elsewhere', ''.join(call.args[0] for call in stderr.write.call_args_list))
                self.assertNotEqual(merged['_config_digest'],
                                    config.settings(str(config.DEFAULT_CONFIG))['_config_digest'])
                self.assertNotIn('_local_config_path', config.settings(str(config.DEFAULT_CONFIG)))


class RemoteTest(unittest.TestCase):
    def test_probe_only_pipes_the_stdlib_probe_over_ssh(self):
        report = {'host': host([gpu(0, 24.0, 23.7)]), 'recommended': {'batch_size': 128}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hostsetup, 'LOCAL_CONFIG', Path(tmp) / 'local.yaml'), \
                patch.object(hostsetup.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], 0, json.dumps(report), '')) as run:
            result = hostsetup.setup_remote(remote_args())
            saved = config.read_yaml(Path(tmp) / 'local.yaml')
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ['ssh', '-o', 'BatchMode=yes', '-o', 'Port=2222', 'gpu-box'])
        self.assertEqual(argv[6], 'source ~/env/bin/activate ms-pred && python3 -')
        self.assertIn('def recommend', run.call_args.kwargs['input'])
        self.assertEqual(result['status'], 'probed_not_installed')
        self.assertEqual(saved['execution']['recommended']['batch_size'], 128)

    def test_installed_remote_runs_setup_in_its_checkout(self):
        remote = {'status': 'benchmarked', 'simulator': {'batch_size': 128}, 'host': host([])}
        stdout = 'benchmark: batch_size=128\nSETUP_JSON=' + json.dumps(remote) + '\n'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hostsetup, 'LOCAL_CONFIG', Path(tmp) / 'local.yaml'), \
                patch.object(hostsetup.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], 0, stdout, '')) as run, \
                patch('builtins.print'):
            result = hostsetup.setup_remote(remote_args(remote_repo='~/msms repo', remote_python='~/venv/bin/python'))
            saved = config.read_yaml(Path(tmp) / 'local.yaml')
        body = run.call_args.args[0][-1]
        self.assertTrue(body.startswith("source ~/env/bin/activate ms-pred && cd ~/'msms repo' && "))
        self.assertIn('msms_structure_elucidation.cli setup --json --ms-pred-dir', body)
        self.assertEqual(result['status'], 'benchmarked')
        self.assertEqual(saved['execution']['simulator']['batch_size'], 128)


if __name__ == '__main__':
    unittest.main()
