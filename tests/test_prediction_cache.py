import json
import os
import importlib.util
import shutil
import tempfile
import threading
import time
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msms_structure_elucidation import cli, glacier, prediction_cache as cache, pubchem


def produce(molecules, energies):
    return [{'smiles': smi, 'predicted_spectra': {str(e): [[float(e), .8]] for e in energies},
             'predicted_fragment_ids': {str(e): ['1'] for e in energies}} for smi in molecules]


class InterruptedCacheTests(unittest.TestCase):
    def hot_cache(self, root):
        identity={'test':'interrupted-writer'}
        expected,_=cache.predictions(root,identity,['CCO'],[20],produce)
        database=root/cache.digest(identity)/'spectra.sqlite3'
        subprocess.run([sys.executable,'-c', '''import sqlite3,os,sys
db=sqlite3.connect(sys.argv[1]);db.execute('PRAGMA cache_size=1');db.execute('BEGIN IMMEDIATE')
db.execute('CREATE TABLE uncommitted (payload BLOB)')
db.executemany('INSERT INTO uncommitted VALUES (?)',[(b'x'*4096,)]*128)
os._exit(0)
''',str(database)],check=True)
        self.assertTrue(database.with_name(database.name+'-journal').is_file())
        reader=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as error:
                reader.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            self.assertEqual(getattr(error.exception,'sqlite_errorcode',8) & 255,8)
        finally:
            reader.close()
        return identity,expected

    def test_prediction_reader_recovers_hot_journal_without_repredicting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identity,expected=self.hot_cache(root)
            actual,receipt=cache.predictions(root,identity,['CCO'],[20],
                lambda *args:self.fail('A recovered cache must not repeat inference'))
            self.assertEqual(actual,expected)
            self.assertEqual(receipt['reused_spectra'],1)

    def test_dispatch_planner_recovers_hot_journal_without_losing_cached_rows(self):
        from msms_structure_elucidation.mcp.raw_outputs import missing_groups
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identity,_=self.hot_cache(root)
            self.assertEqual(missing_groups(root,identity,['CCO'],[20,40]),{(40,):['CCO']})


class AdapterIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('worker.py', 'glacier.py', 'iceberg.py'):
            shutil.copyfile(Path(cache.__file__).with_name(name), self.root / name)
        self.worker = self.root / 'worker.py'

    def test_scoring_only_change_reuses_existing_raw_predictions(self):
        original = cache.adapter_fingerprint(self.worker)
        first, _ = cache.predictions(self.root, {'adapter_sha256': original}, ['CCO'], [20, 40], produce)
        source = self.worker.read_text()
        modified = source.replace('return super().int_frags', 'return None')
        self.assertNotEqual(source, modified)
        self.worker.write_text(modified)
        identity = {'adapter_sha256': cache.adapter_fingerprint(self.worker)}
        self.assertEqual(identity['adapter_sha256'], original)
        second, receipt = cache.predictions(self.root, identity, ['CCO'], [20, 40],
            lambda *args: self.fail('scoring-only change invalidated raw predictions'))
        self.assertEqual(first, second)
        self.assertEqual(receipt['reused_spectra'], 2)
        self.assertEqual(receipt['predicted_spectra'], 0)

    def test_raw_generation_or_model_adapter_changes_invalidate(self):
        before = cache.adapter_fingerprint(self.worker)
        raw = self.worker.read_text()
        self.worker.write_text(raw.replace("runtime.get('sparse_k', 100)", "runtime.get('sparse_k', 200)"))
        self.assertNotEqual(cache.adapter_fingerprint(self.worker), before)
        self.worker.write_text(raw)
        for name in ('glacier.py', 'iceberg.py'):
            path = self.root / name
            original = path.read_bytes()
            path.write_bytes(original + b'\nRAW_GENERATION_VERSION = 2\n')
            self.assertNotEqual(cache.adapter_fingerprint(self.worker), before)
            path.write_bytes(original)

    def test_verified_legacy_namespace_can_be_retained_during_a_rollout(self):
        legacy = 'e06de0363d3b474f1683b692d8faafe435244861a76ebf2e38aa4a9af491a42f'
        verified = '4116460df09642d1ca64381b287450ad5ea3e370e6b9e1bf10190749a720b87d'
        with patch.dict(os.environ, {'MSMS_PREDICTION_CACHE_ADAPTER': legacy}), \
                patch.object(cache, '_raw_adapter_source', side_effect=[verified, 'unverified-source']):
            self.assertEqual(cache.adapter_fingerprint(self.worker), legacy)
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                cache.adapter_fingerprint(self.worker)


