"""Stepped acquisition semantics, lossless conversion and metadata evidence."""
import contextlib
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from msms_structure_elucidation import worker
from msms_structure_elucidation.collision import energy_values, stepped_components
from msms_structure_elucidation.energy_metadata import inspect_energy
from msms_structure_elucidation.mgf import convert_mgf
from msms_structure_elucidation.spectrum import inspect_ms


class EnergyInputTests(unittest.TestCase):
    def test_explicit_mean_policy_and_json_metadata_preserve_repeated_components(self):
        with patch('msms_structure_elucidation.config.settings', return_value={
                'pipeline': {'stepped_aggregation': 'arithmetic_mean'}}):
            info = stepped_components('[20,20,40]', 250, 'NCE')
        self.assertEqual(info['merge_method'], 'arithmetic_mean')
        self.assertEqual(info['model_energies_ev'], [10, 10, 20])
        self.assertEqual(info['weights'], [1 / 3] * 3)
        for value in ['[true,40]', '["20",40]']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                energy_values(value)
        with patch('msms_structure_elucidation.config.settings', return_value={
                'pipeline': {'stepped_aggregation': 'unknown'}}), self.assertRaises(ValueError):
            stepped_components([20,40], 250, 'NCE')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ms(self, header='20,40,60', blocks='>collision 20 NCE\n50 10\n60 3\n'):
        path = self.root / 'sample.ms'
        path.write_text(f'>compound query\n>parentmass 250\n>ionization [M+H]+\n'
                        f'>stepped_collision_energies {header}\n\n{blocks}')
        return path

    def mzml(self, activation, values=None):
        path = self.root / 'sample.mzML'
        scans = []
        for index, value in enumerate(values or ['unused'], 1):
            scans.append(f'<spectrum id="controllerType=0 scan={index}">'
                         '<cvParam accession="MS:1000511" value="2"/>'
                         '<scanList><scan><cvParam accession="MS:1000016" value="2" '
                         'unitAccession="UO:0000031"/></scan></scanList>'
                         '<precursorList><precursor><selectedIonList><selectedIon>'
                         '<cvParam accession="MS:1000744" value="250"/></selectedIon>'
                         '</selectedIonList><activation>' + activation.replace('VALUE', value)
                         + '</activation></precursor></precursorList></spectrum>')
        path.write_text('<mzML xmlns="http://psi.hupo.org/ms/mzml"><run><spectrumList>'
                        + ''.join(scans) + '</spectrumList></run></mzML>')
        return path

    def test_stepped_preflight_preserves_one_observation_and_all_settings(self):
        result = inspect_ms(self.ms(), 'NCE')
        self.assertEqual(result['acquisition_mode'], 'stepped')
        self.assertEqual(list(result['spectra']), ['stepped'])
        self.assertEqual([row['model_energy_ev'] for row in result['energy_mapping']], [10, 20, 30])
        self.assertEqual(result['stepped_collision']['values'], [20, 40, 60])
        source = {'stepped': result['stepped_collision']}
        self.assertEqual(worker.model_collision_energies_ev(result['spectra'], source), [10, 20, 30])

    def test_stepped_input_rejects_multiple_observations_and_center_spread(self):
        with self.assertRaisesRegex(ValueError, 'one measured'):
            inspect_ms(self.ms(blocks='>collision 20\n50 1\n\n>collision 40\n60 1\n'), 'NCE')
        for label in ['40 +/- 20', '20-60', '20,nan,60', '0,20,40']:
            with self.subTest(label=label), self.assertRaises(ValueError):
                inspect_ms(self.ms(header=label), 'NCE')

    def test_mgf_does_not_truncate_multi_energy_labels(self):
        path = self.root / 'features.mgf'
        path.write_text('BEGIN IONS\nFEATURE_ID=1\nPEPMASS=250\nCOLLISION_ENERGY=20,40,60 NCE\n50 10\nEND IONS\n')
        rows = convert_mgf(path, self.root / 'missing', 'NCE')
        self.assertEqual(rows[0]['status'], 'needs_energy')
        self.assertEqual(rows[0]['energy_source'], 'ambiguous_energy_setting')
        rows = convert_mgf(path, self.root / 'stepped', 'NCE', stepped_energies=[20, 40, 60])
        self.assertEqual(rows[0]['status'], 'ready')
        parsed = inspect_ms(Path(rows[0]['ms_path']), 'NCE')
        self.assertEqual(parsed['stepped_collision']['values'], [20, 40, 60])
        self.assertEqual(parsed['spectra']['stepped'], [[50, 10]])

    def test_mzml_normalized_energy_comes_from_activation_cv(self):
        path = self.mzml('<cvParam accession="MS:1000138" name="normalized collision energy" '
                          'value="40" unitAccession="UO:0000187"/>')
        result = inspect_energy(path, 250, 120)
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['settings'][0]['unit'], 'NCE')
        self.assertEqual(result['settings'][0]['energies'], [40])
        self.assertEqual(result['scans']['controllerType=0 scan=1']['rt'], 120)
        self.assertEqual(inspect_energy(path, 800)['status'], 'needs_user')

    def test_separate_scans_are_never_inferred_to_be_stepped(self):
        path = self.mzml('<cvParam accession="MS:1000138" name="normalized collision energy" '
                          'value="VALUE" unitAccession="UO:0000187"/>', ['20', '40', '60'])
        result = inspect_energy(path)
        self.assertEqual(len(result['settings']), 3)
        self.assertEqual({s['acquisition_mode'] for s in result['settings']}, {'separate'})

    def test_explicit_mzml_stepped_setting_reaches_mgf_conversion(self):
        raw = self.mzml('<userParam name="stepped normalized collision energy" value="20,40,60"/>')
        self.assertEqual(inspect_energy(raw)['settings'][0]['acquisition_mode'], 'stepped')
        mgf = self.root / 'features.mgf'
        mgf.write_text('BEGIN IONS\nFEATURE_ID=1\nMS2_SCAN=1\nPEPMASS=250\n50 10\nEND IONS\n')
        rows = convert_mgf(mgf, self.root / 'converted', 'NCE', raw_mzxml=raw)
        self.assertEqual(rows[0]['status'], 'ready')
        self.assertEqual(rows[0]['energy_source'], 'mzML:MS2_scan')
        self.assertEqual(inspect_ms(Path(rows[0]['ms_path']), 'NCE')['acquisition_mode'], 'stepped')

    def test_missing_units_conflicts_and_unknown_steps_need_user(self):
        for activation in ['<cvParam accession="MS:1000045" name="collision energy" value="40"/>',
                           '<cvParam accession="MS:1000138" name="normalized collision energy" value="40" unitAccession="UO:0000266"/>',
                           '<userParam name="stepped normalized collision energy" value="40 +/- 20"/>',
                           '<userParam name="stepped NCE" value="20,40,60"/>'
                           '<userParam name="stepped energy weights" value="1,2,1"/>',
                           '<cvParam accession="MS:1000138" name="normalized collision energy" value="40"/>'
                           '<cvParam accession="MS:1000045" name="collision energy" value="50" unitAccession="UO:0000266"/>']:
            with self.subTest(activation=activation):
                self.assertEqual(inspect_energy(self.mzml(activation))['status'], 'needs_user')
        raw = self.root / 'sample.raw'
        raw.write_bytes(b'proprietary raw fixture')
        self.assertEqual(inspect_energy(raw)['status'], 'needs_conversion_or_vendor_reader')

    def test_metadata_unit_conflict_blocks_conversion(self):
        raw = self.mzml('<cvParam accession="MS:1000138" name="normalized collision energy" value="40"/>')
        mgf = self.root / 'features.mgf'
        mgf.write_text('BEGIN IONS\nFEATURE_ID=1\nMS2_SCAN=1\nPEPMASS=250\n50 10\nEND IONS\n')
        row = convert_mgf(mgf, self.root / 'out', 'eV', raw_mzxml=raw)[0]
        self.assertEqual(row['status'], 'needs_energy')
        self.assertEqual(row['energy_source'], 'conflicting_collision_units')

    def test_mzml_reference_groups_keep_activation_units(self):
        path = self.mzml('<referenceableParamGroupRef ref="energy"/>')
        path.write_text(path.read_text().replace('<run>',
            '<referenceableParamGroupList><referenceableParamGroup id="energy">'
            '<cvParam accession="MS:1000045" name="collision energy" value="30" '
            'unitAccession="UO:0000266"/></referenceableParamGroup>'
            '</referenceableParamGroupList><run>'))
        result = inspect_energy(path)
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['settings'][0]['unit'], 'eV')
        self.assertEqual(result['settings'][0]['energies'], [30])

    def test_mzml_export_preserves_normalized_and_stepped_settings(self):
        import numpy as np
        script = Path(__file__).resolve().parents[1] / '.agents/skills/msms-preprocess/scripts/run.py'
        spec = importlib.util.spec_from_file_location('stepped_preprocess', script)
        preprocess = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preprocess)
        raw = self.mzml('<userParam name="stepped normalized collision energy" value="20,40,60"/>')
        spectrum = {'id': 'controllerType=0 scan=1', 'ms level': 2,
                    'precursorList': {'precursor': [{'selectedIonList': {'selectedIon': [{'selected ion m/z': 250}]}}]},
                    'm/z array': np.array([50., 60.]), 'intensity array': np.array([10., 3.])}
        package = types.ModuleType('pyteomics')
        package.mzml = types.ModuleType('pyteomics.mzml')
        reader = MagicMock()
        reader.__enter__.return_value = [spectrum]
        package.mzml.MzML = Mock(return_value=reader)
        with patch.dict('sys.modules', {'pyteomics': package, 'pyteomics.mzml': package.mzml}), \
                contextlib.redirect_stderr(io.StringIO()):
            preprocess.convert_to_ms(raw, self.root / 'export')
        result = inspect_ms(self.root / 'export/mz250.ms', 'NCE')
        self.assertEqual(result['header_units'], ['NCE'])
        self.assertEqual(result['stepped_collision']['values'], [20, 40, 60])


class NativeSteppedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from ms_pred import common
        except ImportError:
            raise unittest.SkipTest('native ms-pred environment required for numerical merge validation')
        cls.common = common

    def prediction(self):
        import numpy as np
        return self.common.CompositeMassSpec([
            self.common.MassSpec(collision_energy=ce, root_canonical_smiles='C',
                                 masses=np.array([50., 60.]), intens=np.array(intens, dtype=float))
            for ce, intens in [(10, [10, 1]), (20, [0, 1]), (30, [0, 1])]])

    def test_native_merge_is_sum_then_normalize_and_score_once(self):
        import numpy as np
        info = stepped_components([20, 40, 60], 250, 'NCE', self.common.nce_to_ev)
        pred = self.prediction()
        merged = worker._scoring_prediction(pred, {'stepped': info})
        np.testing.assert_allclose(merged['stepped'].intens, [1, .3])
        np.testing.assert_allclose(pred['10'].intens, [10, 1])
        exp = {'stepped': self.common.MassSpec(10, masses=np.array([50., 60.]), intens=np.array([1., .3]))}
        pairs = worker.align_energies(exp, merged, {'stepped': info}, 'NCE')
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['input_values'], [20, 40, 60])
        self.assertAlmostEqual(worker._score(exp, merged, pairs, 250), 1., places=6)
        with self.assertRaisesRegex(ValueError, 'Missing stepped'):
            worker._scoring_prediction(self.common.CompositeMassSpec([pred['10']]), {'stepped': info})

    def test_both_model_scoring_adapters_use_components_and_one_merged_comparison(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'q.ms'
            path.write_text('>compound x\n>parentmass 250\n>ionization [M+H]+\n'
                            '>stepped_collision_energies 20,40,60\n\n>collision 20 NCE\n50 10\n60 3\n')
            for model in ['iceberg', 'glacier']:
                db = Mock()
                db.get_all_specs.return_value = [(self.prediction(),)]
                with self.subTest(model=model), patch.object(worker, '_run_model', return_value=Path(tmp)) as run, \
                     patch.object(self.common, 'PredSpecDB', return_value=db), \
                     patch.object(worker, '_model_versions', return_value=('test', 'test')), \
                     patch.object(worker, '_structure_image', return_value=None):
                    candidates = worker.simulate(str(path), ['C'], 'CH4', model, None, None, None, tmp, 'NCE')
                self.assertEqual(run.call_args.args[1], [10, 20, 30])
                self.assertFalse(run.call_args.args[2])
                self.assertEqual(len(candidates[0]['energy_alignment']), 1)
                self.assertEqual(candidates[0]['collision_energies'], [20, 40, 60])
                np.testing.assert_allclose([p[1] for p in candidates[0]['predicted_spectra']['stepped']], [1, .3])
                db.close.assert_called_once()

    def test_forward_stepped_prediction_preserves_component_spectra(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Mock()
            db.get_all_specs.return_value = [(self.prediction(),)]
            with patch.object(worker, '_run_model', return_value=Path(tmp)), \
                 patch.object(self.common, 'PredSpecDB', return_value=db), \
                 patch.object(worker, '_model_versions', return_value=('test', 'test')):
                result = worker.predict(['C'], [10, 20, 30], 'eV', '[M+H]+',
                                        'iceberg', None, None, None, stepped=True)[0]
            self.assertEqual(result['acquisition_mode'], 'stepped')
            self.assertEqual(set(result['predicted_spectra']), {'10', '20', '30', 'stepped'})
            self.assertAlmostEqual(result['predicted_spectra']['stepped'][1][1], .3)
            self.assertEqual(result['predicted_spectra']['10'][0][1], 10)
            db.close.assert_called_once()

    def test_forward_nce_stepping_keeps_repeated_rounded_energy_contributions(self):
        import numpy as np
        pred = self.common.CompositeMassSpec([
            self.common.MassSpec(ce, root_canonical_smiles='C', masses=np.array([1., 2.]),
                                 intens=np.array(values, dtype=float))
            for ce, values in [(1, [10, 1]), (2, [0, 1])]])
        db = Mock()
        db.get_all_specs.return_value = [(pred,)]
        with patch.object(worker, '_run_model', return_value=Path('/unused')) as run, \
             patch.object(self.common, 'PredSpecDB', return_value=db), \
             patch.object(worker, '_model_versions', return_value=('test', 'test')):
            result = worker.predict(['C'], [20, 40, 60], 'NCE', '[M+H]+',
                                    'glacier', None, None, None, stepped=True)[0]
        self.assertTrue(run.call_args.args[2])
        self.assertEqual(result['stepped_collision']['model_energies_ev'], [1, 1, 2])
        self.assertAlmostEqual(result['predicted_spectra']['stepped'][1][1], .15)

    def test_atlas_components_are_merged_before_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            formula = 'C16H34'
            precursor = self.common.formula_mass(formula) + self.common.ion2mass['[M+H]+']
            spectrum = root / 'query.ms'
            spectrum.write_text(f'>compound x\n>parentmass {precursor}\n>ionization [M+H]+\n'
                                '>stepped_collision_energies 10,20,30\n\n>collision 10 eV\n50 10\n60 3\n')
            mgf = root / 'atlas.mgf'
            mgf.write_text(''.join(f'BEGIN IONS\nSMILES=CCCCCCCCCCCCCCCC\nCOLLISION_ENERGY={ce}\n'
                                   f'50 {a}\n60 1\nEND IONS\n' for ce, a in [(10, 10), (20, 0), (30, 0)]))
            with patch.object(worker, '_structure_image', return_value=None):
                result = worker.rank(str(spectrum), str(mgf), formula, 10, 'eV')
            self.assertEqual(result['scored_structures'], 1)
            self.assertEqual(result['missing_energies_ev'], [])
            candidate = result['candidates'][0]
            self.assertEqual(len(candidate['energy_alignment']), 1)
            self.assertAlmostEqual(candidate['entropy_similarity'], 1., places=6)
            self.assertAlmostEqual(candidate['predicted_spectra']['stepped'][1][1], .3)


if __name__ == '__main__':
    unittest.main()
