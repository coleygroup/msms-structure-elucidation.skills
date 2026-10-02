import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation import hostsetup
from msms_structure_elucidation.mcp import backends, runner, tasks
from msms_structure_elucidation.mcp.jobs import JobStore

SPECTRUM = '>compound x\n>parentmass 195.0877\n>ionization [M+H]+\n>formula C8H10N4O2\n\n>collision 20\n138.0662 100\n110.0713 20\n'


def gpu(index, total, free):
    return {'index': index, 'name': f'GPU{index}', 'memory_total_gb': total, 'memory_free_gb': free,
            'memory_used_gb': total - free}


def fake_tools(directory: Path, scripts: dict[str, str]) -> dict:
    """Write executable shell scripts and return an environment with them first on PATH."""
    for name, body in scripts.items():
        path = directory / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, 'PATH': f'{directory}{os.pathsep}{os.environ["PATH"]}'}


class JobStoreTest(unittest.TestCase):
    def test_same_request_reuses_the_job_and_a_failed_one_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job_id, reused = store.create('predict_spectra', {'smiles': ['C']}, kind='gpu')
            self.assertFalse(reused)
            self.assertEqual(store.request(job_id)['kind'], 'gpu')
            self.assertEqual(store.create('predict_spectra', {'smiles': ['C']}), (job_id, True))
            self.assertNotEqual(store.create('predict_spectra', {'smiles': ['CC']})[0], job_id)
            store.set_status(job_id, 'failed', error='boom')
            self.assertEqual(store.create('predict_spectra', {'smiles': ['C']}), (job_id, False))
            self.assertEqual(store.status(job_id)['state'], 'queued')

    def test_inline_inputs_are_written_and_change_the_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            first, _ = store.create('score_candidates', {}, {'spectrum.ms': SPECTRUM})
            second, _ = store.create('score_candidates', {}, {'spectrum.ms': SPECTRUM + '\n'})
            self.assertNotEqual(first, second)
            self.assertEqual((store.path(first) / 'spectrum.ms').read_text(), SPECTRUM)

    def test_job_ids_cannot_escape_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for bad in ('../etc', 'predict_spectra-../../x', 'x'):
                with self.assertRaises(ValueError):
                    store.path(bad)


class RunnerTest(unittest.TestCase):
    def run_job(self, function):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(Path(tmp.name))
        job_id, _ = store.create('fake_task', {'value': 2})
        with patch.dict(tasks.RUNNERS, {'fake_task': function}), patch.object(runner, 'settings', return_value={}), \
                patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '3'}):
            code = runner.main([str(store.path(job_id))])
        return store, job_id, code

    def test_result_and_devices_are_recorded(self):
        seen = {}

        def task(params, job_dir, config, devices, log):
            seen['devices'] = devices
            return {'double': params['value'] * 2}
        store, job_id, code = self.run_job(task)
        self.assertEqual((code, store.status(job_id)['state'], store.result(job_id)), (0, 'done', {'double': 4}))
        self.assertEqual(seen['devices'], '3')
        with patch.dict(tasks.RUNNERS, {'fake_task': lambda *a: 1 / 0}):
            self.assertEqual(runner.main([str(store.path(job_id))]), 0)  # done jobs are not rerun

    def test_failures_keep_the_error_and_traceback(self):
        def task(params, job_dir, config, devices, log):
            raise FileNotFoundError('checkpoint missing')
        store, job_id, code = self.run_job(task)
        status = store.status(job_id)
        self.assertEqual((code, status['state']), (1, 'failed'))
        self.assertIn('checkpoint missing', status['error'])
        self.assertIn('Traceback', (store.path(job_id) / 'log.txt').read_text())


class GpuPickerTest(unittest.TestCase):
    def test_free_means_idle_with_a_fraction_of_its_own_memory(self):
        cards = [gpu(0, 8.0, 7.5), gpu(1, 80.0, 30.0), gpu(2, 24.0, 23.0), gpu(3, 24.0, 23.0), gpu(4, 48.0, 47.0)]
        with patch.object(backends.hostprobe, 'gpus', return_value=cards), \
                patch.object(backends, 'busy_gpu_indices', return_value={3}):
            self.assertEqual(backends.free_gpus(0.8, reserved={4}), [0, 2])

    def test_busy_indices_come_from_compute_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fake_tools(Path(tmp), {'nvidia-smi': '''case "$1" in
  --query-gpu=index,uuid) printf '0, GPU-aaa\\n1, GPU-bbb\\n' ;;
  --query-compute-apps=gpu_uuid) printf 'GPU-bbb\\nGPU-bbb\\n' ;;
esac
'''})
            with patch.dict(os.environ, env):
                self.assertEqual(backends.busy_gpu_indices(), {1})


