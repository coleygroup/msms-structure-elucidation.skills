"""Slow endpoint I/O must preserve bounded dispatch, ownership and collection."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.mcp import backends, fleet
from msms_structure_elucidation.mcp.jobs import JobStore
from msms_structure_elucidation.mcp.network import NetworkQueue, Pending


class NetworkFairnessTests(unittest.TestCase):
    def test_write_buffer_can_feed_a_slow_reconciliation_pass_without_more_workers(self):
        queue=NetworkQueue(2,1,1);self.addCleanup(queue.close)
        self.assertEqual(queue.limits['submission'],1)
        self.assertEqual(queue.queue_limits['submission'],256)
        self.assertEqual(queue.queue_limits['rpc'],128)

    def test_shutdown_cancels_waiting_reads_and_preserves_queued_submission(self):
        queue=NetworkQueue(1,1);release=threading.Event();submitted=threading.Event();polled=threading.Event()
        self.addCleanup(queue.close);self.addCleanup(release.set)
        for key,action in ((('poll','host','running'),lambda:release.wait(3)),
                           (('poll','host','queued'),polled.set),(('submit','host','write'),submitted.set)):
            with self.assertRaises(Pending):queue.call(key,action)
        closer=threading.Thread(target=lambda:queue.close(cancel_waiting_reads=True));closer.start()
        deadline=time.monotonic()+2
        future=queue.futures[('rpc',('poll','host','queued'))]
        while not future.cancelled() and time.monotonic()<deadline:time.sleep(.005)
        self.assertTrue(future.cancelled());release.set();closer.join(2)
        self.assertFalse(closer.is_alive());self.assertTrue(submitted.is_set());self.assertFalse(polled.is_set())

    def test_submission_completes_while_the_poll_queue_is_full_and_blocked(self):
        queue = NetworkQueue(1, 1)
        released = threading.Event()
        submitted = threading.Event()
        self.addCleanup(queue.close)
        self.addCleanup(released.set)
        def blocking():
            released.wait(3)
        queue.queue_limits['rpc'] = 2
        with self.assertRaises(Pending):
            queue.call(('poll', 'host', 'first'), blocking)
        with self.assertRaises(Pending):
            queue.call(('poll', 'host', 'second'), blocking)
        with self.assertRaisesRegex(Pending, 'queue is full'):
            queue.call(('poll', 'host', 'overflow'), blocking)
        with self.assertRaises(Pending):
            queue.call(('submit', 'host', 'new'), submitted.set)
        self.assertTrue(submitted.wait(1))
        self.assertFalse(released.is_set())
        deadline = time.monotonic() + 3
        while True:
            try:
                queue.call(('submit', 'host', 'new'), submitted.set)
                break
            except Pending:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.005)
        self.assertFalse(any(pool=='submission' for pool,_ in queue.futures))

    def test_polling_completes_while_the_submission_queue_is_full_and_blocked(self):
        queue=NetworkQueue(1,1);release=threading.Event();polled=threading.Event()
        self.addCleanup(queue.close);self.addCleanup(release.set)
        queue.queue_limits['submission']=1
        with self.assertRaises(Pending):queue.call(('worker-submit','host','running'),lambda:release.wait(3))
        with self.assertRaisesRegex(Pending,'queue is full'):queue.call(('worker-submit','host','overflow'),lambda:None)
        with self.assertRaises(Pending):queue.call(('worker-poll','host','status'),polled.set)
        self.assertTrue(polled.wait(1));self.assertFalse(release.is_set())
        self.assertEqual(queue.describe()['active']['submission'],1)

    def test_cleanup_and_cancellation_do_not_wait_for_status_polling(self):
        queue=NetworkQueue(1,1);release=threading.Event();self.addCleanup(queue.close);self.addCleanup(release.set)
        with self.assertRaises(Pending):queue.call(('worker-poll','host','waiting'),lambda:release.wait(3))
        for operation in ('worker-cleanup','worker-cancel'):
            completed=threading.Event()
            with self.assertRaises(Pending):queue.call((operation,'host','done'),completed.set)
            self.assertTrue(completed.wait(1));self.assertFalse(release.is_set())

    def test_waiting_queue_and_running_workers_are_bounded_separately(self):
        queue = NetworkQueue(1, 1)
        queue.queue_limits['rpc'] = 3
        released = threading.Event()
        self.addCleanup(queue.close)
        self.addCleanup(released.set)
        for index in range(3):
            with self.assertRaises(Pending):
                queue.call(('poll', 'host', str(index)), lambda: released.wait(3))
        with self.assertRaisesRegex(Pending, 'queue is full'):
            queue.call(('poll', 'host', 'overflow'), lambda: None)
        stats = queue.describe()
        self.assertEqual(stats['active']['rpc'], 1)
        self.assertEqual(stats['queued']['rpc'], 2)


class AsyncPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / 'jobs')
        self.release = threading.Event()
        self.servers = {'slow': {'transport': 'local', 'job_root': str(self.root / 'slow'), 'max_jobs': 1},
                        'fast': {'transport': 'local', 'job_root': str(self.root / 'fast'), 'max_jobs': 1}}
        config = {'mcp': {'backend': 'pool', 'servers': self.servers, 'min_free_memory_gb': 0}}
        with patch.object(backends.hostprobe, 'gpus', return_value=[]):
            self.backend = fleet.FleetBackend(self.store, config)
        self.backend.cpu.pump = lambda: None
        self.addCleanup(self.backend.close)
        self.addCleanup(self.release.set)

    def child(self, number):
        return self.store.create('model_inference', {'arguments': {'model': 'glacier'}, 'n': number})[0]

    def test_inventory_refresh_retains_a_dictionary_snapshot(self):
        self.backend.snapshots['slow'] = (0, {'backend': 'local', 'max_concurrent_jobs': 2})
        with patch.object(self.backend, '_probe', side_effect=Pending('refresh pending')):
            description = self.backend.describe()
        self.assertEqual(description['servers']['slow'], {'backend': 'local', 'max_concurrent_jobs': 2})
        self.assertEqual(description['servers']['fast'], {'state': 'probing'})

    def test_stalled_server_does_not_occupy_another_servers_control_workers(self):
        self.backend.rpc_workers_per_server=1
        slow=self.backend.endpoints['slow'];fast=self.backend.endpoints['fast']
        started=threading.Event()
        def call(endpoint,operation,payload=None):
            if endpoint.name=='slow':
                started.set();self.release.wait(3)
            return {'server':endpoint.name}
        with patch.object(fleet.Endpoint,'call',new=call):
            for number in range(3):
                with self.assertRaises(Pending):self.backend._call(slow,'worker-poll',identity=str(number))
            self.assertTrue(started.wait(1))
            deadline=time.monotonic()+2
            while True:
                try:result=self.backend._call(fast,'worker-submit',identity='new');break
                except Pending:
                    self.assertLess(time.monotonic(),deadline);time.sleep(.005)
            self.assertEqual(result,{'server':'fast'})
            self.assertFalse(self.release.is_set())

    def test_same_servers_polling_and_submissions_have_independent_capacity(self):
        self.backend.rpc_workers_per_server=1
        endpoint=self.backend.endpoints['slow'];submitted=threading.Event()
        def call(remote,operation,payload=None):
            if operation=='worker-poll':self.release.wait(3)
            else:submitted.set()
            return {'operation':operation}
        with patch.object(fleet.Endpoint,'call',new=call):
            with self.assertRaises(Pending):self.backend._call(endpoint,'worker-poll',identity='old')
            with self.assertRaises(Pending):self.backend._call(endpoint,'worker-submit',identity='new')
            self.assertTrue(submitted.wait(1));self.assertFalse(self.release.is_set())
            stats=self.backend.server_rpc['slow'].describe()
            self.assertEqual(stats['active']['rpc'],1)
            self.assertEqual(stats['retained']['submission'],1)

    def until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.backend.pump()
            time.sleep(.005)
        self.assertTrue(condition())

    def test_slow_probe_does_not_block_another_endpoint_or_the_queue_lock(self):
        def call(remote, operation, payload=None, output=None):
            if remote.name == 'slow':
                self.release.wait(3)
            if operation == 'probe':
                return {'free_gpus': [0], 'max_concurrent_jobs': 1}
            if operation == 'submit':
                return {'job_id': 'model_inference-' + 'a' * 16}
            return {'status': {'state': 'running'}}
        job = self.child(1)
        with patch.object(fleet.Endpoint, 'call', new=call):
            self.until(lambda: self.store.launch(job).get('remote_job_id'))
            self.assertEqual(self.store.launch(job)['server'], 'fast')
            self.assertFalse(self.release.is_set())

    def test_prefetch_is_bounded_and_does_not_change_gpu_execution_limit(self):
        self.backend.endpoints.pop('slow')
        jobs = [self.child(n) for n in range(3)]
        def call(remote, operation, payload=None, output=None):
            if operation == 'probe':
                return {'free_gpus': [], 'max_concurrent_jobs': 1}
            if operation == 'submit':
                return {'job_id': payload['job_id']}
            return {'status': {'state': 'running'}}
        with patch.object(fleet.Endpoint, 'call', new=call):
            self.until(lambda: sum(bool(self.store.launch(job).get('remote_job_id')) for job in jobs) == 2)
            for _ in range(5):
                self.backend.pump()
                time.sleep(.005)
        self.assertEqual(sum(bool(self.store.launch(job)) for job in jobs), 2)
        self.assertEqual(self.backend.endpoints['fast'].settings['max_jobs'], 1)

    def test_transfer_does_not_prevent_the_next_group_from_being_submitted(self):
        self.backend.endpoints.pop('slow')
        first = self.child(1)
        transferred = threading.Event()
        def call(remote, operation, payload=None, output=None):
            if operation == 'probe':
                return {'free_gpus': [0], 'max_concurrent_jobs': 1}
            if operation == 'submit':
                return {'job_id': payload['job_id']}
            if operation == 'poll':
                return {'status': {'state': 'done'}, 'inventory': {'native_outputs/preds.hdf5': {}}}
            return {}
        def collect(*args):
            transferred.set()
            self.release.wait(3)
            raise RuntimeError('fixture interrupted transfer')
        with patch.object(fleet.Endpoint, 'call', new=call), patch.object(fleet, 'collect', side_effect=collect):
            self.until(transferred.is_set)
            second = self.child(2)
            self.until(lambda: self.store.launch(second).get('remote_job_id'))
            self.assertFalse(self.release.is_set())
            self.assertNotEqual(self.store.status(first)['state'], 'done')
            self.release.set()
            self.backend.close()

    def test_revision_aliases_share_one_hardware_queue_budget(self):
        for remote in self.backend.endpoints.values():
            remote.settings.update(resource_group='one-host', prefetch_jobs=0)
        jobs = [self.child(n) for n in range(3)]
        def call(remote, operation, payload=None, output=None):
            if operation == 'probe':
                return {'free_gpus': [0], 'max_concurrent_jobs': 1}
            if operation == 'submit':
                return {'job_id': payload['job_id']}
            return {'status': {'state': 'running'}}
        with patch.object(fleet.Endpoint, 'call', new=call):
            self.until(lambda: any(self.store.launch(job).get('remote_job_id') for job in jobs))
            for _ in range(5):
                self.backend.pump()
                time.sleep(.005)
        self.assertEqual(sum(bool(self.store.launch(job)) for job in jobs), 1)

    def test_children_from_a_running_legacy_parent_are_discovered_after_index_migration(self):
        parent, _ = self.store.create('score_candidates', {}, kind='pipeline')
        request = self.store.request(parent)
        request.pop('index_version')
        from msms_structure_elucidation.mcp.jobs import write_json
        write_json(self.store.path(parent) / 'request.json', request)
        self.store.active()
        child = self.child(1)
        (self.store.root / 'active' / child).unlink()
        self.store.set_phase(parent, 'waiting_gpu', prediction_jobs=[child])
        self.assertIn(child, {row['job_id'] for row in self.store.active()})
        self.assertIn(child, self.store.children(parent))


if __name__ == '__main__':
    unittest.main()