class SharedPredictionTests(unittest.TestCase):
    def test_missing_cache_recheck_releases_reader_before_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);identity={'weights':'same'};molecules=[f'candidate_{i}' for i in range(300)]
            database=root/cache.digest(identity)/'spectra.sqlite3'
            def generate(smiles,energies):
                # Another producer must be able to publish while inference runs.
                with sqlite3.connect(database,timeout=.1) as writer:
                    writer.execute('CREATE TABLE independent_writer (value INTEGER)')
                    writer.execute('INSERT INTO independent_writer VALUES (1)')
                return produce(smiles,energies)
            actual,receipt=cache.predictions(root,identity,molecules,[20,40],generate)
            self.assertEqual(actual,produce(molecules,[20,40]))
            self.assertEqual(receipt['predicted_spectra'],600)
            reused,receipt=cache.predictions(root,identity,molecules,[20,40],
                lambda *args:self.fail('Completed predictions must be reused'))
            self.assertEqual(actual,reused);self.assertEqual(receipt['reused_spectra'],600)

    def test_a_large_warm_reader_releases_its_snapshot_for_another_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {'weights': 'same'}
            molecules = [f'candidate_{i}' for i in range(300)]
            cache.predictions(root, identity, molecules, [20], produce)
            database = root / cache.digest(identity) / 'spectra.sqlite3'
            original = cache.sqlite3.connect
            errors = []
            writes = []
            class Reader:
                def __init__(self, connection):
                    self.connection, self.reads = connection, 0
                def execute(self, sql, parameters=()):
                    if sql.startswith('SELECT payload'):
                        self.reads += 1
                        if self.reads == 257:
                            def write():
                                try:
                                    with original(database, timeout=.5) as db:
                                        db.execute('INSERT INTO spectra VALUES (?, ?, ?)', ('other', '20', '{}'))
                                    writes.append(True)
                                except Exception as error:
                                    errors.append(str(error))
                            thread = threading.Thread(target=write)
                            thread.start()
                            thread.join(2)
                            if thread.is_alive():
                                errors.append('writer did not finish between read windows')
                    return self.connection.execute(sql, parameters)
                def commit(self):
                    return self.connection.commit()
                def close(self):
                    return self.connection.close()
            def connect(*args, **kwargs):
                connection = original(*args, **kwargs)
                return Reader(connection) if kwargs.get('uri') else connection
            with patch.object(cache.sqlite3, 'connect', side_effect=connect):
                records, receipt = cache.predictions(root, identity, molecules, [20], produce)
            self.assertEqual(errors, [])
            self.assertEqual(writes, [True])
            self.assertEqual(len(records), 300)
            self.assertEqual(receipt['predicted_spectra'], 0)

    def test_complete_cache_hit_needs_no_writer_lease_or_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, _ = cache.predictions(root, {'weights': 'same'}, ['CCO', 'COC'], [20, 40], produce)
            with patch.object(cache, 'candidate_guard', side_effect=AssertionError('warm hit took writer lease')):
                second, receipt = cache.predictions(root, {'weights': 'same'}, ['CCO', 'COC'], [20, 40],
                    lambda *a: self.fail('warm hit predicted again'))
            self.assertEqual(first, second)
            self.assertEqual(receipt['reused_spectra'], 4)
            self.assertEqual(receipt['predicted_spectra'], 0)
    def test_large_candidate_batch_uses_bounded_file_descriptors(self):
        handles = Path('/proc/self/fd')
        if not handles.is_dir():
            self.skipTest('open-descriptor accounting requires procfs')
        before = len(list(handles.iterdir()))
        def predict(molecules, energies):
            self.assertLess(len(list(handles.iterdir())), before + 10)
            return produce(molecules, energies)
        with tempfile.TemporaryDirectory() as tmp:
            records, receipt = cache.predictions(Path(tmp), {'model': 'large'},
                [f'candidate_{index}' for index in range(5000)], [20], predict)
        self.assertEqual(len(records), 5000)
        self.assertEqual(receipt['predicted_spectra'], 5000)

    def test_disjoint_candidate_sets_can_predict_concurrently(self):
        barrier = threading.Barrier(2)
        def predict(molecules, energies):
            barrier.wait(timeout=3)
            return produce(molecules, energies)
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.predictions, Path(tmp), {'model': 'same'}, ['CCO'], [20], predict)
            second = pool.submit(cache.predictions, Path(tmp), {'model': 'same'}, ['CCC'], [20], predict)
            self.assertEqual(len(first.result()[0]), 1)
            self.assertEqual(len(second.result()[0]), 1)

    def test_abandoned_partial_lease_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            leases = Path(tmp) / cache.digest({'model': 'same'}) / 'leases'
            leases.mkdir(parents=True)
            (leases / 'dead.json').write_text('{partial')
            (leases / 'dead.lock').touch()
            result, _ = cache.predictions(Path(tmp), {'model': 'same'}, ['CCO'], [20], produce)
            self.assertEqual(len(result), 1)
            self.assertFalse((leases / 'dead.json').exists())
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_overlapping_candidates_and_energies_predict_only_missing_pairs(self):
        calls = []
        def predict(molecules, energies):
            calls.append((molecules, energies))
            return produce(molecules, energies)
        first, _ = cache.predictions(self.root, {'weights': 'one'}, ['CCO', 'COC'], [20, 40], predict)
        second, receipt = cache.predictions(self.root, {'weights': 'one'}, ['COC', 'CCC'], [20, 40, 60], predict)
        self.assertEqual(calls, [(['CCO', 'COC'], [20, 40]), (['COC'], [60]), (['CCC'], [20, 40, 60])])
        self.assertEqual(receipt['reused_spectra'], 2)
        self.assertEqual(receipt['predicted_spectra'], 4)
        self.assertEqual(first[1]['predicted_spectra']['40'], second[0]['predicted_spectra']['40'])
        self.assertNotIn('entropy_similarity', second[0])

    def test_concurrent_features_share_one_prediction(self):
        calls = []
        entered = threading.Event()
        def predict(molecules, energies):
            calls.append((molecules, energies))
            entered.set()
            time.sleep(.08)
            return produce(molecules, energies)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.predictions, self.root, {'model': 'same'}, ['CCO'], [20], predict)
            self.assertTrue(entered.wait(2))
            second = pool.submit(cache.predictions, self.root, {'model': 'same'}, ['CCO'], [20], predict)
            a, b = first.result(), second.result()
        self.assertEqual(len(calls), 1)
        self.assertEqual(a[0], b[0])
        self.assertEqual(b[1]['reused_spectra'], 1)

    def test_scientific_context_changes_cannot_reuse_predictions(self):
        baseline = {'model': 'glacier', 'weights': 'a', 'adduct': '[M+H]+', 'instrument': 'Orbitrap',
                    'source': 'one', 'sparse_k': 100}
        with patch(__name__ + '.produce', wraps=produce) as predict:
            cache.predictions(self.root, baseline, ['CCO'], [20], predict)
            for key, value in [('model', 'iceberg'), ('weights', 'b'), ('adduct', '[M-H]-'),
                               ('instrument', 'QTOF'), ('source', 'two'), ('sparse_k', 50)]:
                _, receipt = cache.predictions(self.root, {**baseline, key: value}, ['CCO'], [20], predict)
                self.assertEqual(receipt['reused_spectra'], 0)
            self.assertEqual(predict.call_count, 7)

    def test_invalid_predictions_are_not_published(self):
        def wrong(molecules, energies):
            return produce(['WRONG'], energies)
        with self.assertRaisesRegex(RuntimeError, 'identities'):
            cache.predictions(self.root, {}, ['CCO'], [20], wrong)
        _, receipt = cache.predictions(self.root, {}, ['CCO'], [20], produce)
        self.assertEqual(receipt['reused_spectra'], 0)

    def test_changed_weights_are_rehashed_even_at_same_path_and_size(self):
        weights = self.root / 'model.ckpt'
        weights.write_bytes(b'old weights')
        first = cache.file_fingerprint(weights, self.root / 'cache')
        weights.write_bytes(b'new weights')
        self.assertNotEqual(first, cache.file_fingerprint(weights, self.root / 'cache'))