class LocalBackendTest(unittest.TestCase):
    def backend(self, tmp, gpus, limit='auto'):
        store = JobStore(Path(tmp))
        with patch.object(backends.hostprobe, 'gpus', return_value=gpus):
            backend = backends.LocalBackend(store, {'mcp': {'max_concurrent_jobs': limit}})
        launched = []

        def launch(job_id, devices):
            launched.append((store.request(job_id)['task'], devices))
            store.set_launch(job_id, backend='local', host=backend.host, pid=os.getpid(), devices=devices)
            store.set_status(job_id, 'submitted')
        backend._launch = launch
        return store, backend, launched

    def test_gpu_jobs_wait_for_a_gpu_while_cpu_capable_jobs_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, backend, launched = self.backend(tmp, [gpu(0, 24.0, 23.0)], limit=4)
            store.create('predict_spectra', {'n': 1}, kind='gpu')
            store.create('retrieve_atlas', {'n': 2}, kind='any')
            with patch.object(backends, 'free_gpus', return_value=[]):
                backend.pump()
            self.assertEqual(launched, [('retrieve_atlas', '')])
            with patch.object(backends, 'free_gpus', return_value=[0]):
                backend.pump()
            self.assertEqual(launched[-1], ('predict_spectra', '0'))

    def test_concurrency_is_capped_and_cpu_hosts_run_on_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, backend, launched = self.backend(tmp, [])
            self.assertEqual(backend.max_jobs, 1)
            store.create('predict_spectra', {'n': 1})
            store.create('predict_spectra', {'n': 2})
            backend.pump()
            self.assertEqual(launched, [('predict_spectra', '')])

    def test_detached_runner_reports_a_failed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            with patch.object(backends.hostprobe, 'gpus', return_value=[]):
                backend = backends.LocalBackend(store, {'mcp': {}})
            job_id, _ = store.create('no_such_task', {}, kind='cpu')
            backend.submit(job_id)
            deadline = time.monotonic() + 60
            while backend.poll(job_id)['state'] in ('queued', 'submitted', 'running') and time.monotonic() < deadline:
                time.sleep(0.2)
            status = store.status(job_id)
            self.assertEqual(status['state'], 'failed')
            self.assertIn('no_such_task', status['error'])


