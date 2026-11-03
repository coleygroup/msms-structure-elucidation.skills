"""CPU-parent budgeting and queue accounting without model or scheduler execution."""
import asyncio
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from msms_structure_elucidation import cli
from msms_structure_elucidation.mcp import backends
from msms_structure_elucidation.mcp.cpu import plan_concurrency, workload
from msms_structure_elucidation.mcp.jobs import JobStore


class CpuBudgetTests(unittest.TestCase):
    def test_plan_bounds_both_active_and_resident_jobs_for_local_and_slurm(self):
        plan = plan_concurrency(96, 384, 2, 8)
        self.assertEqual(plan['capacity_ceiling_jobs'], 48)
        self.assertEqual(plan['reserved_at_ceiling'], {'cpus': 96, 'memory_gb': 384})
        with tempfile.TemporaryDirectory() as tmp, patch.object(backends.hostprobe, 'gpus', return_value=[]):
            config = {'mcp': plan['mcp_settings']}
            store = JobStore(Path(tmp))
            local = backends.LocalBackend(store, config)
            slurm = backends.SlurmBackend(store, config)
        self.assertEqual((local.max_cpu_jobs, local.max_live_pipelines, slurm.max_live_pipelines), (48, 48, 48))

    def test_ram_and_explicit_cap_can_be_more_restrictive_than_cpu(self):
        plan = plan_concurrency(96, 64, 2, 8)
        self.assertEqual(plan['capacity_ceiling_jobs'], 8)
        self.assertEqual(plan['cpu_bound_jobs'], 48)
        capped = plan_concurrency(96, 384, 2, 8, max_jobs=20)
        self.assertEqual(capped['capacity_ceiling_jobs'], 20)
        self.assertEqual(capped['reserved_at_ceiling'], {'cpus': 40, 'memory_gb': 160})

    def test_insufficient_budget_does_not_force_an_oversized_job(self):
        plan = plan_concurrency(2, 2, 2, 8)
        self.assertEqual(plan['status'], 'insufficient_budget')
        self.assertEqual(plan['capacity_ceiling_jobs'], 0)
        self.assertIsNone(plan['mcp_settings'])

    def test_fractional_cpu_budget_uses_decimal_resource_quantities(self):
        plan = plan_concurrency(.3, 1, .1, .1)
        self.assertEqual(plan['capacity_ceiling_jobs'], 3)
        self.assertEqual(plan['reserved_at_ceiling'], {'cpus': .3, 'memory_gb': .3})

    def test_invalid_resources_and_caps_are_rejected(self):
        for bad in (0, -1, float('nan'), float('inf'), True):
            for index in range(4):
                inputs = [96, 384, 2, 8]
                inputs[index] = bad
                with self.subTest(bad=bad, index=index), self.assertRaises(ValueError):
                    plan_concurrency(*inputs)
        for bad in (0, -1, 2.5, True):
            with self.subTest(cap=bad), self.assertRaises(ValueError):
                plan_concurrency(96, 384, 2, 8, max_jobs=bad)

    def test_cli_only_plans_and_does_not_probe_a_login_node_or_change_settings(self):
        with patch.object(cli.hostprobe, 'probe', side_effect=AssertionError('unexpected host probe')), \
                patch('msms_structure_elucidation.hostsetup.save_local', side_effect=AssertionError('unexpected config write')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['plan-cpu', '--cpu-budget', '24', '--memory-budget-gb', '64',
                '--cpus-per-job', '2', '--memory-per-job-gb', '4']), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['capacity_ceiling_jobs'], 12)
        self.assertEqual(plan['mcp_settings'], {'max_cpu_jobs': 12, 'max_live_pipelines': 12})


