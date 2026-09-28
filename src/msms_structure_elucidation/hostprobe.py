"""Probe host hardware and recommend ms-pred inference settings.

Standard library only, so it also runs on a remote host without this package:
``ssh HOST python3 - < hostprobe.py`` prints the probe and recommendation as JSON.
"""
from __future__ import annotations

import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys

# Measured anchors (GPU memory GiB -> ICEBERG batch_size, 2 GPU workers, 16 CPU workers):
# RTX 4070 Laptop 8 GB -> 16; RTX A5000 24 GB -> 128.
ANCHORS = ((8.0, 16), (24.0, 128))
MIN_BATCH, MAX_BATCH = 4, 256
# Batch admission budget: RAM held by one batch worker process (ms-pred loaded plus one feature;
# measured about 1.4 GB on eight clinical spectra), and by one ICEBERG model job.
FEATURE_RAM_GB = 1.5
MODEL_JOB_RAM_GB = 4
MAX_FEATURES = 8


def cpu_threads() -> int:
    if hasattr(os, 'sched_getaffinity'):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def ram_gb() -> tuple[float | None, float | None]:
    """Return (total, available) system memory in GiB."""
    if os.path.exists('/proc/meminfo'):
        info = {}
        with open('/proc/meminfo') as handle:
            for line in handle:
                key, _, value = line.partition(':')
                info[key] = int(value.split()[0]) / 1024 ** 2
        return round(info['MemTotal'], 1), round(info.get('MemAvailable', info['MemFree']), 1)
    if sys.platform == 'darwin':
        total = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True)
        if total.returncode == 0:
            return round(int(total.stdout) / 1024 ** 3, 1), None
    return None, None


def gpus() -> list[dict]:
    """List NVIDIA GPUs from nvidia-smi; memory in GiB."""
    smi = shutil.which('nvidia-smi')
    if not smi:
        return []
    run = subprocess.run([smi, '--query-gpu=index,name,memory.total,memory.free,memory.used',
                          '--format=csv,noheader,nounits'], capture_output=True, text=True)
    if run.returncode:
        return []
    found = []
    for line in run.stdout.strip().splitlines():
        index, name, total, free, used = [part.strip() for part in line.split(',')]
        found.append({'index': int(index), 'name': name, 'memory_total_gb': round(int(total) / 1024, 1),
                      'memory_free_gb': round(int(free) / 1024, 1), 'memory_used_gb': round(int(used) / 1024, 1)})
    return found


def probe() -> dict:
    total, available = ram_gb()
    return {'hostname': socket.gethostname(), 'platform': platform.platform(),
            'cpu_threads': cpu_threads(), 'ram_total_gb': total, 'ram_available_gb': available,
            'gpus': gpus()}


def anchor_batch_size(memory_gb: float) -> int:
    """Interpolate the measured anchors on a log-log scale, rounded down to a power of two."""
    (m0, b0), (m1, b1) = ANCHORS
    exponent = math.log(b1 / b0) / math.log(m1 / m0)
    estimate = b0 * (max(memory_gb, 1.0) / m0) ** exponent
    return int(min(MAX_BATCH, max(MIN_BATCH, 2 ** math.floor(math.log2(estimate) + 1e-9))))


def batch_workers(threads: int, ram_total_gb: float | None, max_model_jobs: int = 1,
                  min_free_gb: float = 4) -> int:
    """Worker processes for `batch`: one feature each, one CPU core each, bounded by RAM."""
    ram = ram_total_gb or 8
    by_ram = int((ram - min_free_gb - MODEL_JOB_RAM_GB * max_model_jobs) // FEATURE_RAM_GB)
    return max(1, min(MAX_FEATURES, threads, by_ram))


def recommend(host: dict) -> dict:
    """Heuristic models.simulator settings for this host; refine with the benchmark."""
    threads = host['cpu_threads']
    notes = []
    candidates = [g for g in host['gpus'] if g['memory_free_gb'] >= 4]
    if not candidates:
        ram = host.get('ram_total_gb') or 8
        workers = max(1, min(threads, 16, int(ram // 4)))
        if host['gpus']:
            notes.append('GPUs found but none has 4 GiB free; using CPU')
        return {'cuda_devices': None, 'batch_size': 4, 'num_cpu_workers': workers,
                'num_gpu_workers': 1, 'shard_size': 256, 'notes': notes}
    best = max(g['memory_free_gb'] for g in candidates)
    # Use every GPU with nearly as much free memory as the best one; busy GPUs are skipped.
    chosen = sorted((g for g in candidates if g['memory_free_gb'] >= 0.9 * best), key=lambda g: g['index'])
    skipped = [g['index'] for g in host['gpus'] if g not in chosen]
    if skipped:
        notes.append(f'skipped busy or small GPUs {skipped}')
    smallest = min(chosen, key=lambda g: g['memory_free_gb'])
    # Anchors were measured by total memory; fall back to free memory when a GPU is partly occupied.
    basis = smallest['memory_total_gb']
    if smallest['memory_free_gb'] < 0.8 * basis:
        basis = smallest['memory_free_gb']
        notes.append(f'GPU {smallest["index"]} is partly occupied; batch size is based on free memory')
    batch_size = anchor_batch_size(basis)
    return {'cuda_devices': ','.join(str(g['index']) for g in chosen), 'batch_size': batch_size,
            'num_cpu_workers': min(16, threads), 'num_gpu_workers': 2 * len(chosen),
            'shard_size': min(2048, max(256, 8 * batch_size)), 'notes': notes}


if __name__ == '__main__':
    host = probe()
    print(json.dumps({'host': host, 'recommended': recommend(host)}, indent=2))
