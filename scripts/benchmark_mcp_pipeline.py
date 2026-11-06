"""Measure real MCP job execution and GPU utilization on a frozen request manifest.

Run separately under each checkout's PYTHONPATH with identical scientific settings.
Inputs and caches belong in an ignored output directory; this does not contact clients.
The transport layer is excluded, while native preparation, inference and scoring run.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from msms_structure_elucidation.config import settings
from msms_structure_elucidation.mcp import backends, tasks
from msms_structure_elucidation.mcp.jobs import FINISHED, JobStore, write_json


def inference_stages(store: JobStore) -> list[dict]:
    """Keep runner, native inference and CPU phases distinct, including startup/idle time."""
    stages = []
    for folder in sorted(store.root.glob('model_inference-*')):
        if not (folder / 'request.json').is_file():
            continue
        request = store.request(folder.name)
        status = store.status(folder.name)
        params = request['params']
        arguments = params['arguments']
        log = folder / 'collected/log.txt'
        if not log.is_file():
            log = folder / 'log.txt'
        text = log.read_text(errors='replace') if log.is_file() else ''
        native = re.findall(r'DONE \d+ batches / (\d+) rows in ([\d.]+)s', text)
        metrics = folder / 'gpu-metrics.json'
        if not metrics.is_file():
            metrics = folder / 'collected/gpu-metrics.json'
        stage = {'job_id': folder.name, 'state': status['state'],
                 'expected_spectra': params.get('expected_spectra',
                     len(arguments['smiles']) * len(set(arguments['energies']))),
                 'phase_seconds': status.get('phase_seconds', {}),
                 'runner_gpu_metrics': json.loads(metrics.read_text()) if metrics.is_file() else None,
                 'native_rows': sum(int(rows) for rows, _ in native) if native else None,
                 'native_seconds': sum(float(seconds) for _, seconds in native) if native else None}
        if native and stage['native_seconds']:
            stage['native_spectra_per_second'] = stage['native_rows'] / stage['native_seconds']
        # These native timestamps use the executing host's local timezone. All
        # CSV counters inside the interval count, including zeros and writer drain.
        stamps = re.findall(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}).*'
                            r'(?:writer process\(es\)|DONE \d+ batches)', text)
        samples = folder / 'gpu-samples.csv'
        if not samples.is_file():
            samples = folder / 'collected/gpu-samples.csv'
        if len(stamps) >= 2 and samples.is_file():
            offset = (stage['runner_gpu_metrics'] or {}).get('host_utc_offset_seconds')
            zone = datetime.timezone(datetime.timedelta(seconds=offset)) if offset is not None else None
            start, end = [datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=zone).timestamp()
                          for s in (stamps[0], stamps[-1])]
            with samples.open() as stream:
                values = [float(row['utilization_pct']) for row in csv.DictReader(stream)
                          if start <= float(row['time']) <= end]
            stage['native_window'] = {'start_epoch': start, 'end_epoch': end,
                'samples': len(values), 'mean_gpu_util_pct': statistics.mean(values) if values else None,
                'includes_idle_samples': True,
                'timezone_source': 'runner' if zone is not None else 'benchmark_host'}
        stages.append(stage)
    return stages


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
    parser.add_argument('--backend', choices=['local', 'pool'], default='local')
    parser.add_argument('--history-jobs', type=int, default=0,
                        help='Completed metadata-only fixture jobs for a populated scheduler store')
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
    config['mcp']['backend'] = args.backend
    config['_mcp_dispatcher'] = {'host': socket.gethostname(), 'pid': os.getpid()}
    # Keep the comparison on one explicitly selected physical GPU. The child's
    # backend sees the same restriction through this immutable config snapshot.
    config['mcp']['gpu_devices'] = [args.gpu]
    requests_path = Path(args.requests).resolve()
    requests = json.loads(requests_path.read_text())
    from msms_structure_elucidation.mcp.features import FEATURE_TASKS
    if any(request['task'] in FEATURE_TASKS for request in requests):
        config['_mcp_feature_driver'] = True
    store = JobStore(root / 'jobs')
    backend = backends.make_backend(args.backend, store, config, sys.executable)
    # Older revisions have no gpu_devices setting. Limit their GPU picker only
    # in the harness; resource scheduling and model code are otherwise unchanged.
    original = backends.free_gpus
    backends.free_gpus = lambda fraction, reserved: [i for i in original(fraction, reserved) if i == args.gpu]
    if not backends.free_gpus(.8, set()):
        raise RuntimeError('Selected GPU is busy or has insufficient free memory')
    for number in range(args.history_jobs):
        old, _ = store.create('infer_formulas', {'benchmark_history_fixture': number}, kind='cpu')
        store.set_status(old, 'done')
    if hasattr(store, 'active'):
        store.active()
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
            job, _ = store.create(task, request['params'], request.get('inputs', {}), tasks.KINDS[task], config)
            jobs.append(job)
        for job,request in zip(jobs,requests):
            if request['task'] in FEATURE_TASKS:
                store.set_launch(job,backend='feature-pipeline')
            else:
                backend.submit(job)
        backend.pump()
        last = {}
        next_audit = 0.
        with (root / 'timeline.jsonl').open('w') as timeline, (root / 'gpu-processes.jsonl').open('w') as audit:
            while True:
                backend.pump()
                states = {job: backend.poll(job) for job in jobs}
                observed = set(jobs)
                for parent in jobs:
                    observed.update(store.status(parent).get('prediction_jobs', []))
                    observed.update(store.status(parent).get('output_jobs', []))
                for job in list(observed):
                    observed.update(store.children(job))
                for job in sorted(observed):
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
                    sessions = {store.launch(job).get('pid') for job in observed}
                    if args.backend == 'pool':
                        for job in observed:
                            launch = store.launch(job)
                            if launch.get('remote_job_id'):
                                remote = Path(config['mcp']['servers'][launch['server']]['job_root'])
                                receipt = remote / launch['remote_job_id'] / 'launch.json'
                                if receipt.is_file():
                                    sessions.add(json.loads(receipt.read_text()).get('pid'))
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
                            'scored_structures':result.get('scored_structures'),
                            'spectra': sum(len(row.get('predicted_spectra', {})) for row in payload),
                            'scientific_sha256': hashlib.sha256(encoded).hexdigest()})
            # ponytail: self-check
            if request['task'] in ('score_candidates', 'predict_spectra') and status['state'] == 'done':
                if len(payload) != len(request['params']['smiles']):
                    raise RuntimeError(f'Incomplete structure accounting in {job}')
        active = [row for row in samples if row[0] <= elapsed]
        summary = {'elapsed_s': elapsed, 'gpu': args.gpu, 'sample_interval_s': .5,
            'inference_stages': inference_stages(store),
            'request_phase_seconds': {job: store.status(job).get('phase_seconds', {}) for job in jobs},
            'cpu_output_stage':config.get('mcp',{}).get('feature_cpu_output',True),
            'hardware': hardware, 'backend': args.backend, 'history_fixture_jobs': args.history_jobs,
            'foreign_gpu_pids': sorted(foreign),
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
        if hasattr(backend, 'close'):
            backend.close()
        for process in getattr(backend, 'processes', {}).values():
            process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
