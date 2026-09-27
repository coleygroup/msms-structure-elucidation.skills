import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation import config, hostprobe, hostsetup


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


class TuneTest(unittest.TestCase):
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
        self.assertTrue(body.startswith("source ~/env/bin/activate ms-pred && cd '~/msms repo' && "))
        self.assertIn('msms_structure_elucidation.cli setup --json --ms-pred-dir', body)
        self.assertEqual(result['status'], 'benchmarked')
        self.assertEqual(saved['execution']['simulator']['batch_size'], 128)


if __name__ == '__main__':
    unittest.main()