class CpuWorkloadTests(unittest.TestCase):
    def test_waiting_pipelines_and_submitted_jobs_do_not_count_as_useful_cpu_work(self):
        rows = [{'kind': 'pipeline', 'state': 'running', 'phase': phase} for phase in
                ['cpu_preparation', 'cpu_scoring', *['waiting_gpu'] * 5, 'waiting_cache', 'waiting_cpu']]
        rows += [{'kind': 'pipeline', 'state': 'queued'}] * 2
        rows += [{'kind': 'pipeline', 'state': 'submitted', 'phase': 'cpu_preparation'},
                 {'kind': 'pipeline', 'state': 'done'},
                 {'kind': 'gpu', 'state': 'running', 'phase': 'gpu_prediction'}]
        config = {'mcp': {'max_cpu_jobs': 48, 'slurm': {'cpus_per_task': 16, 'mem': '64G',
                  'cpu': {'cpus_per_task': 2, 'mem': '8G'}}},
                  'models': {'simulator': {'num_cpu_workers': 12, 'glacier': {'num_cpu_workers': 4}}}}
        report = workload(rows, config, SimpleNamespace(name='slurm', max_live_pipelines=50))
        self.assertEqual(report['working_jobs'], 2)
        self.assertEqual(report['waiting_prediction_jobs'], 6)
        self.assertEqual(report['waiting_cpu_jobs'], 1)
        self.assertEqual(report['running_jobs'], 9)
        self.assertEqual(report['submitted_jobs'], 1)
        self.assertEqual(report['queued_jobs'], 2)
        self.assertEqual(report['awaiting_scheduler_confirmation_jobs'], 1)
        self.assertEqual(report['resident_pipeline_limit'], 50)
        self.assertIsNone(report['active_cpu_job_limit'])
        self.assertEqual(report['new_slurm_cpu_job_resources'], {'cpus_per_task': 2, 'mem': '8G'})
        self.assertEqual(report['model_cpu_workers_per_prediction_process'], {'iceberg': 12, 'glacier': 4})


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP package is required')
class CpuToolTests(unittest.TestCase):
    def test_server_info_exposes_cpu_phases_without_counting_gpu_jobs_as_cpu_work(self):
        from msms_structure_elucidation.mcp import server
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for index, (kind, state, phase) in enumerate([
                    ('pipeline', 'running', 'cpu_preparation'),
                    ('pipeline', 'running', 'waiting_gpu'),
                    ('pipeline', 'submitted', 'cpu_preparation'),
                    ('gpu', 'running', 'gpu_prediction')]):
                job, _ = store.create('test', {'index': index}, kind=kind)
                store.set_status(job, state)
                store.set_phase(job, phase)
            backend = SimpleNamespace(name='local', max_cpu_jobs=2, max_prepared_jobs=4, max_live_pipelines=6,
                                      describe=lambda: {'backend': 'local'})
            runtime = {'config': {'_config_path': 'test.yaml'}, 'store': store, 'backend': backend}
            with patch.object(server, '_runtime', return_value=runtime), \
                    patch.object(server.tasks, 'missing_assets', return_value=[]), \
                    patch.object(server.asyncio, 'to_thread', AsyncMock(side_effect=lambda fn, *args: fn(*args))):
                report = asyncio.run(server.server_info())
        self.assertEqual(report['cpu_workload']['working_jobs'], 1)
        self.assertEqual(report['cpu_workload']['waiting_prediction_jobs'], 1)
        self.assertEqual(report['cpu_workload']['submitted_jobs'], 1)
        self.assertEqual(report['cpu_workload']['running_jobs'], 2)
        self.assertEqual(report['cpu_workload']['active_cpu_job_limit'], 2)
        self.assertEqual(report['cpu_workload']['prepared_waiter_limit'], 4)
        self.assertEqual(report['cpu_workload']['resident_pipeline_limit'], 6)

    def test_planner_tool_has_no_runtime_or_model_dependency(self):
        from msms_structure_elucidation.mcp import server
        with patch.object(server, '_runtime', side_effect=AssertionError('planner accessed the runtime')):
            result = asyncio.run(server.plan_cpu_concurrency(96, 384, 2, 8))
        self.assertEqual(result['capacity_ceiling_jobs'], 48)
        tools = {tool.name: tool for tool in asyncio.run(server.server.list_tools())}
        self.assertIn('plan_cpu_concurrency', tools)
        self.assertEqual(set(tools['plan_cpu_concurrency'].input_schema['required']),
                         {'cpu_budget', 'memory_budget_gb', 'cpus_per_job', 'memory_per_job_gb'})


if __name__ == '__main__':
    unittest.main()
