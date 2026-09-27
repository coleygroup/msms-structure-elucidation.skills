"""Check that the committed analysis snapshot builds a complete public demo."""
import gzip
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'demo/msms-structure-elucidation/source.json.gz'


class DemoBuildTests(unittest.TestCase):
    def test_site_build_uses_current_viewer_and_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / 'index.html'
            subprocess.run(
                [sys.executable, str(ROOT / 'scripts/build_demo.py'),
                 '--snapshot', str(SNAPSHOT), '--output', str(output)],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )
            page = output.read_text()
            manifest = json.loads(output.with_name('manifest.json').read_text())
        self.assertIn('window.MSMS_STATIC=', page)
        self.assertIn('click', page)
        self.assertEqual(manifest['user_confirmed_collision_unit'], 'NCE')
        self.assertEqual(manifest['model_energies_ev'], [11, 22, 32, 43, 54])
        self.assertEqual(manifest['ranked_candidates'], 20)
        self.assertEqual(manifest['html_sha256'], hashlib.sha256(page.encode()).hexdigest())
        match = re.search(r'<script>window\.MSMS_STATIC=(.*?);</script>', page, re.S)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1))
        self.assertEqual(len(data['results'][0]['structures']), 20)
        self.assertTrue(data['results'][0]['structures'][0]['fragments'])
        self.assertNotIn('/home/', page)
        self.assertNotIn('/mnt/', page)

    def test_snapshot_contains_only_public_sources(self):
        with gzip.open(SNAPSHOT, 'rt', encoding='utf-8') as stream:
            data = json.load(stream)
        self.assertTrue(data['demo_reviews'])
        candidates = data['results'][0]['result']['candidates']
        self.assertTrue(all(c['source'].startswith('public ICEBERG') for c in candidates))


if __name__ == '__main__':
    unittest.main()
