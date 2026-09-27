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

    def _run_args(self, root, **extra):
        spec = root / 'query.ms'
        spec.write_text('>compound unknown\n>parentmass 17.0386\n>ionization [M+H]+\n\n>collision 20 eV\n15.0 12\n')
        mgf = root / 'library.mgf'
        mgf.write_text('BEGIN IONS\nSMILES=C\nCOLLISION_ENERGY=30\n15.0 12\nEND IONS\n')
        args = dict(input=str(spec), output_dir=str(root / 'output'), formula=None, ms_pred_python='python',
                    atlas_mgf=str(mgf), atlas_url='https://example.test', top_k=10, no_report=True,
                    collision_unit='eV', ms1_ppm=None, ms2_ppm=None, formula_elements='CHNOPS', no_pubchem=False)
        args.update(extra)
        return SimpleNamespace(**args)

    def test_pubchem_formula_fallback_when_msbuddy_finds_none(self):
        from msms_structure_elucidation import pubchem
        found = [{'formula': 'CH4', 'structures': 3, 'ppm': 1.2, 'source': 'PubChem mass match (±10 ppm, CHNOPS)'}]
        ranked = {'library_structures': 1, 'scored_structures': 0, 'candidates': []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (patch.object(cli, 'worker', side_effect=lambda python, action, **kw: [] if action == 'formula' else ranked),
                  patch.object(pubchem, 'formulas_by_mass', return_value=found) as search):
                self.assertEqual(cli.run(self._run_args(root)), 2)
            search.assert_called_once()
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertEqual(data['formula'], 'CH4')
            self.assertTrue(data['formula_source'].startswith('PubChem mass match'))
            self.assertEqual(data['next_step'], 'iceberg-atlas')
            self.assertTrue(any('--proposals atlas' in w for w in data['warnings']))

    def test_mass_tolerance_follows_instrument(self):
        from msms_structure_elucidation.spectrum import mass_tolerance
        self.assertEqual(mass_tolerance('Bruker maXis impact Q-TOF (LCMS)')['ms1_ppm'], 10.0)
        self.assertEqual(mass_tolerance('Orbitrap (LCMS)')[ 'ms2_ppm'], 10.0)
        self.assertEqual(mass_tolerance('Orbitrap (LCMS)')['ms1_ppm'], 5.0)
        self.assertEqual(mass_tolerance(None), {'instrument': 'unknown', 'ms1_ppm': 10.0, 'ms2_ppm': 20.0})
        from msms_structure_elucidation import pubchem
        calls = []
        def fake(python, action, **kw):
            calls.append((action, kw.get('ms1_ppm'), kw.get('ms2_ppm')))
            return []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._run_args(root)
            spec = Path(args.input)
            spec.write_text(spec.read_text().replace('>ionization', '>instrumentation Orbitrap (LCMS)\n>ionization'))
            with (patch.object(cli, 'worker', side_effect=fake),
                  patch.object(pubchem, 'formulas_by_mass', return_value=[]) as search):
                cli.run(args)
            self.assertEqual(calls, [('formula', 5.0, 10.0)])
            self.assertEqual(search.call_args.args[2], 5.0)
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertEqual(data['mass_tolerance'], {'instrument': 'Orbitrap', 'ms1_ppm': 5.0, 'ms2_ppm': 10.0,
                                                      'source': 'instrument default'})
            calls.clear()
            with (patch.object(cli, 'worker', side_effect=fake), patch.object(pubchem, 'formulas_by_mass', return_value=[])):
                cli.run(self._run_args(root, ms1_ppm=3.0))
            self.assertEqual(calls, [('formula', 3.0, 20.0)])

    def test_no_pubchem_formula_asks_for_review_and_frigid(self):
        from msms_structure_elucidation import pubchem
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (patch.object(cli, 'worker', return_value=[]), patch.object(pubchem, 'formulas_by_mass', return_value=[])):
                self.assertEqual(cli.run(self._run_args(root)), 2)
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertIsNone(data['formula'])
            self.assertEqual(data['next_step'], 'review-frigid')
            self.assertTrue(any('FRIGID' in w for w in data['warnings']))

    def test_formula_missing_from_atlas_or_failed_retrieval_sets_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, 'download_mgf', side_effect=ValueError('Atlas returned no MGF')):
                self.assertEqual(cli.run(self._run_args(root, formula='CH4', atlas_mgf=None)), 2)
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertEqual((data['next_step'], data['library_structures']), ('iceberg-pubchem', 0))
            with patch.object(cli, 'worker', side_effect=RuntimeError('worker crashed\nMemoryError')):
                cli.run(self._run_args(root, formula='CH4'))
            data = json.loads((root / 'output/retrieval.json').read_text())
            self.assertEqual(data['next_step'], 'retry-retrieval')
            self.assertIn('MemoryError', data['warnings'][-1])

    def test_review_reuses_atlas_entries_regardless_of_stereochemistry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgf = root / 'atlas.mgf'
            mgf.write_text('BEGIN IONS\nSMILES=CC(N)C(=O)O\nEND IONS\n')
            result = root / 'retrieval.json'
            result.write_text(json.dumps({'input': str(root / 'q.ms'), 'formula': 'C3H7NO2', 'collision_unit': 'eV',
                                          'atlas_mgf': str(mgf), 'candidates': [], 'warnings': []}))
            proposals = root / 'p.json'
            proposals.write_text(json.dumps(['C[C@H](N)C(=O)O', 'NCCC(=O)O']))
            atlas_hit = {'smiles': 'CC(N)C(=O)O', 'inchikey': 'QNAYBMKLOCPYGJ-UHFFFAOYSA-N',
                         'source': 'public ICEBERG 2.1 PubChem atlas', 'entropy_similarity': 0.6, 'explained_intensity': 0.5}
            simulated = []
            def fake(python, action, **kw):
                if action == 'validate':
                    return [{'smiles': 'C[C@H](N)C(=O)O', 'formula': 'C3H7NO2', 'formula_match': True, 'connectivity': 'QNAYBMKLOCPYGJ'},
                            {'smiles': 'NCCC(=O)O', 'formula': 'C3H7NO2', 'formula_match': True, 'connectivity': 'UCMIRNVEIXFBKS'}]
                if action == 'rank':
                    return {'candidates': [atlas_hit]}
                simulated.append(json.loads(Path(kw['smiles_json']).read_text()))
                return []
            args = SimpleNamespace(result=str(result), smiles_json=str(proposals), proposals=None, max_structures=500,
                                   ms_pred_python='python', model='iceberg', checkpoint=None,
                                   gen_checkpoint=None, inten_checkpoint=None)
            with patch.object(cli, 'worker', side_effect=fake), patch.object(cli, 'write_report'):
                cli.review(args)
            data = json.loads(result.read_text())
            self.assertEqual([c['smiles'] for c in data['candidates']], ['CC(N)C(=O)O'])
            self.assertEqual(simulated, [['NCCC(=O)O']])
            self.assertEqual(data['review']['atlas_reused'], 1)

    def test_review_proposals_from_cached_atlas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgf = root / 'atlas.mgf'
            mgf.write_text('BEGIN IONS\nSMILES=CCO\nEND IONS\nBEGIN IONS\nSMILES=CCO\nEND IONS\n'
                           'BEGIN IONS\nSMILES=COC\nEND IONS\nBEGIN IONS\nSMILES=CCC\nEND IONS\n')
            args = SimpleNamespace(smiles_json=None, proposals='atlas', max_structures=2)
            path = cli.proposal_file(args, {'atlas_mgf': str(mgf)}, root / 'retrieval.json', 'C2H6O')
            self.assertEqual(json.loads(path.read_text()), ['CCO', 'COC'])
            with self.assertRaisesRegex(ValueError, 'No cached atlas MGF'):
                cli.proposal_file(args, {}, root / 'retrieval.json', 'C2H6O')

    def test_pubchem_mass_search_keeps_neutral_single_component_chnops(self):
        from msms_structure_elucidation import pubchem
        props = [
            {'MolecularFormula': 'C2H6O', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': 'CCO'},
            {'MolecularFormula': 'C2H6O', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': 'COC'},
            {'MolecularFormula': 'CH2O2', 'MonoisotopicMass': '46.0055', 'Charge': 0, 'SMILES': 'OC=O'},
            {'MolecularFormula': 'C2H6O', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': 'CCO.O'},
            {'MolecularFormula': 'C2H5Si', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': 'C[SiH2]C'},
            {'MolecularFormula': 'C2H7O+', 'MonoisotopicMass': '46.0419', 'Charge': 1, 'SMILES': 'CC[OH2+]'},
            {'MolecularFormula': 'C2H5O', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': 'C[CH2]O'},
            {'MolecularFormula': 'CH4O', 'MonoisotopicMass': '46.0419', 'Charge': 0, 'SMILES': '[2H]C([2H])([2H])O'}]
        def fake(path, data=None):
            return {'IdentifierList': {'CID': [1, 2, 3, 4, 5, 6, 7, 8]}} if 'monoisotopic_mass' in path else {'PropertyTable': {'Properties': props}}
        with patch.object(pubchem, '_get', side_effect=fake):
            found = pubchem.formulas_by_mass(47.0492, '[M+H]+', ppm=10)
        self.assertEqual([(f['formula'], f['structures']) for f in found], [('C2H6O', 2), ('CH2O2', 1)])
        self.assertLess(abs(found[0]['ppm']), 10)

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
