"""Kubernetes worker relays keep separate CPU/GPU Jobs and archive before cleanup."""
import json
import types
import unittest
from unittest.mock import patch

from msms_structure_elucidation.mcp import worker_scheduler,kubernetes


class RelayTests(unittest.TestCase):
    def fixture(self,complete=False,archived=False,cpu=False):
        job='prepare_feature_list-'+'a'*16 if cpu else 'model_inference-'+'a'*16
        name='msms-'+job.replace('_','-')
        settings={'context':'test-context','namespace':'test-namespace','image':'runtime@sha256:test','shared_workspace':True}
        manifest=kubernetes.job_manifest(settings,name,['python','runner'],'cpu' if cpu else 'gpu')
        status={'state':'done' if complete else 'submitted','scheduler_confirmed':archived}
        record={'job_id':job,'name':name,'status':status,'manifest':manifest,'archived':archived}
        endpoint=types.SimpleNamespace(name='cluster',settings={'mcp':{'kubernetes':settings}})
        calls=[]
        endpoint.call=lambda operation,payload:calls.append((operation,payload))
        actual={'metadata':{'name':name},'status':{'conditions':[{'type':'Complete','status':'True'}] if complete else []}}
        pods=[{'metadata':{'labels':{'job-name':name}},'status':{'phase':'Succeeded' if complete else 'Running',
            'containerStatuses':[{'state':{'terminated':{'exitCode':0}}}] if complete else []}}]
        return endpoint,record,actual,pods,calls

    def test_completed_gpu_job_is_confirmed_and_archived_before_deletion(self):
        endpoint,record,job,pods,updates=self.fixture(complete=True)
        commands=[]
        def api(settings,*args,**kwargs):
            commands.append(args)
            body={'items':[job]} if args[1]=='jobs' else {'items':pods}
            return types.SimpleNamespace(returncode=0,stdout='logs' if args[0]=='logs' else json.dumps(body),stderr='')
        with patch.object(worker_scheduler,'kubectl',side_effect=api):
            worker_scheduler.sync(endpoint,{'scheduler_jobs':[record]},'glacier')
        self.assertTrue(updates[0][1]['updates'][0]['success'])
        self.assertIn('records',updates[0][1]['updates'][0])
        self.assertFalse(any(command[0]=='delete' for command in commands))
        record['archived']=True;record['status']['scheduler_confirmed']=True
        with patch.object(worker_scheduler,'kubectl',side_effect=api):
            worker_scheduler.sync(endpoint,{'scheduler_jobs':[record]},'glacier')
        self.assertTrue(any(command[0]=='delete' for command in commands))
        self.assertTrue(updates[-1][1]['updates'][0]['deleted'])

    def test_missing_cpu_job_is_submitted_without_gpu_resources(self):
        endpoint,record,_,_,_=self.fixture(cpu=True)
        commands=[]
        def api(settings,*args,**kwargs):
            commands.append((args,kwargs))
            self.assertEqual(settings['context'],'test-context')
            self.assertEqual(settings['namespace'],'test-namespace')
            return types.SimpleNamespace(returncode=0,stdout=json.dumps({'items':[]}),stderr='')
        with patch.object(worker_scheduler,'kubectl',side_effect=api):
            worker_scheduler.sync(endpoint,{'scheduler_jobs':[record]},'glacier')
        submission=next(kwargs for args,kwargs in commands if args[0]=='create')
        spec=json.loads(submission['input'])['items'][0]
        resources=spec['spec']['template']['spec']['containers'][0]['resources']
        self.assertNotIn('nvidia.com/gpu',resources['requests'])
        self.assertGreater(spec['spec']['activeDeadlineSeconds'],0)
        self.assertFalse(spec['spec']['template']['spec']['automountServiceAccountToken'])

    def test_failed_container_cannot_be_confirmed_as_a_success(self):
        endpoint,record,job,pods,updates=self.fixture(complete=True)
        pods[0]['status']['containerStatuses'][0]['state']['terminated']['exitCode']=1
        def api(settings,*args,**kwargs):
            value='failed' if args[0]=='logs' else json.dumps({'items':[job] if args[1]=='jobs' else pods})
            return types.SimpleNamespace(returncode=0,stdout=value,stderr='')
        with patch.object(worker_scheduler,'kubectl',side_effect=api):
            worker_scheduler.sync(endpoint,{'scheduler_jobs':[record]},'glacier')
        self.assertFalse(updates[0][1]['updates'][0]['success'])


if __name__=='__main__':unittest.main()
