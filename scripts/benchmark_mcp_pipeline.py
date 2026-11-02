"""Measure real MCP job execution and GPU utilization on a frozen request manifest.

Run separately under each checkout's PYTHONPATH with identical scientific settings.
Inputs and caches belong in an ignored output directory; this does not contact clients.
The transport layer is excluded, while native preparation, inference and scoring run.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from msms_structure_elucidation.config import settings
from msms_structure_elucidation.mcp import backends, tasks
from msms_structure_elucidation.mcp.jobs import FINISHED, JobStore, write_json


def scientific_payload(result: dict) -> list:
    """Keep predicted peaks, fragments and experimental scores for exact cross-run checks."""
    rows = result.get('predictions', result.get('candidates', []))
    fields = ('smiles', 'predicted_spectra', 'predicted_fragment_ids', 'entropy_similarity',
              'explained_intensity', 'matched_peaks', 'energy_alignment', 'annotated_peaks')
    return sorted([{key: row[key] for key in fields if key in row} for row in rows], key=lambda r: r['smiles'])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--requests', required=True, help='JSON list of {task, params, inputs}')
    parser.add_argument('--output', required=True)
    parser.add_argument('--gpu', type=int, required=True)
    parser.add_argument('--timeout', type=float, default=3600)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ['XDG_CACHE_HOME'] = str(root / 'native_cache')
    os.environ['MPLCONFIGDIR'] = str(root / 'matplotlib')
    config_path = Path(args.config).resolve()
    os.environ['MSMS_CONFIG'] = str(config_path)
    config = settings(str(config_path))
    config['_mcp_python'] = sys.executable
    config['mcp']['backend'] = 'local'
    # Keep the comparison on one explicitly selected physical GPU. The child's
    # backend sees the same restriction through this immutable config snapshot.
    config['mcp']['gpu_devices'] = [args.gpu]
    requests_path = Path(args.requests).resolve()
    requests = json.loads(requests_path.read_text())
    store = JobStore(root / 'jobs')
    backend = backends.LocalBackend(store, config, sys.executable)
    # Older revisions have no gpu_devices setting. Limit their GPU picker only
    # in the harness; resource scheduling and model code are otherwise unchanged.
    original = backends.free_gpus
    backends.free_gpus = lambda fraction, reserved: [i for i in original(fraction, reserved) if i == args.gpu]
    if not backends.free_gpus(.8, set()):
        raise RuntimeError('Selected GPU is busy or has insufficient free memory')
    write_json(root / 'config.json', config)
    hardware = subprocess.check_output(['nvidia-smi', '-i', str(args.gpu),
        '--query-gpu=uuid,name,driver_version,memory.total', '--format=csv,noheader,nounits'], text=True).strip()
    uuid = next(csv.reader([hardware]))[0].strip()
    start = time.monotonic()
    samples = []
    foreign = set()
    sampler = subprocess.Popen(['nvidia-smi', '-i', str(args.gpu),
        '--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,power.draw',
        '--format=csv,noheader,nounits', '-lms', '500'], stdout=subprocess.PIPE, text=True)
    def sample():
        with (root / 'gpu.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['elapsed_s', 'timestamp', 'gpu_util_pct', 'memory_util_pct', 'memory_mib', 'power_w'])
            for line in sampler.stdout:
                values = next(csv.reader([line]))
                if len(values) == 5:
                    elapsed = time.monotonic() - start
                    writer.writerow([elapsed, *values])
                    stream.flush()
                    samples.append((elapsed, float(values[1]), float(values[3])))
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    jobs = []
    try:
        for request in requests:
            task = request['task']
            job, _ = store.create(task, request['params'], request.get('inputs', {}), tasks.KINDS[task])
            write_json(store.path(job) / 'config.json', config)
            jobs.append(job)
        last = {}
        next_audit = 0.
        with (root / 'timeline.jsonl').open('w') as timeline, (root / 'gpu-processes.jsonl').open('w') as audit:
            while True:
                backend.pump()
                states = {job: backend.poll(job) for job in jobs}
                for row in store.list(100_000):
                    job = row['job_id']
                    snapshot = store.status(job)
                    if last.get(job) != snapshot:
                        timeline.write(json.dumps({'elapsed_s': time.monotonic()-start, 'job_id': job,
                                                   **snapshot}) + '\n')
                        timeline.flush()
                        last[job] = snapshot
                if time.monotonic() - start >= next_audit:
                    query = subprocess.run(['nvidia-smi', '-i', str(args.gpu),
                        '--query-compute-apps=gpu_uuid,pid,process_name,used_memory',
                        '--format=csv,noheader,nounits'], capture_output=True, text=True, check=True)
                    sessions = {store.launch(row['job_id']).get('pid') for row in store.list(100_000)}
                    processes = []
                    for row in csv.reader(query.stdout.splitlines()):
                        if len(row) != 4 or row[0].strip() != uuid:
                            continue
                        pid = int(row[1].strip())
                        stat = Path(f'/proc/{pid}/stat')
                        try:
                            session = int(stat.read_text().rsplit(')', 1)[1].split()[3])
                        except FileNotFoundError:  # A short-lived GPU process may exit between the two probes.
                            session = None
                        processes.append({'pid': pid, 'session': session, 'name': row[2].strip()})
                        if session is not None and session not in sessions:
                            foreign.add(pid)
                    audit.write(json.dumps({'elapsed_s': time.monotonic()-start, 'processes': processes})+'\n')
                    audit.flush()
                    if foreign:
                        raise RuntimeError(f'Foreign GPU processes detected; benchmark invalid: {sorted(foreign)}')
                    next_audit = time.monotonic()-start+2.
                if all(state['state'] in FINISHED for state in states.values()):
                    break
                if time.monotonic() - start > args.timeout:
                    raise TimeoutError('Benchmark exceeded --timeout')
                time.sleep(.2)
        elapsed = time.monotonic() - start
        results = []
        for job, request in zip(jobs, requests):
            status = store.status(job)
            result = store.result(job) if status['state'] == 'done' else {}
            payload = scientific_payload(result)
            encoded = json.dumps({'spectra': payload, 'formula_hypotheses': result.get('formula_hypotheses')},
                                 sort_keys=True, separators=(',', ':')).encode()
            results.append({'job_id': job, 'task': request['task'], 'state': status['state'],
                            'error': status.get('error'), 'structures': len(payload),
                            'spectra': sum(len(row.get('predicted_spectra', {})) for row in payload),
                            'scientific_sha256': hashlib.sha256(encoded).hexdigest()})
            # ponytail: self-check
            if request['task'] in ('score_candidates', 'predict_spectra') and status['state'] == 'done':
                if len(payload) != len(request['params']['smiles']):
                    raise RuntimeError(f'Incomplete structure accounting in {job}')
        active = [row for row in samples if row[0] <= elapsed]
        summary = {'elapsed_s': elapsed, 'gpu': args.gpu, 'sample_interval_s': .5,
            'hardware': hardware, 'foreign_gpu_pids': sorted(foreign),
            'host_loadavg': list(os.getloadavg()) if hasattr(os, 'getloadavg') else None,
            'gpu_samples': len(active), 'gpu_mean_util_pct': sum(r[1] for r in active)/len(active) if active else None,
            'gpu_active_fraction': sum(r[1] > 0 for r in active)/len(active) if active else None,
            'gpu_median_util_pct': statistics.median(r[1] for r in active) if active else None,
            'gpu_first_active_s': next((r[0] for r in active if r[1] > 0), None),
            'gpu_mean_when_active_pct': statistics.mean(r[1] for r in active if r[1] > 0)
                if any(r[1] > 0 for r in active) else 0.,
            'gpu_peak_memory_mib': max((r[2] for r in active), default=None),
            'requests_sha256': hashlib.sha256(requests_path.read_bytes()).hexdigest(), 'results': results}
        write_json(root / 'summary.json', summary)
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if active and not foreign and all(row['state'] == 'done' for row in results) else 1
    finally:
        for job in jobs:
            if store.status(job)['state'] not in FINISHED:
                backend.cancel(job)
        sampler.terminate()
        sampler.wait(timeout=5)
        thread.join(timeout=5)
        for process in backend.processes.values():
            process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
