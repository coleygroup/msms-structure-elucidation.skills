"""Resource probes cannot steal virtual CPU tasks from the worker controller."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from msms_structure_elucidation.mcp.backends import SlurmBackend
from msms_structure_elucidation.mcp.kubernetes import KubernetesBackend
from msms_structure_elucidation.mcp.jobs import JobStore
from msms_structure_elucidation.mcp import endpoint


class StageOwnershipTests(unittest.TestCase):
    def test_raw_inventory_is_observational(self):
        with tempfile.TemporaryDirectory() as folder:
            backend=SimpleNamespace(describe=lambda:{'backend':'local'},
                pump=lambda: self.fail('Inventory must not drive the scheduler'))
            with patch.object(endpoint,'make_backend',return_value=backend), \
                    patch('msms_structure_elucidation.hostprobe.probe',return_value={}):
                result=endpoint.handle('probe',{'endpoint':{'job_root':folder}})
            self.assertEqual(result['active_jobs'],0)

    def test_scheduler_backends_never_submit_worker_owned_children(self):
        for backend_type in (SlurmBackend,KubernetesBackend):
            with self.subTest(backend=backend_type.__name__),tempfile.TemporaryDirectory() as folder:
                store=JobStore(Path(folder)/'jobs')
                config={'mcp':{'route_canary':False,'kubernetes':{'shared_workspace':True,'context':'test',
                        'namespace':'test','image':'runtime'}}}
                backend=backend_type(store,config)
                jobs=[store.create(task,{},kind='cpu')[0] for task in
                      ('publish_prediction_group','score_prepared_feature','build_prediction_group')]
                with patch('subprocess.run',side_effect=AssertionError('Unexpected scheduler submission')):
                    backend.pump()
                    for job in jobs:backend.submit(job)
                self.assertTrue(all(store.status(job)['state']=='queued' and not store.launch(job) for job in jobs))


if __name__=='__main__':unittest.main()
