"""Sample GPU activity during an allocated prediction stage, including idle samples."""
from __future__ import annotations

import csv
import datetime
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import write_json

FIELDS = ['time', 'index', 'uuid', 'name', 'utilization_pct', 'memory_used_mib', 'memory_total_mib', 'foreign_pids']


def sample(selector: str) -> list[dict]:
    """Use scheduler GPU IDs for NVML, which can differ from CUDA's logical indices."""
    run = subprocess.run(['nvidia-smi', '-i', selector,
        '--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total',
        '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
    if run.returncode:
        raise RuntimeError(run.stderr[-1000:])
    processes = subprocess.run(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
    foreign = {}
    if not processes.returncode:
        for row in csv.reader(processes.stdout.splitlines()):
            if len(row) != 2:
                continue
            uuid, pid = (value.strip() for value in row)
            try:
                other = hasattr(os, 'getpgid') and os.getpgid(int(pid)) != os.getpgrp()
            except (OSError, ValueError):  # a sampled process may already have exited
                continue
            if other:
                foreign.setdefault(uuid, []).append(pid)
    rows = []
    for row in csv.reader(run.stdout.splitlines()):
        index, uuid, name, utilization, used, total = (value.strip() for value in row)
        counters = [float(value) for value in (utilization, used, total)]
        if not all(math.isfinite(value) for value in counters):
            raise RuntimeError('GPU activity counters are unavailable')
        rows.append(dict(zip(FIELDS, [time.time(), index, uuid, name, *counters,
                                     ','.join(foreign.get(uuid, []))])))
    return rows


def summarize(path: Path, start: float, end: float) -> dict:
    """Report time bounds and every sample; no filtering to active-only measurements."""
    devices = {}
    if path.is_file():
        with path.open() as stream:
            for row in csv.DictReader(stream):
                device = devices.setdefault(row['uuid'], {'index': row['index'], 'name': row['name'],
                    'samples': 0, 'utilization_sum': 0., 'peak_memory_mib': 0., 'foreign_compute_observed': False})
                device['samples'] += 1
                device['utilization_sum'] += float(row['utilization_pct'])
                device['peak_memory_mib'] = max(device['peak_memory_mib'], float(row['memory_used_mib']))
                device['foreign_compute_observed'] |= bool(row['foreign_pids'])
    for device in devices.values():
        device['mean_utilization_pct'] = device.pop('utilization_sum') / device['samples']
    return {'available': bool(devices), 'start': start, 'end': end, 'elapsed_seconds': max(0., end - start),
            'window': 'runner task, including model startup, native preparation, writers and publication; excludes queue and runner import',
            'devices': devices}


class GpuMonitor:
    """Finalize telemetry before a runner publishes its terminal status and collection manifest."""

    def __init__(self, folder: Path, devices: str | None, interval: float = 1.):
        self.folder, self.interval = folder, float(interval)
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('GPU monitor interval must be finite and nonnegative')
        visible = os.environ.get('NVIDIA_VISIBLE_DEVICES')
        if visible and visible.lower() in ('all', 'none', 'void'):
            visible = None
        self.selector = os.environ.get('SLURM_JOB_GPUS') or visible or devices
        self.stop = threading.Event()
        self.thread = None
        self.errors = []

    def __enter__(self):
        self.start = time.time()
        if self.selector and self.interval > 0 and shutil.which('nvidia-smi'):
            self.thread = threading.Thread(target=self._record, daemon=True)
            self.thread.start()
        return self

    def _record(self) -> None:
        with (self.folder / 'gpu-samples.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            while not self.stop.is_set():
                try:
                    writer.writerows(sample(self.selector))
                    stream.flush()
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                    self.errors = [str(exc)[-1000:]]
                self.stop.wait(self.interval)

    def __exit__(self, *exc) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=12)
        summary = summarize(self.folder / 'gpu-samples.csv', self.start, time.time())
        summary.update(query_selector=self.selector, requested_interval_seconds=self.interval, errors=self.errors)
        summary['host_utc_offset_seconds'] = datetime.datetime.now().astimezone().utcoffset().total_seconds()
        write_json(self.folder / 'gpu-metrics.json', summary)