class InferenceDefaultsTests(unittest.TestCase):
    def test_mcp_formula_inference_returns_top_three_without_candidate_search(self):
        import sys
        from msms_structure_elucidation.mcp import tasks
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            spectrum=root/'query.ms'
            spectrum.write_text('>parentmass 47.0491\n>ionization [M+H]+\n>instrumentation Orbitrap\n\n>collision 20 eV\n31 1\n')
            hypotheses=[{'formula':f'C{i}H{2*i+2}','source':'MSBuddy inferred'} for i in range(1,6)]
            settings={'_config_path':str(root/'config.yaml'),'models':{'simulator':{'python':sys.executable}}}
            with patch.object(cli,'worker',return_value=hypotheses) as worker:
                result=tasks.infer_formulas({'spectrum_path':str(spectrum),'collision_unit':'eV'},root,settings,None,None)
            self.assertEqual(result['formula_hypotheses'],hypotheses[:3])
            self.assertEqual(worker.call_args.args[1],'formula')
            self.assertEqual(worker.call_args.kwargs['ms1_ppm'],5)

    def test_glacier_does_not_inherit_iceberg_tuning(self):
        settings = {'models': {'simulator': {'batch_size': 1, 'num_cpu_workers': 32, 'num_gpu_workers': 4}}}
        options = cli._model_options(SimpleNamespace(model='glacier'), settings)
        self.assertEqual([options[k] for k in ('batch_size', 'num_cpu_workers', 'num_gpu_workers',
            'num_writers', 'num_h5_chunks', 'feat_cache_size', 'sparse_k')], [128, 4, 1, 2, 4, 256, 100])
        options = cli._model_options(SimpleNamespace(model='glacier', model_cpu_workers=0), settings)
        self.assertEqual(options['num_cpu_workers'], 0)

    def test_model_worker_defaults_and_saved_tuning(self):
        self.assertEqual(cli._model_options(SimpleNamespace(model='iceberg'), {})['num_gpu_workers'], 2)
        self.assertEqual(cli._model_options(SimpleNamespace(model='glacier'), {})['num_gpu_workers'], 1)
        settings = {'models': {'simulator': {'num_gpu_workers': 4,
            'glacier': {'batch_size': 512, 'num_gpu_workers': 1}}}}
        self.assertEqual(cli._model_options(SimpleNamespace(model='iceberg'), settings)['num_gpu_workers'], 4)
        options = cli._model_options(SimpleNamespace(model='glacier'), settings)
        self.assertEqual((options['batch_size'], options['num_gpu_workers']), (512, 1))

    def test_native_command_receives_io_settings_and_only_one_gpu(self):
        import sys
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'predict_smis_joint.py'
            script.write_text("parser.add_argument('--num-writers')\nparser.add_argument('--num-h5-chunks')\nparser.add_argument('--feat-cache-size')\n")
            common = types.ModuleType('ms_pred.common')
            common.normalize_instrument = lambda x: x
            common.mass_from_smi = lambda _: 46.
            common.ion2mass = {'[M+H]+': 1.}
            common.rm_stereo = lambda x: x
            common.inchikey_from_smiles = lambda _: 'KEY'
            model = types.ModuleType('ms_pred.glacier.glacier_elucidation')
            model.__file__ = str(root / 'glacier_elucidation.py')
            package = types.ModuleType('ms_pred')
            package.common = common
            glacier_package = types.ModuleType('ms_pred.glacier')
            glacier_package.glacier_elucidation = model
            def launch(command, env):
                for key, value in [('batch-size',128), ('num-cpu-workers',4),
                    ('num-gpu-workers',len(env['CUDA_VISIBLE_DEVICES'].split(','))),
                    ('num-writers',2), ('num-h5-chunks',4), ('feat-cache-size',256), ('sparse-k',100)]:
                    self.assertEqual(command[command.index('--'+key)+1], str(value))
                self.assertIn(env['CUDA_VISIBLE_DEVICES'], ['3','3,4'])
                (root / 'preds_chunk_0.hdf5').touch()
                return SimpleNamespace(returncode=0)
            with patch.dict(sys.modules, {'ms_pred':package,'ms_pred.common':common,
                'ms_pred.glacier':glacier_package,'ms_pred.glacier.glacier_elucidation':model}), \
                patch.object(glacier.tempfile,'mkdtemp',return_value=str(root)), \
                patch.object(glacier.subprocess,'run',side_effect=launch) as process:
                self.assertEqual(glacier.run(['CCO'],[20],False,'[M+H]+','Orbitrap','weights','3',
                                             128,4,2,4,256,100),root)
                self.assertEqual(glacier.run(['CCO'],[20],False,'[M+H]+','Orbitrap','weights','3,4',
                                             128,4,2,4,256,100),root)
                script.write_text('')
                with self.assertRaisesRegex(RuntimeError,'runtime lacks'):
                    glacier.run(['CCO'],[20],False,'[M+H]+','Orbitrap','weights','3',128,4,2,4,256,100)
                self.assertEqual(process.call_count,2)


