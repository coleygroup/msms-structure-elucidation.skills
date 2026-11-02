"""Measure concurrent CPU handoffs on an isolated local or NFS job root; no model/GPU work.

Run with PYTHONPATH pointing at the revision under test. The generated fixture
registers only in subprocesses launched by this benchmark. Every request returns
its token and all CPU-ready/resumption timestamps; it produces no scientific spectra.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
from pathlib import Path

from msms_structure_elucidation.mcp.backends import LocalBackend
from msms_structure_elucidation.mcp.jobs import JobStore, write_json

FIXTURE = '''import os,time
from msms_structure_elucidation.mcp import tasks,pipeline,backends
backends.hostprobe.gpus=lambda: []
def fixture(params,folder,config,devices,log):
    assert not os.environ.get('CUDA_VISIBLE_DEVICES')
    store=pipeline.JobStore(folder.parent);job=folder.name
    backend=backends.LocalBackend(store,config,config['_mcp_python'])
    turns=[]
    for n in range(3):
        store.set_phase(job,'waiting_gpu');time.sleep(.03)
        requested=time.time();pipeline.resume_cpu(store,job,config,backend)
        turns.append({'requested':requested,'resumed':time.time()});time.sleep(.02)
    return {'token':params['token'],'turns':turns,'cuda_visible':os.environ.get('CUDA_VISIBLE_DEVICES')}
tasks.RUNNERS['scheduler_fixture']=fixture
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=32)
    parser.add_argument('--cpu-slots', type=int, default=4)
    parser.add_argument('--max-live', type=int, default=8)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 256 or min(args.cpu_slots, args.max_live, args.timeout) <= 0:
        parser.error('Use 1–256 jobs and positive resource/time bounds')
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = args.output / 'runtime'
    runtime.mkdir()
    (runtime / 'sitecustomize.py').write_text(FIXTURE)
    os.environ['PYTHONPATH'] = str(runtime.resolve()) + os.pathsep + os.environ.get('PYTHONPATH', '')
    from msms_structure_elucidation.mcp import backends
    backends.hostprobe.gpus = lambda: []
    config = {'_mcp_python': sys.executable, 'mcp': {'backend': 'local', 'scheduler_process': True,
        'max_cpu_jobs': args.cpu_slots, 'max_prepared_jobs': args.max_live,
        'max_live_pipelines': args.max_live, 'min_free_memory_gb': 0}}
    store = JobStore(args.output / 'jobs')
    jobs = [store.create('scheduler_fixture', {'token': n}, kind='pipeline', config=config)[0]
            for n in range(args.jobs)]
    backend = LocalBackend(store, config, sys.executable)
    samples = []
    start = time.monotonic()
    completed = False
    try:
        backend.submit(jobs[0])
        while time.monotonic() - start < args.timeout:
            # A baseline without detached dispatch is driven by this same client.
            backend.pump()
            rows = [store.status(j) for j in jobs]
            sample = {'seconds': time.monotonic() - start,
                'live': sum(r['state'] in ('running', 'submitted') for r in rows),
                'cpu': sum(r['state'] in ('running', 'submitted') and r.get('phase') not in
                           ('waiting_gpu', 'waiting_cpu', 'waiting_cache') for r in rows),
                'done': sum(r['state'] == 'done' for r in rows)}
            samples.append(sample)
            if all(r['state'] == 'done' for r in rows):
                completed = True
                break
            if any(r['state'] in ('failed', 'cancelled') for r in rows):
                break
            time.sleep(.25)
        elapsed = time.monotonic() - start
        results = [store.result(j) for j in jobs if store.status(j)['state'] == 'done']
        waits = [t['resumed'] - t['requested'] for r in results for t in r['turns']]
        # ponytail: self-check — require complete request tokens before a successful exit.
        receipt = {'host': socket.gethostname(), 'scope': 'CPU scheduler fixture; no model inference or GPU utilization claim',
            'complete': completed, 'expected_jobs': args.jobs, 'completed_jobs': len(results),
            'tokens_valid': sorted(r['token'] for r in results) == list(range(args.jobs)),
            'validated_handoffs': sum(len(r['turns']) for r in results), 'elapsed_seconds': elapsed,
            'jobs_per_second': len(results) / elapsed, 'cpu_slots': args.cpu_slots,
            'configured_max_live': args.max_live, 'peak_live_observed': max(s['live'] for s in samples),
            'peak_cpu_observed': max(s['cpu'] for s in samples),
            'max_cpu_resume_wait_seconds': max(waits, default=0),
            'median_cpu_resume_wait_seconds': statistics.median(waits) if waits else None,
            'samples': samples}
        write_json(args.output / 'receipt.json', receipt)
        print(json.dumps({k: v for k, v in receipt.items() if k != 'samples'}), flush=True)
        return 0 if completed and receipt['tokens_valid'] else 1
    finally:
        for jid in jobs:
            if store.status(jid)['state'] in ('queued', 'submitted', 'running'):
                backend.cancel(jid)
        write_json(store.root / 'scheduler-stop.json', {'reason': 'bounded benchmark finished'})
        # Wait for this benchmark's detached owner to acknowledge shutdown before archiving.
        deadline = time.monotonic() + 10
        while (store.root / 'scheduler-service.json').exists() and time.monotonic() < deadline:
            if json.loads((store.root / 'scheduler-service.json').read_text()).get('state') == 'stopped':
                break
            time.sleep(.1)


if __name__ == '__main__':
    sys.exit(main())
