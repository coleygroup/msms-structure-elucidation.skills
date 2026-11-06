import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class SpectrumQCTests(unittest.TestCase):
    def test_precursor_is_excluded_before_base_peak_and_threshold_is_inclusive(self):
        from msms_structure_elucidation.visualize import spectrum_qc
        result = {'parentmass': 500, 'spectra': {'10': [
            [500, 10000], [499.995, 5000], [100, 100], [120, 10],
            [140, 10], [160, 10], [180, 10], [200, 9.999]]}}
        before = json.dumps(result)
        qc = spectrum_qc(result)
        self.assertTrue(qc['precursor_known'])
        self.assertEqual(sum(v >= 10 for v in qc['relative_intensities']), 5)
        self.assertEqual(sum(v >= 100 for v in qc['relative_intensities']), 1)
        self.assertEqual(len(qc['relative_intensities']), 6)
        self.assertEqual(json.dumps(result), before)

    def test_repeated_fragments_merge_once_across_energies_before_normalization(self):
        from msms_structure_elucidation.visualize import spectrum_qc
        result = {'parentmass': 500, 'spectra': {
            '10': [[100, 100], [120, 6]], '20': [[100.001, 100], [140, 20]]}}
        qc = spectrum_qc(result)
        self.assertEqual(qc['relative_intensities'], [100, 10, 3])
        stepped = {'parentmass': 500, 'spectra': {'stepped': [[100, 200], [140, 20], [120, 6]]}}
        self.assertEqual(spectrum_qc(stepped), qc)

    def test_empty_precursor_only_invalid_and_missing_mass(self):
        from msms_structure_elucidation.visualize import spectrum_qc
        for spectra in ({}, {'10': []}, {'10': [[500, 100]]},
                        {'10': [[100, 0], [120, -1], [-1, 100], [float('nan'), 1], [140, float('inf')]]}):
            self.assertEqual(spectrum_qc({'parentmass': 500, 'spectra': spectra}),
                             {'relative_intensities': [], 'precursor_known': True})
        for mass in (None, 0, -1, float('nan'), float('inf')):
            self.assertEqual(spectrum_qc({'parentmass': mass, 'spectra': {'10': [[100, 1]]}}),
                             {'relative_intensities': [], 'precursor_known': False})


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
        for asset in ('viewer.js', 'viewer_merge.js', 'viewer.css'):
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

    def test_several_results_share_one_page_with_separate_notes(self):
        from msms_structure_elucidation.visualize import create_app
        second_dir = self.root / 'feature_2'
        second_dir.mkdir()
        second = second_dir / 'retrieval.json'
        source = json.loads(self.result.read_text())
        source['candidates'][0].update(inchikey='SECOND-KEY', entropy_similarity=0.8)
        second.write_text(json.dumps(source))
        empty_dir = self.root / 'feature_3'
        empty_dir.mkdir()
        (empty_dir / 'retrieval.json').write_text(json.dumps({**source, 'candidates': [], 'formula': None}))
        client = create_app([self.result, second, empty_dir / 'retrieval.json']).test_client()
        index = client.get('/api/index').json['results']
        self.assertEqual([r['label'] for r in index], [self.root.name, 'feature_2', 'feature_3'])
        self.assertEqual(index[1]['top_similarity'], 0.8)
        self.assertEqual(index[2]['candidates'], 0)
        state = client.get('/api/state?result=1').json
        self.assertEqual(state['result']['candidates'][0]['review_key'], 'inchikey:SECOND-KEY')
        self.assertEqual(client.get('/api/state?result=3').status_code, 404)
        self.assertEqual(client.get('/api/fragment/1/0/10/0').status_code, 200)
        body = {'result': 1, 'candidate_key': 'inchikey:SECOND-KEY', 'decision': 'reject', 'comment': ''}
        response = client.post('/api/review', json=body, headers={'X-Review-Token': state['review_token']})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['reviewed'], 1)
        self.assertTrue((second_dir / 'review_notes.json').exists())
        self.assertFalse((self.root / 'review_notes.json').exists())
        wrong = {**body, 'result': 0}
        self.assertEqual(client.post('/api/review', json=wrong,
            headers={'X-Review-Token': state['review_token']}).status_code, 400)

    def test_candidate_payload_thumbnail_and_index_fields(self):
        payload = self.client.get('/api/candidate/0/0')
        self.assertEqual(payload.status_code, 200, payload.json)
        self.assertIn('<svg', payload.json['svg'])
        self.assertNotIn('#000000', payload.json['svg'])
        self.assertEqual(len(payload.json['peaks']['10']), 1)
        self.assertEqual(set(payload.json['fragments']['1']), {'a', 'b'})
        self.assertEqual(self.client.get('/api/candidate/0/1').status_code, 404)
        thumb = self.client.get('/api/thumb/0')
        self.assertEqual(thumb.status_code, 200)
        self.assertIn(b'<svg', thumb.data)
        index = self.client.get('/api/index').json
        self.assertEqual(index['results'][0]['outcome'], 'model')
        self.assertEqual(index['results'][0]['top_model'], 'ICEBERG')
        from msms_structure_elucidation.visualize import model_label
        self.assertEqual(model_label({'source': 'ICEBERG', 'model_name': 'ICEBERG', 'model_version': '2.1'}), 'ICEBERG 2.1')
        self.assertIsNone(model_label({'source': 'public ICEBERG 2.1 PubChem atlas'}))
        self.assertTrue(index['review_token'])
        state = self.client.get('/api/state').json['result']
        self.assertEqual(list(state['candidates'][0]['predicted_spectra']), ['10'])

    def test_no_candidates_keeps_experimental_spectra_and_stepped_metadata(self):
        from msms_structure_elucidation.visualize import create_app, export_static
        source = json.loads(self.result.read_text())
        source.update(candidates=[], status='no_candidates', acquisition_mode='stepped',
                      collision_unit='NCE', spectra={'stepped': [[31.0, 10.0], [40.0, 3.0]]},
                      stepped_collision={'values': [20, 40, 60], 'unit': 'NCE'},
                      energy_mapping=[{'input_value': v, 'input_unit': 'NCE',
                                       'acquisition_mode': 'stepped'} for v in (20, 40, 60)])
        self.result.write_text(json.dumps(source))
        client = create_app(self.result).test_client()
        self.assertEqual(client.get('/api/index').json['results'][0]['outcome'], 'no-candidates')
        live = client.get('/api/state').json['result']
        page = export_static([self.result], self.root / 'empty.html').read_text()
        data = json.loads(page.split('window.MSMS_STATIC=', 1)[1].split(';</script>', 1)[0])
        for result in (live, data['results'][0]['result']):
            self.assertEqual(result['candidates'], [])
            self.assertEqual(result['spectra'], source['spectra'])
            self.assertEqual(result['acquisition_mode'], 'stepped')
            self.assertEqual(result['stepped_collision'], source['stepped_collision'])
            self.assertEqual(result['energy_mapping'], source['energy_mapping'])
        self.assertEqual(data['results'][0]['structures'], [])

    def test_live_and_export_qc_use_full_spectra_before_display_trimming(self):
        from msms_structure_elucidation.visualize import create_app, export_static
        source = json.loads(self.result.read_text())
        source.update(parentmass=1000, candidates=[], spectra={'10': [
            [1000, 10000], [50, 100], *[[float(mz), 10] for mz in range(51, 301)]]})
        self.result.write_text(json.dumps(source))
        before = self.result.read_bytes()
        client = create_app(self.result).test_client()
        live = client.get('/api/index').json['results'][0]['spectrum_qc']
        out = export_static([self.result], self.root / 'qc.html', max_exp_peaks=2)
        data = json.loads(out.read_text().split('window.MSMS_STATIC=', 1)[1].split(';</script>', 1)[0])
        self.assertEqual(data['index'][0]['spectrum_qc'], live)
        self.assertEqual(len(live['relative_intensities']), 251)
        self.assertEqual(sum(v >= 10 for v in live['relative_intensities']), 251)
        self.assertEqual(len(data['results'][0]['result']['spectra']['10']), 2)
        self.assertEqual(self.result.read_bytes(), before)

    def test_csv_rankings_keep_top_ten_when_report_shows_only_five(self):
        from unittest.mock import patch
        from msms_structure_elucidation.visualize import create_app, export_static
        source = json.loads(self.result.read_text())
        expected = [{'smiles': 'C' * (i + 1), 'entropy_similarity': 0.9876543210123456 - i / 100}
                    for i in range(12)]
        expected[0]['entropy_similarity'] = 0
        source['candidates'] = [{**source['candidates'][0], **candidate, 'inchikey': f'KEY-{i}'}
                                for i, candidate in enumerate(expected)]
        self.result.write_text(json.dumps(source))
        before = self.result.read_bytes()
        live = create_app(self.result).test_client().get('/api/index').json['results'][0]
        with patch('msms_structure_elucidation.visualize.saved_candidate_payload', return_value={}):
            page = export_static([self.result], self.root / 'rankings.html', top_k=5).read_text()
        data = json.loads(page.split('window.MSMS_STATIC=', 1)[1].split(';</script>', 1)[0])
        self.assertEqual(live['ranked_candidates'], expected[:10])
        self.assertEqual(data['index'][0]['ranked_candidates'], expected[:10])
        self.assertEqual(len(data['results'][0]['result']['candidates']), 5)
        self.assertEqual(live['label'], data['index'][0]['label'])
        self.assertEqual(self.result.read_bytes(), before)

    def test_static_export_is_self_contained(self):
        from msms_structure_elucidation.visualize import export_static
        source = json.loads(self.result.read_text())
        source['candidates'][0]['smiles'] = 'CC</script>O'
        (self.root / 'odd').mkdir()
        odd = self.root / 'odd' / 'retrieval.json'
        odd.write_text(json.dumps(source))
        out = export_static([self.result, odd], self.root / 'report.html')
        page = out.read_text()
        self.assertNotIn('/assets/viewer.js', page)
        self.assertNotIn('/assets/viewer_merge.js', page)
        self.assertIn('window.MSMS_STATIC=', page)
        self.assertIn('root.MSMSMerge = api', page)
        data = page.split('window.MSMS_STATIC=', 1)[1].split(';</script>', 1)[0]
        self.assertNotIn('</script>', data)
        parsed = json.loads(data.replace('<\\/', '</'))
        self.assertEqual([r['label'] for r in parsed['index']], [self.root.name, 'odd'])
        self.assertEqual(parsed['results'][1]['result']['candidates'][0]['smiles'], 'CC</script>O')

    def test_static_export_with_separate_data_and_demo_reviews(self):
        import gzip
        from msms_structure_elucidation.visualize import export_static
        out = export_static([self.result], self.root / 'site' / 'review.html', split_data=True,
                            demo_reviews=True, title='Demo Review')
        page = out.read_text()
        self.assertIn('<title>Demo Review</title>', page)
        self.assertIn('window.MSMS_STATIC_SRC="review.data.json.gz"', page)
        self.assertNotIn('window.MSMS_STATIC=', page)
        data = json.loads(gzip.decompress((self.root / 'site' / 'review.data.json.gz').read_bytes()))
        self.assertTrue(data['demo_reviews'])
        self.assertEqual(len(data['results']), 1)

    def test_public_demo_export_hides_local_paths_and_keeps_fragments(self):
        from msms_structure_elucidation.visualize import export_static
        source = json.loads(self.result.read_text())
        source['input'] = str(self.root / 'private' / 'query.ms')
        source['formula_results'] = [{'formula': 'C2H6O', 'atlas_mgf': str(self.root / 'atlas.mgf')}]
        self.result.write_text(json.dumps(source))
        out = export_static([self.result], self.root / 'public.html', demo_reviews=True,
                            demo_source_url='https://example.org/query.ms',
                            demo_install_url='https://example.org/skills')
        page = out.read_text()
        self.assertNotIn(str(self.root), page)
        data = json.loads(page.split('window.MSMS_STATIC=', 1)[1].split(';</script>', 1)[0])
        self.assertEqual(data['demo_source_url'], 'https://example.org/query.ms')
        self.assertEqual(data['results'][0]['result']['input'], 'query.ms')
        self.assertNotIn('atlas_mgf', data['results'][0]['result']['formula_results'][0])
        self.assertEqual(len(data['results'][0]['structures'][0]['peaks']['10']), 1)
        self.assertTrue(data['results'][0]['structures'][0]['fragments'])

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