class SlurmTest(unittest.TestCase):
    def test_sbatch_options_contain_only_configured_keys(self):
        options, script = backends.sbatch_script({'partition': None, 'gpus': None, 'setup': []}, 'gpu', ['py', '-m', 'x'])
        self.assertEqual(options, [])
        self.assertEqual(script, '#!/bin/bash\nexec py -m x\n')
        slurm = {'partition': 'p', 'account': 'a', 'qos': 'q', 'gpus': 't:1', 'cpus_per_task': 16, 'mem': '160G',
                 'time': '01:00:00', 'requeue': True, 'extra_args': ['--constraint=x'],
                 'setup': ['source /etc/profile', 'conda activate env'], 'cpu': {'cpus_per_task': 4, 'mem': None}}
        options, script = backends.sbatch_script(slurm, 'gpu', ['py', 'a b'])
        self.assertEqual(options, ['--partition=p', '--account=a', '--qos=q', '--time=01:00:00', '--gpus=t:1',
                                   '--cpus-per-task=16', '--mem=160G', '--requeue', '--constraint=x'])
        self.assertEqual(script.splitlines()[1:], ['source /etc/profile', 'conda activate env', "exec py 'a b'"])
        options, _ = backends.sbatch_script(slurm, 'any', ['py'])
        self.assertNotIn('--gpus=t:1', options)
        self.assertIn('--cpus-per-task=4', options)
        self.assertIn('--mem=160G', options)

    def test_submit_poll_and_cancel_through_slurm_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / 'state'
            state.write_text('PENDING')
            env = fake_tools(tmp, {'sbatch': 'echo "$@" > "$(dirname "$0")/sbatch.args"; echo "4242;cluster"\n',
                                   'sacct': f'cat {state}\n', 'squeue': 'true\n',
                                   'scancel': 'echo "$1" > "$(dirname "$0")/scancel.args"\n'})
            store = JobStore(tmp / 'jobs')
            backend = backends.SlurmBackend(store, {'mcp': {'slurm': {'partition': 'p', 'gpus': '1'}}}, python='/venv/python')
            job_id, _ = store.create('predict_spectra', {})
            with patch.dict(os.environ, env):
                backend.submit(job_id)
                self.assertEqual(store.status(job_id)['slurm_job_id'], '4242')
                args = (tmp / 'sbatch.args').read_text()
                self.assertIn('--partition=p', args)
                self.assertIn('/venv/python -m msms_structure_elucidation.mcp.runner', (store.path(job_id) / 'job.sh').read_text())
                self.assertEqual(backend.poll(job_id)['scheduler_state'], 'PENDING')
                state.write_text('OUT_OF_MEMORY')
                self.assertEqual(backend.poll(job_id)['state'], 'failed')
                second, _ = store.create('predict_spectra', {'n': 2})
                backend.submit(second)
                state.write_text('RUNNING')
                backend.cancel(second)
                self.assertEqual((tmp / 'scancel.args').read_text().strip(), '4242')
                self.assertEqual(store.status(second)['state'], 'cancelled')

    def test_a_runner_result_wins_over_the_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fake_tools(Path(tmp), {'sacct': 'echo COMPLETED\n'})
            store = JobStore(Path(tmp) / 'jobs')
            job_id, _ = store.create('predict_spectra', {})
            store.set_launch(job_id, backend='slurm', slurm_job_id='7')
            store.set_status(job_id, 'done')
            with patch.dict(os.environ, env):
                self.assertEqual(backends.SlurmBackend(store, {}).poll(job_id)['state'], 'done')

    def test_partitions_are_summarised_from_sinfo(self):
        out = 'gpu*|gpu:a:4|2-00:00:00|64|512000|up|3\ngpu*|gpu:b:8(S:0-1)|2-00:00:00|128|1024000|up|2\ncpu|(null)|1:00:00|32|128000|up|10\n'
        rows = hostsetup.slurm_partitions(lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=''))
        self.assertEqual(rows[0]['partition'], 'gpu')
        self.assertTrue(rows[0]['default'])
        self.assertEqual(rows[0]['gpus'], ['gpu:a:4', 'gpu:b:8(S:0-1)'])
        self.assertEqual((rows[0]['cpus_per_node'], rows[0]['nodes']), (128, 5))
        self.assertEqual(rows[1]['gpus'], [])


class ClientConfigTest(unittest.TestCase):
    def test_local_and_ssh_entries(self):
        self.assertEqual(hostsetup.client_entry(None, '/venv/bin/python'),
                         {'command': '/venv/bin/python', 'args': ['-m', 'msms_structure_elucidation.mcp']})
        entry = hostsetup.client_entry({'mode': 'remote', 'host': 'user@gpu-box', 'repo': '~/my repo',
                                        'python': '.cache/mcp-venv/bin/python', 'prefix': None, 'ssh_options': ['Port=2222']})
        self.assertEqual(entry['command'], 'ssh')
        self.assertEqual(entry['args'][:5], ['-o', 'BatchMode=yes', '-o', 'Port=2222', 'user@gpu-box'])
        self.assertEqual(entry['args'][-1], "cd ~/'my repo' && .cache/mcp-venv/bin/python -m msms_structure_elucidation.mcp")
        with self.assertRaises(ValueError):
            hostsetup.client_entry({'mode': 'remote', 'host': 'h', 'repo': None})

    def test_writing_keeps_other_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.mcp.json'
            path.write_text(json.dumps({'mcpServers': {'other': {'command': 'x'}}}))
            hostsetup.write_client_config(path, 'msms', {'command': 'y'})
            self.assertEqual(json.loads(path.read_text())['mcpServers'], {'other': {'command': 'x'}, 'msms': {'command': 'y'}})


class InterpreterTest(unittest.TestCase):
    def test_setup_reports_interpreters_missing_modules(self):
        def run(command, **kwargs):
            missing = 'ms_pred' if 'frigid' in command[0] else ''
            return SimpleNamespace(returncode=0, stdout=missing + '\n', stderr='')
        problems = hostsetup.check_interpreters({'simulator': {'python': '/envs/ms/bin/python'},
                                                 'denovo': {'frigid_python': '/envs/frigid/bin/python'}}, run)
        self.assertEqual(len(problems), 1)
        self.assertIn('models.denovo.frigid_python', problems[0])
        self.assertIn('cannot import ms_pred', problems[0])

    def test_venv_interpreters_are_not_resolved_to_their_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'base-python'
            base.write_text('')
            venv = Path(tmp) / 'venv' / 'bin'
            venv.mkdir(parents=True)
            (venv / 'python').symlink_to(base)
            config = {'_config_path': str(Path(tmp) / 'c.yaml')}
            self.assertEqual(tasks._interpreter(str(venv / 'python'), config), str(venv / 'python'))
            self.assertEqual(tasks.cli.model_python(str(venv / 'python')), str(venv / 'python'))


