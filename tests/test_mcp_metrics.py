import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp.metrics import FIELDS, GpuMonitor, summarize


class MetricsTests(unittest.TestCase):
    def test_mean_keeps_idle_samples_and_detects_foreign_compute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gpu-samples.csv'
            with path.open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                for utilization, pids in [(0, ''), (100, '123')]:
                    writer.writerow(dict(zip(FIELDS, [10, 0, 'GPU-uuid', 'card', utilization, 1024, 11264, pids])))
            metrics = summarize(path, 1., 3.)
            self.assertEqual(metrics['devices']['GPU-uuid']['mean_utilization_pct'], 50)
            self.assertTrue(metrics['devices']['GPU-uuid']['foreign_compute_observed'])
            self.assertEqual(metrics['elapsed_seconds'], 2.)

    def test_disabled_monitor_is_unavailable_instead_of_fake_zero(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            with GpuMonitor(Path(tmp), None):
                pass
            result = json.loads((Path(tmp) / 'gpu-metrics.json').read_text())
            self.assertFalse(result['available'])
            self.assertEqual(result['devices'], {})

    def test_container_uuid_is_preferred_to_cuda_logical_index(self):
        with patch.dict('os.environ', {'NVIDIA_VISIBLE_DEVICES': 'GPU-allocated'}, clear=True):
            self.assertEqual(GpuMonitor(Path('/tmp'), '0').selector, 'GPU-allocated')


if __name__ == '__main__':
    unittest.main()
