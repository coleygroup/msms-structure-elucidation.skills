import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation import cli
from msms_structure_elucidation.atlas import download_mgf
from msms_structure_elucidation.atlas import AtlasNoEntry
from msms_structure_elucidation.spectrum import inspect_ms


class WorkflowTests(unittest.TestCase):
    def test_ms_preflight_and_report_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'query.ms'
            spec.write_text('>compound unknown\n>parentmass 101.0\n>ionization [M+H]+\n\n>collision 20 eV\n50.0 12\n75.0 4\n')
            mgf = root / 'library.mgf'
            mgf.write_text('BEGIN IONS\nSMILES=C\nCOLLISION_ENERGY=20\n50.0 12\nEND IONS\n')
            parsed = inspect_ms(spec, 'eV')
            self.assertEqual((parsed['peaks'], list(parsed['spectra'])), (2, ['20.0']))
            candidate = {'smiles': 'C', 'formula': 'CH4', 'source': 'public ICEBERG 2.1 PubChem atlas',
                'entropy_similarity': 0.65, 'explained_intensity': 0.75,
                'matched_peaks': [{'ce': '20.0', 'mz': 50.0, 'predicted_mz': 50.0}],
                'energy_alignment': [{'experimental_key': '20.0', 'atlas_key': '20',
                    'input_value': 20.0, 'input_unit': 'eV', 'experimental_ev': 20.0,
                    'atlas_ev': 20.0, 'delta_ev': 0.0}],
                'predicted_spectra': {'20': [[50.0, 12.0]]}}
            args = SimpleNamespace(input=str(spec), output_dir=str(root / 'output'), formula='CH4',
                ms_pred_python='python', atlas_mgf=str(mgf), atlas_url='https://example.test',
                top_k=10, no_report=False, collision_unit='eV')
            def fake_worker(_, action, **kw):
                if action == 'atlas-info':
                    return {'library_structures': 1, 'missing_energies_ev': [], 'smiles': ['C']}
                return {'library_structures': 1, 'scored_structures': 1, 'candidates': [candidate]}
            with patch.object(cli, 'worker', side_effect=fake_worker):
                self.assertEqual(cli.run(args), 0)
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertEqual(data['review_evidence']['strongest_unexplained_peaks'][0]['mz'], 75.0)
            self.assertTrue(data['review_evidence']['weak_match_review_suggested'])
            report = (root / 'output/report.html').read_text()
            self.assertIn('Download SVG', report)
            self.assertIn('Download PNG', report)
            self.assertIn('50.0', report)

    def test_reject_html_atlas_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            class FakeResponse:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, size): return b'<html>formula absent</html>'
            with patch('msms_structure_elucidation.atlas.urlopen', return_value=FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, 'non-MGF'):
                    download_mgf('C2H6O', '[M+H]+', Path(tmp) / 'atlas.mgf')
            self.assertFalse((Path(tmp) / 'atlas.mgf').exists())

    def test_reject_invalid_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.ms'
            path.write_text('>parentmass 100\n>collision 20 eV\n50 nan\n')
            with self.assertRaisesRegex(ValueError, 'invalid peak'):
                inspect_ms(path, 'eV')

    def test_packaged_default_config_matches_checkout(self):
        from msms_structure_elucidation.config import settings
        import msms_structure_elucidation.config as config_module
        checkout = settings(str(config_module.REPO_CONFIG))
        bundled = settings(str(Path(config_module.__file__).with_name('default.yaml')))
        checkout.pop('_config_path'); bundled.pop('_config_path')
        self.assertEqual(checkout, bundled)

    def test_user_confirmed_nce_overrides_incorrect_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'query.ms'
            path.write_text('>parentmass 260.1715\n>collision 20 eV\n50 10\n')
            data = inspect_ms(path, 'NCE')
            self.assertEqual(data['header_units'], ['eV'])
            self.assertAlmostEqual(data['energy_mapping'][0]['collision_energy_ev'], 10.40686)
            self.assertIn('10.40686', data['spectra'])

    def test_all_nce_energies_align_to_atlas(self):
        from msms_structure_elucidation.worker import align_energies
        precursor = 260.1715
        experimental = {str(nce*precursor/500): None for nce in (10, 20, 30, 40, 50)}
        source = {str(nce*precursor/500): float(nce) for nce in (10, 20, 30, 40, 50)}
        predicted = {str(ev): None for ev in (3, 5, 8, 10, 13, 16, 18, 21, 23, 26)}
        pairs = align_energies(experimental, predicted, source, 'NCE')
        self.assertEqual([p['atlas_ev'] for p in pairs], [5, 10, 16, 21, 26])
        self.assertEqual(len(pairs), 5)
        self.assertEqual(align_energies({'20.1': None}, {'22': None}, {'20.1': 20.1}, 'eV'), [])

    def test_model_uses_integer_ev_for_both_simulators(self):
        from msms_structure_elucidation import worker
        precursor = 260.1715
        converted = {str(round(nce*precursor/500, 8)): None
                     for nce in (10, 20, 30, 40, 50)}
        source = {key: float(nce) for key, nce in zip(converted, (10, 20, 30, 40, 50))}
        self.assertEqual(worker.model_collision_energies_ev(converted), [5, 10, 16, 21, 26])
        self.assertEqual(worker.model_collision_energies_ev({'20.2': None, '20.4': None,
                                                             '21.6': None}), [20, 22])
        duplicate_pairs = worker.align_energies({'20.2': None, '20.4': None},
            {'20': None}, {'20.2': 20.2, '20.4': 20.4}, 'eV')
        self.assertEqual(len(duplicate_pairs), 2)
        captured = []
        modules = {}
        for name in ('ms_pred', 'ms_pred.common', 'ms_pred.glacier', 'ms_pred.iceberg'):
            module = types.ModuleType(name)
            module.__path__ = []
            modules[name] = module
        class EmptyPredDB:
            def __init__(self, *_): pass
            def get_all_specs(self): return []
            def close(self): pass
        modules['ms_pred.common'].PredSpecDB = EmptyPredDB
        for model in ('glacier', 'iceberg'):
            module = types.ModuleType(f'ms_pred.{model}.{model}_elucidation')
            def predict(**kwargs):
                captured.append(kwargs)
                model_name = 'glacier' if 'ckpt' in kwargs else 'iceberg'
                (Path(tmp) / f'{model_name}_run_successful').touch()
                (Path(tmp) / 'preds.hdf5').touch()
                return (tmp, None)
            setattr(module, f'{model}_prediction', predict)
            modules[module.__name__] = module
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'weights.ckpt'
            checkpoint.touch()
            with patch.dict(sys.modules, modules), patch.object(worker, '_check_glacier_features'), patch.object(worker, '_experimental_spectra',
                    return_value=({'parentmass': str(precursor), 'ionization': '[M+H]+'},
                                  converted, source)):
                for model in ('glacier', 'iceberg'):
                    self.assertEqual(worker.simulate('unused.ms', [], 'CH4', model,
                        str(checkpoint), str(checkpoint), str(checkpoint), tmp, 'NCE',
                        instrument='QTOF', cuda_devices='0'), [])
        self.assertEqual(len(captured), 2)
        for call in captured:
            self.assertEqual(call['collision_energies'], [5, 10, 16, 21, 26])
            self.assertIs(call['nce'], False)
            self.assertEqual(call['instrument'], 'QTOF')
            self.assertEqual(call['cuda_devices'], '0')
            self.assertTrue(all(type(value) is int for value in call['collision_energies']))

    def test_viewer_stops_cleanly_on_interrupt(self):
        args = SimpleNamespace(result='results/query/retrieval.json', atlas_mgf=None,
                               ms_pred_python='/path/to/model/python', port=0)
        with (patch.object(cli, 'model_python', return_value='/path/to/model/python'),
              patch.object(cli.subprocess, 'run', side_effect=KeyboardInterrupt)):
            self.assertEqual(cli.visualize(args), 0)

    def test_three_formula_hypotheses_are_all_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'query.ms'
            spec.write_text('>parentmass 100\n>collision 20 eV\n50 10\n')
            mgf = root / 'atlas.mgf'; mgf.write_text('BEGIN IONS\nEND IONS\n')
            ranked = []
            def fake_worker(_, action, **kw):
                if action == 'formula':
                    return [{'formula': f'F{i}'} for i in range(3)]
                if action == 'atlas-info':
                    return {'library_structures': 1, 'missing_energies_ev': [], 'smiles': ['C']}
                if action == 'rank':
                    ranked.append(kw['formula'])
                    i = int(kw['formula'][1])
                    return {'library_structures': 1, 'scored_structures': 1,
                        'missing_energies_ev': [], 'candidates': [{'smiles': f'C{i}',
                        'formula': kw['formula'], 'source': 'public ICEBERG 2.1 PubChem atlas',
                        'entropy_similarity': [0.1, 0.63, 0.2][i], 'explained_intensity': 0.5,
                        'matched_peaks': [], 'energy_alignment': [], 'predicted_spectra': {}}]}
                raise AssertionError(action)
            args = SimpleNamespace(input=str(spec), collision_unit='eV', output_dir=str(root / 'out'),
                formula=None, formulas_file=None, ms_pred_python='python', atlas_mgf=None,
                atlas_url=None, top_k=10, no_report=True)
            with patch.object(cli, 'worker', side_effect=fake_worker), patch.object(cli, 'download_mgf', return_value=mgf):
                self.assertEqual(cli.run(args), 0)
            result = json.loads((root / 'out/retrieval.json').read_text())
            self.assertEqual(ranked, ['F0', 'F1', 'F2'])
            self.assertEqual(result['formula'], 'F1')
            self.assertEqual(len(result['formula_results']), 3)

    def test_model_asset_block_is_fatal_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'query.ms'; spec.write_text('>parentmass 100\n>collision 20 eV\n50 10\n')
            mgf = root / 'atlas.mgf'; mgf.write_text('BEGIN IONS\nEND IONS\n')
            args = SimpleNamespace(input=str(spec), collision_unit='eV', output_dir=str(root / 'out'),
                formula='C2H4', formulas_file=None, ms_pred_python='python', atlas_mgf=str(mgf),
                atlas_url=None, top_k=10, no_report=True)
            with patch.object(cli, 'worker', return_value={'library_structures': 1,
                    'missing_energies_ev': [20], 'smiles': ['C']}):
                with self.assertRaisesRegex(RuntimeError, 'ms-pred checkout'):
                    cli.run(args)
            result = json.loads((root / 'out/retrieval.json').read_text())
            self.assertEqual(result['formula_results'][0]['status'], 'blocked_model_assets')
            self.assertEqual(result['status'], 'blocked_model_assets')

    def test_absent_atlas_is_valid_empty_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'query.ms'; spec.write_text('>parentmass 100\n>collision 20 eV\n50 10\n')
            args = SimpleNamespace(input=str(spec), collision_unit='eV', output_dir=str(root / 'out'),
                formula='C2H4', formulas_file=None, ms_pred_python='python', atlas_mgf=None,
                atlas_url=None, top_k=10, no_report=True)
            with patch.object(cli, 'download_mgf', side_effect=AtlasNoEntry('no public entry')):
                self.assertEqual(cli.run(args), 0)
            result = json.loads((root / 'out/retrieval.json').read_text())
            self.assertEqual(result['status'], 'no_atlas_coverage')

    def test_mgf_uses_ms2_scan_not_ms1_apex(self):
        from msms_structure_elucidation.mgf import convert_mgf
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgf = root / 'features.mgf'
            mgf.write_text('BEGIN IONS\nFEATURE_ID=1\nMSLEVEL=1\nSCANS=919\nPEPMASS=100\n50 1\nEND IONS\n'
                'BEGIN IONS\nFEATURE_ID=1\nMSLEVEL=2\nSCANS=919\nMERGED_SCANS=1280\nPEPMASS=100\n50 10\nEND IONS\n')
            raw = root / 'raw.mzXML'
            raw.write_text('<mzXML><msRun><scan num="919" msLevel="1"/><scan num="1280" msLevel="2" collisionEnergy="35"/></msRun></mzXML>')
            rows = convert_mgf(mgf, root / 'converted', 'eV', raw_mzxml=raw)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['collision_energy'], 35)
            self.assertEqual(rows[0]['source_scan'], '919')
            self.assertEqual(rows[0]['ms2_scans'], '1280')
            self.assertIn('>collision 35.0 eV', Path(rows[0]['ms_path']).read_text())

    def test_mgf_missing_energy_is_manifest_status(self):
        from msms_structure_elucidation.mgf import convert_mgf
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgf = root / 'features.mgf'
            mgf.write_text('BEGIN IONS\nFEATURE_ID=188\nMSLEVEL=2\nSOURCE_SCAN=919\nPEPMASS=300\n50 10\nEND IONS\n')
            rows = convert_mgf(mgf, root / 'converted', 'eV')
            self.assertEqual(rows[0]['status'], 'needs_energy')
            self.assertFalse(list((root / 'converted').glob('*.ms')))

    def test_review_model_failure_leaves_result_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / 'retrieval.json'
            original = {'input': str(root / 'query.ms'), 'formula': 'CH4', 'collision_unit': 'eV',
                'candidates': [], 'spectra': {'20.0': [[10, 1]]}}
            result_path.write_text(json.dumps(original))
            proposals = root / 'proposals.json'; proposals.write_text('["C"]')
            checkout = root / 'ms-pred'; (checkout / 'src/ms_pred').mkdir(parents=True)
            def fake_worker(_, action, **kw):
                if action == 'validate':
                    return [{'smiles': 'C', 'formula_match': True}]
                raise RuntimeError('model subprocess crashed: incompatible features')
            args = SimpleNamespace(result=str(result_path), smiles_json=str(proposals),
                ms_pred_python='python', ms_pred_dir=str(checkout), model='glacier',
                checkpoint=None, gen_checkpoint=None, inten_checkpoint=None,
                instrument=None, cuda_devices=None, model_batch_size=None, formula=None, config=None)
            with patch.object(cli, 'worker', side_effect=fake_worker):
                with self.assertRaisesRegex(RuntimeError, 'incompatible features'):
                    cli.review(args)
            self.assertEqual(json.loads(result_path.read_text()), original)

    def test_energy_gap_simulates_every_structure_in_resumable_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / 'query.ms'; spec.write_text('>parentmass 700\n>collision 20 eV\n50 1\n')
            mgf = root / 'atlas.mgf'; mgf.write_text('BEGIN IONS\nEND IONS\n')
            checkout = root / 'ms-pred'; (checkout / 'src/ms_pred').mkdir(parents=True)
            gen = root / 'gen.ckpt'; gen.touch()
            inten = root / 'inten.ckpt'; inten.touch()
            options = {'model': 'iceberg', 'ms_pred_dir': str(checkout), 'checkpoint': None,
                'gen_checkpoint': str(gen), 'inten_checkpoint': str(inten),
                'cuda_devices': None, 'batch_size': 1}
            calls = []
            def fake_worker(_, action, **kwargs):
                if action == 'atlas-smiles':
                    return ['C', 'CC', 'CCC']
                self.assertEqual(action, 'simulate')
                chunk = json.loads(Path(kwargs['smiles_json']).read_text())
                calls.extend(chunk)
                self.assertEqual(kwargs['instrument'], 'QTOF')
                return [{'smiles': s, 'formula': 'C3H8', 'entropy_similarity': 0.1 * len(s),
                    'explained_intensity': 0.5, 'matched_peaks': []} for s in chunk]
            with patch.object(cli, 'worker', side_effect=fake_worker):
                first, count = cli._simulate_shards('python', spec, mgf, 'C3H8', 'eV', root,
                    options, 'QTOF', 2, 2)
                second, count2 = cli._simulate_shards('python', spec, mgf, 'C3H8', 'eV', root,
                    options, 'QTOF', 2, 2)
            self.assertEqual(calls, ['C', 'CC', 'CCC'])
            self.assertEqual((count, count2), (3, 3))
            self.assertEqual([x['smiles'] for x in first], [x['smiles'] for x in second])
            self.assertEqual(len(list((root / 'model_shards/C3H8').glob('*.json'))), 4)

    def test_fragment_ids_are_peak_aligned_and_preserve_large_values(self):
        from msms_structure_elucidation.worker import _serial_fragment_ids
        spec = SimpleNamespace(masses=[31.0, 45.0], intens=[0.5, 1.0])
        record = {'10': ([[31.0, 0.5], [45.0, 1.0]],
                         ['9007199254740993', '3'])}
        self.assertEqual(_serial_fragment_ids({'10': spec}, record)['10'][0],
                         '9007199254740993')
        wrong = {'10': ([[31.0, 0.5], [46.0, 1.0]], ['1', '3'])}
        self.assertEqual(_serial_fragment_ids({'10': spec}, wrong), {})


if __name__ == '__main__':
    unittest.main()
