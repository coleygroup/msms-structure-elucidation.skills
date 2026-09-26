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
            with patch.object(cli, 'worker', return_value={'library_structures': 1,
                    'scored_structures': 1, 'candidates': [candidate]}):
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
                with self.assertRaisesRegex(ValueError, 'no MGF'):
                    download_mgf('C2H6O', '[M+H]+', Path(tmp) / 'atlas.mgf')
            self.assertFalse((Path(tmp) / 'atlas.mgf').exists())

    def test_reject_invalid_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.ms'
            path.write_text('>parentmass 100\n>collision 20 eV\n50 nan\n')
            with self.assertRaisesRegex(ValueError, 'invalid peak'):
                inspect_ms(path, 'eV')

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
        for name in ('ms_pred', 'ms_pred.glacier', 'ms_pred.iceberg'):
            module = types.ModuleType(name)
            module.__path__ = []
            modules[name] = module
        for model in ('glacier', 'iceberg'):
            module = types.ModuleType(f'ms_pred.{model}.{model}_elucidation')
            def predict(**kwargs):
                captured.append(kwargs)
                return ('unused', None)
            setattr(module, f'{model}_prediction', predict)
            module.load_pred_spec = lambda _: ([], [])
            modules[module.__name__] = module
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'weights.ckpt'
            checkpoint.touch()
            with patch.dict(sys.modules, modules), patch.object(worker, '_experimental_spectra',
                    return_value=({'parentmass': str(precursor), 'ionization': '[M+H]+'},
                                  converted, source)):
                for model in ('glacier', 'iceberg'):
                    self.assertEqual(worker.simulate('unused.ms', ['C'], 'CH4', model,
                        str(checkpoint), str(checkpoint), str(checkpoint), tmp, 'NCE'), [])
        self.assertEqual(len(captured), 2)
        for call in captured:
            self.assertEqual(call['collision_energies'], [5, 10, 16, 21, 26])
            self.assertIs(call['nce'], False)
            self.assertTrue(all(type(value) is int for value in call['collision_energies']))

    def test_viewer_stops_cleanly_on_interrupt(self):
        args = SimpleNamespace(result='results/query/retrieval.json', atlas_mgf=None,
                               ms_pred_python='/path/to/model/python', port=0)
        with (patch.object(cli, 'model_python', return_value='/path/to/model/python'),
              patch.object(cli.subprocess, 'run', side_effect=KeyboardInterrupt)):
            self.assertEqual(cli.visualize(args), 0)

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
