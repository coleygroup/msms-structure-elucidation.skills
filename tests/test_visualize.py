import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec('flask') and importlib.util.find_spec('ms_pred'),
                     'visualization extra and ms-pred are required')
class VisualizeTests(unittest.TestCase):
    def setUp(self):
        from msms_structure_elucidation.visualize import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result = self.root / 'retrieval.json'
        self.result.write_text(json.dumps({
            'input': 'query.ms', 'parentmass': 47.049, 'adduct': '[M+H]+',
            'formula': 'C2H6O', 'collision_unit': 'eV',
            'spectra': {'10.0': [[31.0, 10.0]]},
            'candidates': [{
                'smiles': 'CCO', 'canonical_smiles': 'CCO', 'inchikey': 'TEST-KEY',
                'source': 'ICEBERG', 'formula': 'C2H6O',
                'entropy_similarity': 0.5, 'explained_intensity': 0.5,
                'predicted_spectra': {'10': [[31.0, 1.0]]},
                'predicted_fragment_ids': {'10': ['1']},
                'energy_alignment': [{'experimental_key': '10.0', 'atlas_key': '10',
                    'input_value': 10.0, 'input_unit': 'eV', 'experimental_ev': 10.0,
                    'atlas_ev': 10.0, 'entropy_similarity': 0.5}],
                'matched_peaks': [{'ce': '10.0', 'mz': 31.0, 'predicted_mz': 31.0}]
            }]
        }))
        self.app = create_app(self.result)
        self.client = self.app.test_client()

    def test_local_viewer_and_fragment_drawing(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Interactive fragment review', response.data)
        response.close()
        for asset in ('viewer.js', 'viewer.css'):
            response = self.client.get('/assets/' + asset)
            self.assertEqual(response.status_code, 200)
            response.close()
        response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['result']['candidates'][0]['review_key'], 'inchikey:TEST-KEY')
        fragment = self.client.get('/api/fragment/0/10/0')
        self.assertEqual(fragment.status_code, 200, fragment.json)
        self.assertIn('<svg', fragment.json['svg'])
        self.assertEqual(fragment.json['fragment_id'], '1')
        self.assertEqual(self.client.get('/api/fragment/0/10/1').status_code, 404)

    def test_review_is_saved_separately_and_reloaded(self):
        state = self.client.get('/api/state').json
        body = {'candidate_key': 'inchikey:TEST-KEY', 'decision': 'keep',
                'comment': 'Supports the main peaks',
                'fragment': {'ce': '10', 'peak_index': 0, 'comment': 'Plausible loss'}}
        self.assertEqual(self.client.post('/api/review', json=body).status_code, 403)
        response = self.client.post('/api/review', json=body,
            headers={'X-Review-Token': state['review_token']})
        self.assertEqual(response.status_code, 200, response.json)
        notes = json.loads((self.root / 'review_notes.json').read_text())
        entry = notes['candidates']['inchikey:TEST-KEY']
        self.assertEqual(entry['decision'], 'keep')
        self.assertEqual(entry['fragments']['10:0']['comment'], 'Plausible loss')
        self.assertNotIn('review', json.loads(self.result.read_text()))
        self.assertEqual(self.client.get('/api/state').json['notes'], notes)
        bad = {**body, 'fragment': {'ce': '10', 'peak_index': 2, 'comment': 'bad'}}
        self.assertEqual(self.client.post('/api/review', json=bad,
            headers={'X-Review-Token': state['review_token']}).status_code, 400)

    def test_legacy_atlas_backfill_requires_aligned_peaks(self):
        from msms_structure_elucidation.visualize import load_result
        source = json.loads(self.result.read_text())
        del source['candidates'][0]['predicted_fragment_ids']
        self.result.write_text(json.dumps(source))
        mgf = self.root / 'atlas.mgf'
        mgf.write_text('BEGIN IONS\nSMILES=CCO\nINCHIKEY=TEST-KEY\n'
                       'COLLISION_ENERGY=10.0\nFRAGS=9007199254740993\n'
                       '31.0 1.0\nEND IONS\n')
        loaded = load_result(self.result, mgf)
        self.assertEqual(loaded['candidates'][0]['predicted_fragment_ids']['10'],
                         ['9007199254740993'])
        mgf.write_text(mgf.read_text().replace('31.0 1.0', '32.0 1.0'))
        loaded = load_result(self.result, mgf)
        self.assertNotIn('10', loaded['candidates'][0].get('predicted_fragment_ids', {}))


if __name__ == '__main__':
    unittest.main()
