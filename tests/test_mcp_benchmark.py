"""Benchmark receipts distinguish real inference from whole-invocation utilization."""
import csv
import datetime
import tempfile
import unittest
from pathlib import Path

from scripts.benchmark_mcp_pipeline import inference_stages
from msms_structure_elucidation.mcp.jobs import JobStore, write_json


class BenchmarkTests(unittest.TestCase):
    def test_native_window_keeps_zero_samples_and_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / 'jobs')
            job, _ = store.create('model_inference', {'arguments': {'smiles': ['CCO', 'COC'],
                'energies': [20, 40]}}, kind='gpu')
            folder = store.path(job)
            store.set_status(job, 'done', phase_seconds={'gpu_prediction': 12})
            (folder / 'log.txt').write_text(
                '2026-11-05 12:00:01,000 INFO: [cuda:0] 2 writer process(es) across 4 HDF5 chunk(s)\n'
                '2026-11-05 12:00:03,000 INFO: [cuda:0] DONE 1 batches / 4 rows in 2.0s (2 rows/s).\n')
            start = datetime.datetime(2026, 11, 5, 12).timestamp()
            with (folder / 'gpu-samples.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=['time', 'utilization_pct'])
                writer.writeheader()
                for second, utilization in [(0, 100), (1, 0), (2, 100), (3, 50), (4, 100)]:
                    writer.writerow({'time': start + second, 'utilization_pct': utilization})
            report = inference_stages(store)[0]
            self.assertEqual(report['native_window']['mean_gpu_util_pct'], 50)
            self.assertEqual(report['native_window']['samples'], 3)
            self.assertEqual(report['expected_spectra'], 4)
            self.assertEqual(report['native_rows'], 4)
            self.assertEqual(report['native_spectra_per_second'], 2)
            self.assertEqual(report['phase_seconds']['gpu_prediction'], 12)

    def test_collected_metrics_and_no_native_timestamps_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / 'jobs')
            job, _ = store.create('model_inference', {'expected_spectra': 3,
                'arguments': {'smiles': [], 'energies': [20, 40]}}, kind='gpu')
            collected = store.path(job) / 'collected'
            collected.mkdir()
            write_json(collected / 'gpu-metrics.json', {'elapsed_seconds': 12})
            (collected / 'log.txt').write_text('No native timing format available\n')
            report = inference_stages(store)[0]
            self.assertEqual(report['expected_spectra'], 3)
            self.assertEqual(report['runner_gpu_metrics']['elapsed_seconds'], 12)
            self.assertIsNone(report['native_seconds'])
            self.assertNotIn('native_window', report)

    def test_worker_timezone_is_used_for_remote_native_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / 'jobs')
            job, _ = store.create('model_inference', {'arguments': {'smiles': ['CCO'],
                'energies': [20]}}, kind='gpu')
            folder = store.path(job) / 'collected'
            folder.mkdir()
            write_json(folder / 'gpu-metrics.json', {'host_utc_offset_seconds': 9 * 3600})
            (folder / 'log.txt').write_text(
                '2026-11-05 12:00:01,000 INFO: [cuda:0] 2 writer process(es)\n'
                '2026-11-05 12:00:03,000 INFO: [cuda:0] DONE 1 batches / 1 rows in 2.0s\n')
            start = datetime.datetime(2026, 11, 5, 3, tzinfo=datetime.timezone.utc).timestamp()
            with (folder / 'gpu-samples.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=['time', 'utilization_pct'])
                writer.writeheader()
                writer.writerow({'time': start + 2, 'utilization_pct': 80})
            report = inference_stages(store)[0]['native_window']
            self.assertEqual(report['timezone_source'], 'runner')
            self.assertEqual(report['mean_gpu_util_pct'], 80)


if __name__ == '__main__':
    unittest.main()