class ExpandedElementsTests(unittest.TestCase):
    def test_formula_candidates_retain_all_cids_and_detect_incomplete_responses(self):
        def get(path,data=None):
            if data is None:
                return {'IdentifierList':{'CID':list(range(1,503))}}
            return {'PropertyTable':{'Properties':[
                {'CID':int(cid),'MolecularFormula':'C2H6O','SMILES':'CCO','Charge':0,
                 'MonoisotopicMass':'46.0418648','InChIKey':f'KEY{cid}'} for cid in data['cid'].split(',')]}}
        with patch.object(pubchem,'_get',side_effect=get) as lookup:
            group=pubchem.candidates_for_formula('C2H6O',46.0418648+pubchem.PROTON,'[M+H]+')
        self.assertEqual(len(group['candidates'][0]['pubchem_cids']),502)
        self.assertEqual(lookup.call_count,3)
        self.assertTrue(group['complete'])
        with patch.object(pubchem,'_get',side_effect=[{'IdentifierList':{'CID':[1,2]}},{}]):
            with self.assertRaisesRegex(RuntimeError,'Incomplete'):
                pubchem.candidates_for_formula('C2H6O',47.,'[M+H]+')

    def test_broader_elements_are_selected_in_one_mass_query(self):
        formulas = ['CH3F','CH3Cl','CH3Br','CH3I','CH4Si','BH3','CH4Se']
        rows = [{'CID':i+1,'MolecularFormula':formula,'SMILES':'C'+formula,'Charge':0,
                 'MonoisotopicMass':'100','InChIKey':f'KEY{i}'} for i,formula in enumerate(formulas)]
        with patch.object(pubchem,'_get',side_effect=[{'IdentifierList':{'CID':list(range(1,8))}},
              {'PropertyTable':{'Properties':rows}}]) as get:
            found=pubchem.mass_candidates(100+pubchem.PROTON,ppm=5)
        self.assertEqual({g['formula'] for g in found['formulas']},set(formulas))
        self.assertEqual(get.call_count,2)


