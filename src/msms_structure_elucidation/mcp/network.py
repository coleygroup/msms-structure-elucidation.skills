"""Bound endpoint RPC and artifact transfers independently of the scheduling tick."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading


class Pending(RuntimeError):
    """The operation is still running, or its bounded executor is full."""


class NetworkQueue:
    """Retain operation results until the owner consumes them on a later tick."""

    def __init__(self, workers: int, transfers: int, submissions: int = 1):
        if min(workers, transfers, submissions) < 1:
            raise ValueError('Pool RPC, submission and transfer worker limits must be positive')
        self.limits = {'rpc': workers, 'submission': submissions, 'transfer': transfers}
        self.queue_limits = {name: count * 64 for name, count in self.limits.items()}
        self.queue_limits['submission'] = submissions * 256
        self.executors = {name: ThreadPoolExecutor(max_workers=count, thread_name_prefix='msms-' + name)
                          for name, count in self.limits.items()}
        self.futures = {}
        self.lock = threading.Lock()

    def call(self, key: tuple, function, transfer: bool = False):
        """Queue at most one operation per key; return its result without blocking."""
        writes = ('submit','worker-submit','cleanup','worker-cleanup','cancel','worker-cancel','worker-scheduler-update')
        pool = 'transfer' if transfer else ('submission' if key[0] in writes else 'rpc')
        token = (pool, key)
        with self.lock:
            future = self.futures.get(token)
            if future is None:
                pending = sum(kind == pool for kind, _ in self.futures)
                if pending >= self.queue_limits[pool]:
                    raise Pending('Endpoint operation queue is full')
                # Each purpose has its own bounded executor and FIFO waiting list.
                # Retained polling replies cannot consume submission capacity.
                self.futures[token] = self.executors[pool].submit(function)
                raise Pending('Endpoint operation started')
            if not future.done():
                raise Pending('Endpoint operation is running')
            del self.futures[token]
        return future.result()

    def discard_finished(self, live_jobs: set[str]) -> None:
        """Retire completed replies for jobs cancelled before their replies were consumed."""
        with self.lock:
            for token, future in list(self.futures.items()):
                _, key = token
                if key[0] in ('submit', 'poll', 'collect', 'cleanup') and key[-1] not in live_jobs and future.done():
                    del self.futures[token]

    def close(self, cancel_waiting_reads: bool = False) -> None:
        """Finish bounded operations before an embedding owner releases its resources."""
        if cancel_waiting_reads:
            with self.lock:
                for (_,key),future in self.futures.items():
                    if key[0] in ('poll','probe','worker-poll','worker-probe'):
                        future.cancel()  # Running operations and write intents continue.
        for executor in self.executors.values():
            executor.shutdown(wait=True)

    def describe(self) -> dict:
        """Report active transport work separately from replies awaiting consumption."""
        with self.lock:
            return {'limits': dict(self.limits), 'queue_limits': dict(self.queue_limits),
                    'active': {name: sum(kind == name and task.running()
                                        for (kind, _), task in self.futures.items()) for name in self.limits},
                    'queued': {name: sum(kind == name and not task.running() and not task.done()
                                        for (kind, _), task in self.futures.items()) for name in self.limits},
                    'retained': {name: sum(kind == name for kind,_ in self.futures) for name in self.limits},
                    'replies_ready_by_pool': {name: sum(kind == name and task.done()
                        for (kind,_),task in self.futures.items()) for name in self.limits},
                    'replies_ready': sum(task.done() for task in self.futures.values())}
