"""Preparation reservations survive scoring pressure and safe live handoffs."""
import unittest
import copy
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import test_mcp_features as fixtures
from msms_structure_elucidation.mcp import cpu_pool
from msms_structure_elucidation.mcp.features import prepare_list
from msms_structure_elucidation.mcp.jobs import read_json,write_json


class CpuPoolTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.FeatureTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store,self.backend,self.core=self.f.store,self.f.backend,self.f.core
        self.config=self.f.config;self.config['mcp'].update(feature_cpu_workers=3,feature_preparation_workers=2)

    def task(self,kind='score_prepared_feature',**params):
        return self.store.create(kind,params or {'prepared':'unused'},kind='cpu',config=self.config)[0]

    def workers(self):
        return self.f.jobs('prepare_feature_list')

    def test_scoring_backlog_cannot_use_reserved_preparation_processes(self):
        scores=[self.task(number=i) for i in range(8)]
        self.backend.pump()
        self.assertEqual(len(self.workers()),1)
        self.assertEqual(cpu_pool.role(self.store,self.workers()[0]),cpu_pool.SCORING)
        first,second=self.f.feature('first'),self.f.feature('second')
        self.backend.pump()
        roles=Counter(cpu_pool.role(self.store,w) for w in self.workers())
        self.assertEqual(roles,{'preparation':2,'scoring':1})
        for feature in (first,second):
            worker=read_json(self.store.path(feature)/'feature-stage.json')['prepare_job']
            self.assertEqual(cpu_pool.role(self.store,worker),'preparation')
        for job in scores:
            self.assertEqual(cpu_pool.role(self.store,self.store.launch(job)['worker_job']),'scoring')

    def test_reserved_roles_reject_cross_assignments(self):
        feature=self.f.feature('feature');score=self.task(number=1);build=self.task('build_prediction_group',plan='unused')
        self.backend.pump()
        prep=next(w for w in self.workers() if cpu_pool.role(self.store,w)=='preparation')
        scorer=next(w for w in self.workers() if cpu_pool.role(self.store,w)=='scoring')
        with self.assertRaisesRegex(ValueError,'Reserved preparation'):
            self.backend.controller._assign(prep,[],[score])
        with self.assertRaisesRegex(ValueError,'Scoring workers'):
            self.backend.controller._assign(scorer,[feature],[])
        with self.assertRaisesRegex(ValueError,'Scoring workers'):
            self.backend.controller._assign(scorer,[],[build])
        self.assertEqual(cpu_pool.role(self.store,self.store.launch(build)['worker_job']),'preparation')

    def test_preparation_pool_prioritizes_batch_assembly(self):
        feature=self.f.feature('prep');build=self.task('build_prediction_group',plan='unused',prepared='unused')
        config={'mcp':{'backend':'slurm','feature_worker_idle_seconds':0}}
        worker,_=self.store.create('prepare_feature_list',{'features':[feature],'scores':[build],
            'cpu_role':'preparation'},kind='cpu',config=config)
        self.store.set_status(worker,'running');order=[]
        with patch('msms_structure_elucidation.mcp.batches.build',side_effect=lambda p:order.append('build') or {}), \
                patch('msms_structure_elucidation.mcp.features._prepare_feature',side_effect=lambda s,f:order.append('prepare') or f):
            result=prepare_list(self.store.path(worker),config)
        self.assertEqual(order,['build','prepare'])
        self.assertEqual(result['built_groups'],[build])
        self.assertEqual(read_json(self.store.path(worker)/'worker-progress.json')['cpu_role'],'preparation')

    def test_legacy_handoff_waits_for_exit_and_preserves_completed_work(self):
        current,pending=self.f.feature('current'),self.f.feature('pending')
        completed=self.task(number=1);queued=self.task(number=2);builder=self.task('build_prediction_group',plan='unused')
        worker,_=self.store.create('prepare_feature_list',{'features':[current,pending],'scores':[completed,queued,builder]},
            kind='cpu',config=self.config)
        self.core.submit(worker);self.backend.controller._assign(worker,[current,pending],[completed,queued,builder])
        cpu_pool.retire(self.backend.controller,worker)
        self.assertEqual(read_json(self.store.path(worker)/'assignments.json')['features'],[])
        cpu_pool.recover(self.backend.controller)
        self.assertEqual(self.store.launch(queued)['worker_job'],worker)
        self.assertEqual(read_json(self.store.path(pending)/'feature-stage.json')['prepare_job'],worker)
        # The old process finishes whichever task it selected before the handoff.
        self.f.ready(current,['C']);self.core.finish(completed,{'prediction_complete':True})
        self.core.finish(worker,{'processed_features':[current],'scored_jobs':[completed]})
        cpu_pool.recover(self.backend.controller)
        self.assertEqual(self.store.status(completed)['state'],'done')
        self.assertTrue((self.store.path(current)/'retrieval/ready.json').exists())
        self.assertEqual(read_json(self.store.path(pending)/'feature-stage.json'),{})
        self.assertEqual(self.store.status(pending)['state'],'queued')
        self.assertEqual(self.store.launch(queued),{})
        self.assertEqual(self.store.launch(builder),{})
        self.backend.pump()
        self.assertEqual(self.store.status(completed)['state'],'done')
        self.assertEqual(cpu_pool.role(self.store,self.store.launch(queued)['worker_job']),'scoring')
        self.assertEqual(cpu_pool.role(self.store,self.store.launch(builder)['worker_job']),'preparation')
        self.assertLessEqual(len([w for w in self.workers() if self.store.status(w)['state']=='running']),3)

    def test_limits_keep_a_scoring_slot_and_legacy_single_worker(self):
        self.assertEqual(cpu_pool.limits({'feature_cpu_workers':20,'feature_preparation_workers':16}),
                         {'preparation':16,'scoring':4})
        self.assertEqual(cpu_pool.limits({'feature_cpu_workers':2,'feature_preparation_workers':'auto'}),
                         {'preparation':1,'scoring':1})
        self.assertEqual(cpu_pool.limits({'feature_cpu_workers':1,'feature_preparation_workers':'auto'}),{'shared':1})
        with self.assertRaises(ValueError):cpu_pool.limits({'feature_cpu_workers':4,'feature_preparation_workers':4})

    def test_explicit_stage_counts_override_the_legacy_total(self):
        self.config['mcp'].update(feature_cpu_workers=1, feature_preparation_workers=2,
                                  feature_scoring_workers=2)
        for index in range(3):self.f.feature('prep'+str(index))
        for index in range(4):self.task(number=index)
        self.backend.pump()
        self.assertEqual(Counter(cpu_pool.role(self.store, w) for w in self.workers()),
                         {'preparation': 2, 'scoring': 2})
        with self.assertRaisesRegex(ValueError, 'positive preparation'):
            cpu_pool.limits({'feature_preparation_workers': 'auto', 'feature_scoring_workers': 2})

    def test_interrupted_handoff_retries_without_losing_original_assignments(self):
        feature=self.f.feature('waiting');worker,_=self.store.create('prepare_feature_list',
            {'features':[feature],'scores':[]},kind='cpu',config=self.config)
        self.core.submit(worker);self.backend.controller._assign(worker,[feature],[])
        handoffs=self.backend.controller.root/'cpu-role-handoffs';handoffs.mkdir()
        marker=handoffs/(worker+'.json')
        original=read_json(self.store.path(worker)/'assignments.json')
        write_json(marker,{'worker':worker,'assignments':original,'created':1})
        cpu_pool.retire(self.backend.controller,worker)
        self.assertEqual(read_json(marker)['assignments'],original)
        self.assertTrue(read_json(self.store.path(worker)/'assignments.json')['retire'])
        self.core.finish(worker,{'held_features':[]})
        cpu_pool.recover(self.backend.controller)
        self.assertEqual(self.store.status(feature)['state'],'queued')

    def test_queue_policy_changes_reuse_preparation_services_and_keep_task_configs(self):
        first=self.f.feature('old-config');self.backend.pump()
        worker=read_json(self.store.path(first)/'feature-stage.json')['prepare_job']
        self.f.ready(first,['C']);write_json(self.store.path(worker)/'worker-progress.json',
            {'accepting':True,'processed_features':1})
        second=self.f.feature('new-config');path=self.store.path(second)/'config.json'
        updated=read_json(path);updated['_config_digest']='new-provenance-hash'
        updated.setdefault('mcp',{}).update(feature_cpu_workers=20,feature_preparation_workers=16)
        write_json(path,updated);self.backend.pump()
        self.assertEqual(read_json(self.store.path(second)/'feature-stage.json')['prepare_job'],worker)
        self.assertEqual(read_json(path),updated)
        self.assertEqual(len(self.workers()),1)

    def test_scientific_and_environment_changes_keep_cpu_services_separate(self):
        base={'models':{'simulator':{'python':'/env/python','glacier_ckpt':'/weights/a.ckpt'}},
            'pipeline':{'stepped_aggregation':'arithmetic_mean'},'mcp':{'backend':'local'},
            '_config_path':'/project/config.yaml'}
        for change in ('weights','aggregation','environment','relative_paths'):
            other=copy.deepcopy(base)
            if change=='weights':other['models']['simulator']['glacier_ckpt']='/weights/b.ckpt'
            elif change=='aggregation':other['pipeline']['stepped_aggregation']='max'
            elif change=='environment':other['mcp']['worker_environment']={'OMP_NUM_THREADS':'4'}
            else:other['_config_path']='/another-project/config.yaml'
            with self.subTest(change=change):
                self.assertNotEqual(cpu_pool.service_identity(base),cpu_pool.service_identity(other))

    def test_slow_prediction_planning_does_not_delay_cpu_admission(self):
        feature=self.f.feature('prepare-before-planning')
        with patch.object(self.backend.controller,'_groups',side_effect=RuntimeError('slow storage')):
            with self.assertRaisesRegex(RuntimeError,'slow storage'):self.backend.pump()
        worker=read_json(self.store.path(feature)/'feature-stage.json')['prepare_job']
        self.assertIn(worker,self.core.submitted)
        self.assertEqual(cpu_pool.role(self.store,worker),'preparation')

    def test_new_builders_fill_remaining_slots_without_overbooking(self):
        self.f.feature('feature');self.task(number=1)
        with patch.object(self.backend.controller,'_groups',side_effect=lambda db:
                [self.task('build_prediction_group',plan='unused',number=i) for i in range(2)]):
            self.backend.pump()
        self.assertEqual(Counter(cpu_pool.role(self.store,w) for w in self.workers()),
                         {'preparation':2,'scoring':1})