class TaskTest(unittest.TestCase):
    def test_missing_assets_name_the_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt = Path(tmp) / 'gen.ckpt'
            ckpt.write_text('x')
            config = {'_config_path': str(Path(tmp) / 'c.yaml'),
                      'models': {'simulator': {'ms_pred_src': tmp, 'gen_ckpt': str(ckpt), 'inten_ckpt': ''}, 'denovo': {}}}
            self.assertEqual(tasks.missing_assets('predict_spectra', config), ['models.simulator.inten_ckpt'])
            self.assertEqual(tasks.missing_assets('predict_spectra', config, model='glacier'), ['models.simulator.glacier_ckpt'])
            self.assertEqual(tasks.missing_assets('retrieve_atlas', config), [])
            self.assertIn('models.denovo.dlm_ckpt', tasks.missing_assets('generate_structures_frigid', config))
            self.assertNotIn('models.denovo.dlm_ckpt', tasks.missing_assets('predict_fingerprint_mist', config))

    def test_gpu_workers_scale_to_the_gpus_a_job_holds(self):
        config = {'_config_path': '/c/c.yaml', 'models': {'simulator': {'cuda_devices': '0,1', 'num_gpu_workers': 4}}}
        self.assertEqual(tasks._simulator_options(config, 'iceberg', '5')['num_gpu_workers'], 2)
        self.assertEqual(tasks._simulator_options(config, 'iceberg', '5')['cuda_devices'], '5')
        self.assertEqual(tasks._simulator_options(config, 'iceberg', None)['num_gpu_workers'], 1)

    def test_prediction_summary_keeps_the_strongest_peaks(self):
        result = {'model': 'ICEBERG', 'predictions': [{'smiles': 'C', 'adduct': '[M+H]+', 'annotated_peaks': None,
                   'predicted_spectra': {'20': [[50.0, 0.1], [60.0, 1.0], [70.0, 0.5]]}}]}
        summary = tasks.summarize('predict_spectra', result, {'max_peaks': 2, 'collision_unit': 'eV'})
        self.assertEqual(summary['predictions'][0]['spectra']['20'], [[60.0, 1.0], [70.0, 0.5]])


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'mcp is not installed (pip install -e .[mcp])')
class ServerTest(unittest.TestCase):
    def setUp(self):
        from msms_structure_elucidation.mcp import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Path(self.tmp.name) / 'config.yaml'
        config.write_text('models:\n  simulator:\n    ms_pred_src: ""\n  denovo:\n    frigid_src: ""\nmcp:\n  backend: local\n')
        self.addCleanup(os.environ.pop, 'MSMS_CONFIG', None)
        with patch.object(backends.hostprobe, 'gpus', return_value=[]):
            server.configure(None, str(config), str(Path(self.tmp.name) / 'jobs'))

    def test_tool_schemas(self):
        tools = {tool.name: tool for tool in asyncio.run(self.server.server.list_tools())}
        self.assertTrue({'server_info', 'predict_spectra', 'score_candidates', 'retrieve_atlas', 'get_job',
                         'generate_structures_frigid', 'predict_fingerprint_mist', 'pubchem_compound'} <= set(tools))
        schema = tools['predict_spectra'].input_schema
        self.assertIn('collision_unit', schema['required'])
        self.assertNotIn('ctx', schema['properties'])

    def test_unconfigured_models_fail_cleanly(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaisesRegex(ToolError, 'not configured on this server'):
            asyncio.run(self.server.server.call_tool('predict_spectra', {
                'smiles': ['C'], 'collision_energies': [20], 'collision_unit': 'eV'}))

    def test_spectrum_must_be_given_once_and_be_valid(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.server._spectrum_inputs({}, None, None)
        with self.assertRaisesRegex(ValueError, 'no MS/MS peaks'):
            self.server._spectrum_inputs({}, '>parentmass 100\n', None)
        self.assertEqual(self.server._spectrum_inputs({}, SPECTRUM, None), {'spectrum.ms': SPECTRUM})


if __name__ == '__main__':
    unittest.main()