@unittest.skipUnless(importlib.util.find_spec('ms_pred'), 'ms-pred is not installed in this interpreter')
class CachedScoringTests(unittest.TestCase):
    def test_unsupported_elements_are_reported_before_prediction(self):
        from msms_structure_elucidation import worker
        rejected=worker.validate_smiles(['[U]'],'U')[0]
        self.assertFalse(rejected['formula_match'])
        self.assertEqual(rejected['unsupported_elements'],['U'])

    def test_native_reader_opens_all_four_hdf5_chunks_without_a_base_file(self):
        import numpy as np
        from ms_pred import common
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'preds.hdf5'
            database=common.PredSpecDB(base,mode='w',num_h5s=4,has_probs=False,has_brokens=False,
                has_masses=True,has_masses_no_adduct=False,has_frag_form_vecs=False,has_frags=False,has_intens=True)
            try:
                for i,smi in enumerate(['CCO','COC']):
                    database.write(f'candidate_{i}',common.MassSpec(collision_energy=20,
                        root_canonical_smiles=smi,adduct='[M+H]+',masses=np.array([31.0184]),intens=np.array([1.])))
            finally:
                database.close()
            self.assertFalse(base.exists())
            self.assertEqual(len(list(base.parent.glob('preds_chunk_*.hdf5'))),4)
            reader=common.PredSpecDB(base)
            try:
                self.assertEqual({row[-1].root_canonical_smiles for row in reader.get_all_specs()},{'CCO','COC'})
            finally:
                reader.close()

    def test_two_unknowns_reuse_raw_spectra_but_have_different_scores(self):
        import numpy as np
        from ms_pred import common
        from msms_structure_elucidation import worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / 'weights.ckpt'
            weights.write_text('mock weights; model inference is replaced in this test')
            predicted = common.CompositeMassSpec([common.MassSpec(collision_energy=20,
                root_canonical_smiles='CCO', adduct='[M+H]+', masses=np.array([31.0184,45.0340]),
                intens=np.array([1.,.5]),int_frags=[1,3])])
            class Database:
                def __init__(self,*args): pass
                def get_all_specs(self): return [('CCO',predicted)]
                def close(self): pass
            files=[]
            for name, peaks in [('one','31.0184 1\n45.0340 .5'),('two','20 1\n30 .5')]:
                path=root/(name+'.ms')
                path.write_text('>parentmass 47.0491\n>ionization [M+H]+\n\n>collision 20 eV\n'+peaks+'\n')
                files.append(path)
            with patch.object(worker,'_run_model',return_value=root) as run, \
                 patch.object(common,'PredSpecDB',Database),patch.object(worker,'_structure_image',return_value=None):
                results=[worker.simulate(str(p),['CCO'],'C2H6O','iceberg',None,str(weights),str(weights),
                    str(root),'eV',instrument='Orbitrap',prediction_cache=str(root/'cache'))[0] for p in files]
            self.assertEqual(run.call_count,1)
            self.assertGreater(results[0]['entropy_similarity'],results[1]['entropy_similarity'])
            self.assertNotEqual(results[0]['matched_peaks'],results[1]['matched_peaks'])
            self.assertEqual(results[1]['prediction_cache']['reused_spectra'],1)


if __name__ == '__main__':
    unittest.main()
