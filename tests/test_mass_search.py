import asyncio
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from msms_structure_elucidation import cli, pubchem
from msms_structure_elucidation import config as config_module
from msms_structure_elucidation.mcp import tasks


def property_row(cid: int, formula: str = 'C2H6O', smiles: str = 'CCO', mass: float = 46.0418648) -> dict:
    return {'CID': cid, 'MolecularFormula': formula, 'SMILES': smiles, 'Charge': 0,
            'MonoisotopicMass': str(mass), 'InChIKey': f'KEY-{cid}'}


class PubChemMassTests(unittest.TestCase):
    def test_all_cid_batches_and_all_formulas_are_retained(self):
        rows = {i: property_row(i, f'C{i}H{2*i+2}', 'C' * i) for i in range(1, 503)}
        batches = []

        def get(path, data=None):
            if 'monoisotopic_mass' in path:
                return {'IdentifierList': {'CID': list(rows)}}
            batch = list(map(int, data['cid'].split(',')))
            batches.append(batch)
            return {'PropertyTable': {'Properties': [rows[cid] for cid in batch]}}

        with patch.object(pubchem, '_get', side_effect=get):
            result = pubchem.mass_candidates(46.0418648 + pubchem.PROTON, ppm=5)
        self.assertEqual(list(map(len, batches)), [500, 2])
        self.assertEqual((len(result['formulas']), result['candidate_structures']), (502, 502))
        self.assertEqual(result['matched_cids'], result['eligible_cids'])
        self.assertTrue(result['complete'])

    def test_filters_exact_mass_and_deduplicates_without_losing_cids(self):
        rows = [property_row(1), property_row(2), property_row(3, smiles='COC'),
                property_row(4, mass=46.1), property_row(5, smiles='CCO.O'),
                {**property_row(6), 'Charge': 1}, property_row(7, smiles='[2H]CO'),
                property_row(8, formula='C2H5Si', smiles='C[SiH2]C'),
                property_row(9, formula='C2H5O', smiles='C[CH2]O')]

        def get(path, data=None):
            return ({'IdentifierList': {'CID': list(range(1, 10))}} if data is None else
                    {'PropertyTable': {'Properties': rows}})

        with patch.object(pubchem, '_get', side_effect=get):
            result = pubchem.mass_candidates(46.0418648 + pubchem.PROTON, ppm=5)
        candidates = result['formulas'][0]['candidates']
        self.assertEqual((result['eligible_cids'], result['excluded_cids']), (3, 6))
        self.assertEqual([row['pubchem_cids'] for row in candidates], [[1, 2], [3]])
        self.assertTrue(all(abs(row['mass_error_ppm']) < 5 for row in candidates))

    def test_adducts_and_mass_window(self):
        for adduct, shift in pubchem.ADDUCT_SHIFT.items():
            with self.subTest(adduct=adduct), patch.object(pubchem, '_get', return_value={}) as get:
                result = pubchem.mass_candidates(100.0 + shift, adduct, 5)
                self.assertEqual(result['neutral_mass'], 100)
                self.assertEqual(result['mass_range'], [99.9995, 100.0005])
                self.assertIn('monoisotopic_mass/range/', get.call_args.args[0])

    def test_incomplete_and_failed_lookups_are_not_empty_successes(self):
        responses = [{'IdentifierList': {'CID': [1, 2]}}, {'PropertyTable': {'Properties': [property_row(1)]}}]
        with patch.object(pubchem, '_get', side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete PubChem property'):
                pubchem.mass_candidates(46.0418648 + pubchem.PROTON)
        with patch.object(pubchem, '_get', return_value={'Fault': {'Message': 'timeout'}}):
            with self.assertRaisesRegex(RuntimeError, 'complete CID list'):
                pubchem.mass_candidates(100)
        with patch.object(pubchem, '_get', side_effect=OSError('offline')):
            with self.assertRaisesRegex(OSError, 'offline'):
                pubchem.mass_candidates(100)

    def test_waiting_search_is_polled_and_no_hits_remain_empty(self):
        responses = [{'Waiting': {'ListKey': 'abc'}}, {'IdentifierList': {'CID': [1]}},
                     {'PropertyTable': {'Properties': [property_row(1)]}}]
        with patch.object(pubchem, '_get', side_effect=responses) as get, patch.object(pubchem.time, 'sleep'):
            result = pubchem.mass_candidates(46.0418648 + pubchem.PROTON)
        self.assertIn('listkey/abc/cids', get.call_args_list[1].args[0])
        self.assertEqual(result['candidate_structures'], 1)
        with patch.object(pubchem, '_get', return_value={}):
            self.assertEqual(pubchem.mass_candidates(100)['formulas'], [])
        with patch.object(pubchem, '_get', side_effect=[{'Waiting': {'ListKey': 'abc'}}, {}]), \
                patch.object(pubchem.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'expired'):
                pubchem.mass_candidates(100)

    def test_invalid_tolerance_mass_and_adduct_fail_before_network(self):
        with patch.object(pubchem, '_get') as get:
            for ppm in (0, -1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    pubchem.mass_candidates(100, ppm=ppm)
            with self.assertRaisesRegex(ValueError, 'Unsupported adduct'):
                pubchem.mass_candidates(100, '[M+2H]2+')
            with self.assertRaises(ValueError):
                pubchem.mass_candidates(0.5)
            get.assert_not_called()


class MassWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = self.root / 'query.ms'
        self.spec.write_text('>parentmass 100\n>ionization [M+H]+\n>formula STALE\n'
                             '>instrumentation Orbitrap\n>collision 20 eV\n50 10\n>collision 40 eV\n60 5\n')
        self.checkout = self.root / 'ms-pred'
        (self.checkout / 'src/ms_pred').mkdir(parents=True)
        for name in ('gen', 'inten', 'glacier'):
            (self.root / f'{name}.ckpt').write_text(name)
        self.config_patch = patch.object(config_module, 'LOCAL_CONFIG', self.root / 'no-local.yaml')
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def argv(self, model='glacier'):
        return ['run', '--mass-only', '--input', str(self.spec), '--output-dir', str(self.root / 'out'),
                '--collision-unit', 'eV', '--ms1-ppm', '5', '--model', model, '--top-k', '2',
                '--model-shard-size', '1', '--ms-pred-python', sys.executable, '--ms-pred-dir', str(self.checkout),
                '--checkpoint', str(self.root / 'glacier.ckpt'), '--gen-checkpoint', str(self.root / 'gen.ckpt'),
                '--inten-checkpoint', str(self.root / 'inten.ckpt')]

    def matches(self):
        groups = [{'formula': f'C{i}H{2*i+2}', 'candidates': [{'smiles': 'C' * i,
            'pubchem_cids': [i], 'pubchem_inchikeys': [f'KEY-{i}'], 'mass_error_ppm': float(i)/10,
            'monoisotopic_mass': 98.992724}]} for i in range(1, 8)]
        return {'complete': True, 'matched_cids': 7, 'eligible_cids': 7, 'excluded_cids': 0,
                'candidate_structures': 7, 'formulas': groups}

    def fake_worker(self, calls):
        def run(python, action, **kwargs):
            calls.append((action, kwargs))
            listing = json.loads(Path(kwargs['smiles_json']).read_text())
            if action == 'validate':
                return [{'smiles': s, 'input_smiles': s, 'formula_match': True, 'formula': kwargs['formula']}
                        for s in listing]
            self.assertEqual(action, 'simulate', 'Formula prediction and atlas work must never run')
            return [{'smiles': s, 'formula': kwargs['formula'], 'source': kwargs['model'].upper(),
                     'model_name': kwargs['model'].upper(), 'model_checkpoint': kwargs['checkpoint'],
                     'entropy_similarity': len(s)/10, 'explained_intensity': 0.5, 'matched_peaks': [],
                     'energy_alignment': [{'experimental_key': str(ce), 'input_value': ce,
                                           'prediction_key': str(int(ce))} for ce in (20.0, 40.0)],
                     'predicted_spectra': {'20': [[50, 1]], '40': [[60, 1]]}}
                    for s in listing]
        return run

    def result(self):
        return json.loads((self.root / 'out/retrieval.json').read_text())

    def test_cli_both_models_predict_all_formulas_and_rank_globally_with_reports(self):
        for model in ('iceberg', 'glacier'):
            with self.subTest(model=model):
                calls = []
                with patch.object(pubchem, 'mass_candidates', return_value=self.matches()) as search, \
                        patch.object(cli, 'worker', side_effect=self.fake_worker(calls)), \
                        patch.object(cli, 'download_mgf', side_effect=AssertionError('Atlas must not be queried')):
                    self.assertEqual(cli.main(self.argv(model)), 0)
                result = self.result()
                self.assertEqual(search.call_args.args[2], 5)
                self.assertEqual([c['formula'] for c in result['candidates']], ['C7H16', 'C6H14'])
                self.assertEqual(result['candidates'][0]['pubchem_cids'], [7])
                self.assertTrue(result['prediction_complete'])
                self.assertEqual((result['scored_structures'], len(result['formula_results'])), (7, 7))
                self.assertTrue(all(row['status'] == 'ranked' for row in result['formula_results']))
                simulated = [kw for action, kw in calls if action == 'simulate']
                self.assertEqual(len(simulated), 7)
                self.assertTrue(all(kw['model'] == model and kw['experimental_unit'] == 'eV' for kw in simulated))
                self.assertTrue(all(kw['force_recompute'] for kw in simulated))
                self.assertTrue((self.root / 'out/report.html').is_file())
                self.assertTrue((self.root / 'out/pubchem_candidates.json').is_file())
                self.assertTrue(any('Ignoring' in warning for warning in result['warnings']))

    def test_resume_reuses_predictions_but_checkpoint_change_invalidates_them(self):
        calls = []
        with patch.object(pubchem, 'mass_candidates', return_value=self.matches()), \
                patch.object(cli, 'worker', side_effect=self.fake_worker(calls)):
            cli.main(self.argv())
            calls.clear()
            cli.main(self.argv() + ['--model-batch-size', '4', '--model-cpu-workers', '2'])
            self.assertFalse(any(action == 'simulate' for action, _ in calls))
            (self.root / 'glacier.ckpt').write_text('changed checkpoint')
            calls.clear()
            cli.main(self.argv())
            self.assertEqual(sum(action == 'simulate' for action, _ in calls), 7)

    def test_canonical_and_stereo_aliases_preserve_every_pubchem_identifier(self):
        matches = self.matches()
        group = matches['formulas'][0]
        alias = {**group['candidates'][0], 'smiles': '[CH4]', 'pubchem_cids': [99],
                 'pubchem_inchikeys': ['KEY-99']}
        group['candidates'].append(alias)
        matches['formulas'] = [group]
        calls = []
        fake = self.fake_worker(calls)

        def canonical(python, action, **kwargs):
            if action == 'validate':
                return [{'smiles': 'C', 'input_smiles': row['smiles'], 'formula_match': True,
                         'model_inchikey': 'SAME-MODEL-IDENTITY'} for row in group['candidates']]
            return fake(python, action, **kwargs)

        with patch.object(pubchem, 'mass_candidates', return_value=matches), \
                patch.object(cli, 'worker', side_effect=canonical):
            cli.main(self.argv())
        result = self.result()
        self.assertEqual(result['scored_structures'], 1)
        self.assertEqual(result['candidates'][0]['pubchem_cids'], [1, 99])
        self.assertEqual(result['candidates'][0]['pubchem_smiles'], ['C', '[CH4]'])

    def test_missing_weights_and_wrong_prediction_identities_fail_explicitly(self):
        calls = []
        fake = self.fake_worker(calls)
        with patch.object(pubchem, 'mass_candidates', return_value=self.matches()), \
                patch.object(cli, 'worker', side_effect=fake), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(self.argv() + ['--checkpoint', str(self.root / 'missing.ckpt')])
        self.assertEqual(self.result()['status'], 'blocked_model_assets')
        self.assertFalse(self.result()['prediction_complete'])

        def wrong(python, action, **kwargs):
            rows = fake(python, action, **kwargs)
            return [{**row, 'smiles': 'WRONG'} for row in rows] if action == 'simulate' else rows

        with patch.object(pubchem, 'mass_candidates', return_value=self.matches()), \
                patch.object(cli, 'worker', side_effect=wrong), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(self.argv())
        self.assertIn('identities differ', self.result()['formula_results'][0]['error'])
        self.assertFalse(any(not path.name.endswith('.smiles.json')
                             for path in (self.root / 'out/model_shards/CH4').glob('*.json')))
        with patch.object(pubchem, 'mass_candidates', return_value=self.matches()), \
                patch.object(cli, 'worker', side_effect=fake):
            self.assertEqual(cli.main(self.argv()), 0)

    def test_partial_failure_has_nonzero_exit_receipt_and_unprocessed_formulas(self):
        calls = []
        fake = self.fake_worker(calls)

        def fail(python, action, **kwargs):
            if action == 'simulate' and kwargs['formula'] == 'C2H6':
                raise RuntimeError('GPU prediction failed')
            return fake(python, action, **kwargs)

        with patch.object(pubchem, 'mass_candidates', return_value=self.matches()), \
                patch.object(cli, 'worker', side_effect=fail), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(self.argv())
        self.assertEqual(error.exception.code, 1)
        result = self.result()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['prediction_complete'])
        self.assertEqual(result['scored_structures'], 1)
        self.assertEqual([row['status'] for row in result['formula_results']],
                         ['ranked', 'error'] + ['not_processed'] * 5)

    def test_search_failure_is_not_reported_as_no_matches(self):
        with patch.object(pubchem, 'mass_candidates', side_effect=RuntimeError('PubChem timed out')), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(self.argv())
        self.assertEqual(self.result()['status'], 'error')
        self.assertFalse(self.result()['pubchem_mass_search']['complete'])

    def test_no_matches_needs_no_model_interpreter_and_returns_no_candidates(self):
        matches = {**self.matches(), 'formulas': [], 'candidate_structures': 0}
        with patch.object(pubchem, 'mass_candidates', return_value=matches), \
                patch.object(cli, 'model_python', side_effect=AssertionError('No prediction needed')):
            self.assertEqual(cli.main(self.argv()), 0)
        self.assertEqual(self.result()['status'], 'no_candidates')
        self.assertNotIn('next_step', self.result())

    def test_conflicting_inputs_fail_before_network_or_models(self):
        for extra in (['--formula', 'C2H6O'], ['--formulas-file', 'unused'], ['--atlas-mgf', 'unused'],
                      ['--no-pubchem'], ['--ms1-ppm', 'nan']):
            with self.subTest(extra=extra), patch.object(pubchem, 'mass_candidates') as search, \
                    patch.object(cli, 'worker') as worker, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main(self.argv() + extra)
                search.assert_not_called()
                worker.assert_not_called()

    def test_batch_propagates_mass_options_and_changes_resume_signature(self):
        captured = []

        def batch(args):
            context = {'args': args, 'out': self.root / 'batch', 'cache': self.root / 'cache',
                       'feature_counts': {'f': 1}, 'formula_list_hash': None, 'config_hash': 'config',
                       'asset_versions': {}}

            def run(options):
                captured.append(options)
                out = Path(options.output_dir)
                out.mkdir(parents=True, exist_ok=True)
                (out / 'retrieval.json').write_text(json.dumps({'status': 'no_candidates'}))

            with patch.object(cli, 'run', side_effect=run):
                first = cli._batch_feature(('f', str(self.spec)), {'formula': 'STALE'}, context)
                second = cli._batch_feature(('f', str(self.spec)), {'formula': 'STALE'}, context)
                self.assertTrue(second['resumed'])
                args.ms1_ppm = 10
                third = cli._batch_feature(('f', str(self.spec)), {'formula': 'STALE'}, context)
            self.assertFalse(first.get('resumed', False))
            self.assertFalse(third.get('resumed', False))
            return 0

        with patch.object(cli, 'batch', side_effect=batch):
            cli.main(['batch', '--manifest', 'unused', '--output-dir', str(self.root / 'batch'), '--collision-unit', 'eV',
                      '--mass-only', '--model', 'glacier', '--ms1-ppm', '5', '--formula-elements', 'CHNOPSCl'])
        self.assertEqual(len(captured), 2)
        self.assertTrue(captured[0].mass_only)
        self.assertIsNone(captured[0].formula)
        self.assertEqual(captured[0].formula_elements, 'CHNOPSCl')

    def test_mcp_task_passes_model_paths_devices_and_mass_mode(self):
        config = {'_config_path': str(self.root / 'config.yaml'),
                  'models': {'simulator': {'python': sys.executable, 'ms_pred_src': str(self.checkout),
                    'glacier_ckpt': str(self.root / 'glacier.ckpt'), 'instrument': 'Orbitrap'}}}
        job = self.root / 'job'
        job.mkdir()
        params = {'spectrum_path': str(self.spec), 'collision_unit': 'eV', 'model': 'glacier',
                  'ppm': 5, 'top_k': 10, 'elements': 'CHNOPS', 'shard_size': 3}

        def run(command, **kwargs):
            self.assertIn('--mass-only', command)
            for flag, value in (('--model', 'glacier'), ('--ms1-ppm', '5'), ('--cuda-devices', '2'),
                                ('--checkpoint', str(self.root / 'glacier.ckpt'))):
                self.assertEqual(command[command.index(flag) + 1], value)
            out = job / 'retrieval'
            out.mkdir(exist_ok=True)
            (out / 'retrieval.json').write_text(json.dumps({'status': 'ranked', 'formula': 'C2H6O'}))
            return SimpleNamespace(returncode=0)

        with patch.object(tasks.subprocess, 'run', side_effect=run):
            result = tasks.search_pubchem_mass(params, job, config, '2', io.StringIO())
        self.assertEqual(result['status'], 'ranked')
        self.assertTrue(Path(result['retrieval_path']).is_file())
        with patch.object(tasks.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'exited with 1'):
                tasks.search_pubchem_mass(params, job, config, '2', io.StringIO())


class WrapperTests(unittest.TestCase):
    def test_retrieval_script_forwards_the_mass_mode_and_weights(self):
        path = Path(__file__).resolve().parents[1] / '.agents/skills/msms-atlas-retrieval/scripts/run.py'
        spec = importlib.util.spec_from_file_location('retrieval_script', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'custom.json'

            def run(command, **kwargs):
                self.assertIn('--mass-only', command)
                for flag, value in (('--ms1-ppm', '5.0'), ('--model', 'glacier'), ('--checkpoint', 'weights.ckpt')):
                    self.assertEqual(command[command.index(flag) + 1], value)
                (Path(tmp) / 'retrieval.json').write_text('{"status":"no_candidates"}')
                return SimpleNamespace(returncode=0)

            with patch.object(module.subprocess, 'run', side_effect=run):
                code = module.main(['--spectrum', 'sample.ms', '--collision-unit', 'eV', '--mass-only',
                                    '--ms1-ppm', '5', '--model', 'glacier', '--checkpoint', 'weights.ckpt',
                                    '--output', str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text())['status'], 'no_candidates')


class ModelAdapterTests(unittest.TestCase):
    def test_new_shards_bypass_native_cache_for_both_models(self):
        from msms_structure_elucidation import worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / 'weights.ckpt'
            checkpoint.touch()
            (root / 'preds.hdf5').touch()
            for model in ('glacier', 'iceberg'):
                with self.subTest(model=model):
                    (root / f'{model}_run_successful').touch()
                    module_name = f'ms_pred.{model}.{model}_elucidation'
                    module = types.ModuleType(module_name)
                    predict = Mock(return_value=(str(root), None))
                    setattr(module, f'{model}_prediction', predict)
                    with patch.dict(sys.modules, {module_name: module}), \
                            patch.object(worker, '_check_glacier_features'):
                        result = worker._run_model(['C'], [20], False, '[M+H]+', model,
                            str(checkpoint), str(checkpoint), str(checkpoint), 'Orbitrap', None,
                            1, 1, 4, force_recompute=True)
                    self.assertEqual(result, root)
                    self.assertTrue(predict.call_args.kwargs['force_recompute'])
                    self.assertEqual(predict.call_args.kwargs['num_gpu_workers'], 1 if model == 'glacier' else 4)


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'RDKit is not installed in this interpreter')
class ModelIdentityTests(unittest.TestCase):
    def test_stereo_free_validation_matches_model_identity_and_preserves_inputs(self):
        from msms_structure_elucidation.worker import validate_smiles
        inputs = ['C[C@H](O)C(=O)O', 'C[C@@H](O)C(=O)O']
        rows = validate_smiles(inputs, 'C3H6O3', keep_duplicates=True, strip_stereo=True)
        self.assertEqual([row['input_smiles'] for row in rows], inputs)
        self.assertEqual(len({row['model_inchikey'] for row in rows}), 1)
        self.assertEqual({row['smiles'] for row in rows}, {'CC(O)C(=O)O'})
        self.assertTrue(all(row['formula_match'] for row in rows))


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'MCP extra is not installed')
class MassToolSchemaTests(unittest.TestCase):
    def test_tool_requires_model_tolerance_and_collision_unit(self):
        from msms_structure_elucidation.mcp import server
        tools = {tool.name: tool for tool in asyncio.run(server.server.list_tools())}
        schema = tools['search_pubchem_mass'].input_schema
        self.assertTrue({'model', 'ppm', 'collision_unit'} <= set(schema['required']))
        self.assertNotIn('formula', schema['properties'])


if __name__ == '__main__':
    unittest.main()