class StageBudgetTests(unittest.TestCase):
    def test_shipped_single_gpu_defaults_keep_mirrored_configs_and_automatic_admission(self):
        from msms_structure_elucidation.config import read_yaml
        root = Path(__file__).resolve().parents[1]
        external = read_yaml(root/'configs/default.yaml')
        bundled = read_yaml(root/'src/msms_structure_elucidation/default.yaml')
        self.assertEqual(external, bundled)
        settings = external['mcp']
        self.assertEqual(cpu_pool.limits(settings), {'preparation':2, 'scoring':1})
        self.assertEqual(cpu_pool.cpu_job_limit(settings, 1), 4)

    def test_ratio_rounds_scoring_up_for_odd_gpu_counts(self):
        for gpus, preparation, scoring, output in [(1,2,1,1), (2,4,1,2), (3,6,2,3), (8,16,4,8)]:
            with self.subTest(gpus=gpus):
                settings = {'feature_pipeline': True, **cpu_pool.stage_defaults(gpus)}
                self.assertEqual(cpu_pool.limits(settings), {'preparation': preparation, 'scoring': scoring})
                self.assertEqual(cpu_pool.output_limit(settings, gpus), output)
                self.assertEqual(cpu_pool.cpu_job_limit(settings, gpus), preparation+scoring+output)
                settings.pop('max_cpu_jobs')
                self.assertEqual(cpu_pool.cpu_job_limit(settings, gpus), preparation+scoring+output)

    def test_explicit_host_cap_and_compatibility_path_are_preserved(self):
        settings = {'feature_pipeline': True, **cpu_pool.stage_defaults(8), 'max_cpu_jobs': 12}
        self.assertEqual(cpu_pool.cpu_job_limit(settings, 8), 12)
        self.assertEqual(cpu_pool.cpu_job_limit({'feature_pipeline': False}), 2)
        self.assertEqual(cpu_pool.cpu_job_limit({'feature_pipeline': True, 'feature_cpu_workers': 20,
            'feature_preparation_workers': 16, 'feature_output_workers': 8}), 28)
        self.assertEqual(cpu_pool.cpu_job_limit({'feature_pipeline': True, **cpu_pool.stage_defaults(8),
            'feature_cpu_output': False}, 8), 20)

    def test_auto_output_uses_effective_counts_not_a_stale_legacy_total(self):
        settings = {'feature_pipeline': True, 'feature_preparation_workers': 16,
            'feature_scoring_workers': 4, 'feature_cpu_workers': 1, 'feature_output_workers': 'auto'}
        self.assertEqual(cpu_pool.output_limit(settings, 8), 8)
        self.assertEqual(cpu_pool.cpu_job_limit(settings, 8), 28)



if __name__=='__main__':unittest.main()
