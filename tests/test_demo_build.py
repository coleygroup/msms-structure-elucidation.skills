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
NAMES = ['plasma_unknown_583', 'csf_unknown', 'plasma_unknown_198']


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
        self.assertEqual([row['name'] for row in manifest['unknowns']], NAMES)
        self.assertTrue(all(row['user_confirmed_collision_unit'] == 'NCE' for row in manifest['unknowns']))
        self.assertEqual([row['model_energies_ev'] for row in manifest['unknowns']],
                         [[11, 22, 32, 43, 54], [5, 10, 16, 21, 26], [4, 8, 12, 16, 20]])
        self.assertEqual([row['ranked_candidates'] for row in manifest['unknowns']], [20, 20, 20])
        self.assertEqual(manifest['html_sha256'], hashlib.sha256(page.encode()).hexdigest())
        match = re.search(r'<script>window\.MSMS_STATIC=(.*?);</script>', page, re.S)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1))
        self.assertEqual([row['label'] for row in data['index']], NAMES)
        self.assertEqual(len(data['demo_source_urls']), 3)
        self.assertEqual(len(data['results']), 3)
        for row in data['results']:
            self.assertEqual(len(row['structures']), 20)
            self.assertTrue(row['structures'][0]['fragments'])
            self.assertEqual(len(row['result']['energy_mapping']), 5)
        self.assertNotIn('/home/', page)
        self.assertNotIn('/mnt/', page)

    def test_snapshot_contains_only_public_sources(self):
        with gzip.open(SNAPSHOT, 'rt', encoding='utf-8') as stream:
            data = json.load(stream)
        self.assertTrue(data['demo_reviews'])
        candidates = [candidate for row in data['results'] for candidate in row['result']['candidates']]
        self.assertTrue(all(c['source'].startswith('public ICEBERG') for c in candidates))


if __name__ == '__main__':
    unittest.main()
