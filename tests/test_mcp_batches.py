"""Bulk scheduling preserves exact requested spectra and small-request latency."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp import batches
from msms_structure_elucidation.mcp.features import prepare_list
from msms_structure_elucidation.mcp.jobs import read_json, write_json
from msms_structure_elucidation.mcp.raw_outputs import publish_native, missing_groups
import test_mcp_features as fixture_module


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.FeatureTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root,self.store,self.backend = self.fixture.root,self.fixture.store,self.fixture.backend
        self.controller = self.backend.controller
        self.fixture.config['mcp'].update(prediction_group_spectra=131072,prediction_group_min_spectra=100001,
            max_group_candidates=131072,prediction_coalesce_seconds=60,prediction_coalesce_max_seconds=300)

    def ready(self, name, smiles, energies):
        job = self.fixture.feature(name)
        self.fixture.ready(job,smiles,name)
        folder = self.store.path(job)/'retrieval'
        request = read_json(folder/'request.json')
        request['raw_cache_identity'] = {'model':'frozen'}
        request['conditions']['collision_energies'] = energies
        request['inference'].update(python='scientific-python',native_source='frozen-native')
        request['inference']['arguments'].update(energies=energies,adduct='[M+H]+',instrument='Orbitrap')
        write_json(folder/'request.json',request)
        payload = read_json(folder/'native.json')
        payload['options'].update(save_dir=str(folder),dataset_labels=str(folder/'labels.tsv'))
        payload['entries'] = [[smi,'old_label',energy,'[M+H]+','Orbitrap',smi,None]
                              for smi in smiles for energy in energies]
        write_json(folder/'native.json',payload)
        with self.controller.connect() as db:
            self.controller._ingest(db,job)
        return job

    def groups(self):
        with self.controller.connect() as db:
            self.controller._groups(db)
        return self.fixture.jobs('model_inference')

    def test_small_request_starts_without_waiting_for_threshold_and_cpu_precedes_gpu(self):
        self.ready('small',['C','CC'],[10,20])
        group, = self.groups()
        params = self.store.request(group)['params']
        self.assertEqual(self.store.status(group)['state'],'staging')
        self.assertNotIn(group,self.fixture.core.submitted)
        plan = batches.read_plan(params['batch_plan'])
        self.assertEqual(plan['spectra'],4)
        self.assertEqual(plan['reason'],'small_workload')
        builder, = self.fixture.jobs(batches.BUILD_TASK)
        # The normal CPU service can assemble manifests without model weights or a GPU.
        config = {'mcp':{'backend':'slurm','feature_worker_idle_seconds':0}}
        service,_ = self.store.create('prepare_feature_list',{'features':[],'scores':[builder]},kind='cpu',config=config)
        self.store.set_status(service,'running')
        result = prepare_list(self.store.path(service),config)
        self.assertEqual(result['built_groups'],[builder])
        self.assertEqual(result['scored_jobs'],[])
        self.assertEqual(result['held_scores'],[])
        self.groups()
        self.assertIn(group,self.fixture.core.submitted)
        self.assertEqual(self.store.status(group)['state'],'running')

    def test_bulk_deduplicates_overlapping_unknowns_and_keeps_per_structure_energies(self):
        smiles = [f'molecule_{i:05}' for i in range(35000)]
        self.ready('a',smiles,[10,20])
        self.ready('b',smiles[10000:]+['other'],[20,30])
        # 35k * 2 + 25k * 1 + 2 < bulk minimum: genuinely final remainder starts promptly.
        group, = self.groups()
        params = self.store.request(group)['params']
        plan = batches.read_plan(params['batch_plan'])
        self.assertEqual(plan['spectra'],95002)
        self.assertEqual(plan['reason'],'compatible_remainder')
        result = batches.build({'plan':params['batch_plan'],'prepared':params['prepared']})
        entries = read_json(Path(params['prepared']))['entries']
        self.assertEqual(result['spectra'],95002)
        self.assertEqual(len({(row[0],row[2]) for row in entries}),95002)
        self.assertNotIn((smiles[0],30),{(row[0],row[2]) for row in entries})
        self.assertLess(len(json.dumps(params)),2000)
        self.assertEqual(len({row[1] for row in entries}),35001)

    def test_large_campaign_builds_above_100k_and_remainder_does_not_wait(self):
        smiles = [f'molecule_{i:05}' for i in range(45000)]
        self.ready('large',smiles,[10,20,30])
        groups = self.groups()
        self.assertEqual(len(groups),2)
        counts = sorted(self.store.request(job)['params']['expected_spectra'] for job in groups)
        self.assertGreater(counts[-1],100000)
        self.assertEqual(sum(counts),135000)
        self.assertLess(counts[0],10000)
        self.assertEqual({batches.read_plan(self.store.request(job)['params']['batch_plan'])['reason'] for job in groups},
                         {'bulk','small_workload'})

    def test_growing_campaign_coalesces_but_wait_is_bounded(self):
        self.ready('first',[f'm{i}' for i in range(10000)],[10,20,30])
        self.fixture.feature('still_preparing')
        with patch('msms_structure_elucidation.mcp.batches.time.time',return_value=1000):
            self.assertEqual(self.groups(),[])
        with patch('msms_structure_elucidation.mcp.batches.time.time',return_value=1061):
            group, = self.groups()
        self.assertEqual(batches.read_plan(self.store.request(group)['params']['batch_plan'])['reason'],'compatible_remainder')

    def test_changed_weights_never_share_a_batch(self):
        self.ready('a',['C'],[10])
        b = self.ready('b',['CC'],[20])
        # Different approved identity, even if all runtime knobs agree.
        with self.controller.connect() as db:
            payload = json.loads(db.execute('SELECT payload FROM conditions WHERE id=?',('b',)).fetchone()[0])
            payload['raw_cache_identity'] = {'weights':'different'}
            db.execute('UPDATE conditions SET payload=? WHERE id=?',(json.dumps(payload),'b'));db.commit()
        self.assertEqual(len(self.groups()),2)

    def test_iceberg_merges_requested_energies_and_assigns_distinct_labels(self):
        prepared = self.root/'iceberg.json'
        write_json(prepared,{'rows':[{'smiles':'C','spec':'same','inchikey':'a','collision_energies':['10','20']},
                                     {'smiles':'CC','spec':'same','inchikey':'b','collision_energies':['30']}]})
        plan = self.root/'plan.json'
        write_json(plan,{'model':'iceberg','expected_energies':{'C':[10,20],'CC':[30]},'spectra':3,'reason':'small_workload',
            'parts':[{'condition':'a','prepared':str(prepared),'smiles':['C'],'energies':[10,20]},
                     {'condition':'b','prepared':str(prepared),'smiles':['CC'],'energies':[30]}]})
        target = self.root/'merged.json'
        batches.build({'plan':str(plan),'prepared':str(target)})
        rows = read_json(target)['rows']
        self.assertEqual([r['collision_energies'] for r in rows],[['10','20'],['30']])
        self.assertEqual(len({r['spec'] for r in rows}),2)


class PublicationTests(unittest.TestCase):
    def test_native_bundle_validated_once_then_published_without_cartesian_extra_energies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); plan=root/'plan.json'
            write_json(plan,{'expected_energies':{'C':[10,20],'CC':[30]}})
            spectra = [('C',{'10':[[12.,1.]],'20':[[12.,.5]]}),('CC',{'30':[[24.,1.]]})]
            opens=[]
            class Reader:
                def __init__(self,*args,**kwargs):opens.append(1)
                def get_all_specs(self):
                    return [(types.SimpleNamespace(root_canonical_smiles=smi,spectra=peaks),) for smi,peaks in spectra]
                def close(self):pass
            common = types.SimpleNamespace(PredSpecDB=Reader)
            params={'batch_plan':str(plan),'expected_spectra':3,'folder':str(root),'native_source':'frozen',
                    'raw_cache_root':str(root/'cache'),'raw_cache_identity':{'weights':'approved'}}
            config={'models':{'simulator':{'ms_pred_src':str(root)}}}
            with patch.dict(sys.modules,{'ms_pred':types.SimpleNamespace(common=common)}), \
                 patch('msms_structure_elucidation.prediction_cache.source_fingerprint',return_value='frozen'), \
                 patch('msms_structure_elucidation.worker._serial_spec',side_effect=lambda p:p.spectra), \
                 patch('msms_structure_elucidation.worker._serial_fragment_ids',side_effect=lambda p:{e:['1'] for e in p.spectra}):
                receipt=publish_native(params,config)
                self.assertEqual(receipt['predicted_spectra'],3)
                self.assertEqual(len(opens),1)
                self.assertEqual(missing_groups(root/'cache',params['raw_cache_identity'],['C'],[10,20,30]),{(30,):['C']})
                # Invalid extra energies must fail before writing even the valid first molecule.
                params['raw_cache_root']=str(root/'invalid-cache')
                spectra[1][1]['40']=[[24.,1.]]
                with self.assertRaisesRegex(RuntimeError,'energy coverage'):
                    publish_native(params,config)
                self.assertFalse(list((root/'invalid-cache').glob('*/spectra.sqlite3')))
