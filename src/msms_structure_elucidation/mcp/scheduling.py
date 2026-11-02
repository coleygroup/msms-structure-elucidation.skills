"""One detached local scheduler per job root; scientific workers only publish state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import JobStore, read_json, write_json
from msms_structure_elucidation.prediction_cache import file_lock


def local_control(root: Path) -> Path:
    """Keep startup arbitration on this machine, outside shared filesystem locks."""
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    uid = str(os.getuid()) if hasattr(os, 'getuid') else hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    folder = Path(tempfile.gettempdir()) / ('msms-scheduler-' + uid) / identity
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def ensure(store: JobStore, config: dict, python: str | None, exclude: set[str]) -> None:
    """Start one detached driver; local locks coalesce concurrent client requests."""
    control = local_control(store.root)
    if (store.root / 'scheduler-stop.json').exists():
        raise RuntimeError('Local scheduler is explicitly stopped; remove scheduler-stop.json after an authorized resume')
    with file_lock(control / 'startup.lock'):
        with file_lock(control / 'owner.lock', blocking=False) as available:
            if not available:
                return
        receipt = read_json(store.root / 'scheduler-service.json', {})
        if receipt.get('host') != socket.gethostname() and time.time() - receipt.get('heartbeat', 0) < 30:
            raise RuntimeError('This job root has a scheduler on another host; use a separate endpoint job root')
        snapshot = control / 'config.json'
        write_json(snapshot, {'config': config, 'exclude_tasks': sorted(exclude)})
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')}
        with (control / 'service.log').open('ab') as log:
            child = subprocess.Popen([python or sys.executable, '-m', __name__, '--root', str(store.root),
                '--config', str(snapshot)], env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with file_lock(control / 'owner.lock', blocking=False) as available:
                if not available:
                    return
            if child.poll() is not None:
                raise RuntimeError(f'Local scheduler exited during startup; see {control / "service.log"}')
            time.sleep(.05)
        raise RuntimeError(f'Local scheduler startup timed out; see {control / "service.log"}')


def main(argv: list[str] | None = None) -> int:
    """Hold the driver lease and schedule until idle or explicitly stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    store = JobStore(Path(args.root))
    control = local_control(store.root)
    # A cross-host lease is acquired once, nonblocking, not by every waiting worker.
    with file_lock(control / 'owner.lock', blocking=False) as local:
        if not local:
            return 0
        with file_lock(store.root / 'scheduler-owner.lock', blocking=False) as shared:
            if not shared:
                return 0
            from msms_structure_elucidation.mcp.backends import LocalBackend
            payload = json.loads(Path(args.config).read_text())
            config = payload['config']
            config['_mcp_cpu_dispatcher'] = {'host': socket.gethostname(), 'pid': os.getpid()}
            if config.get('mcp', {}).get('backend', 'local') == 'local':
                config['_mcp_dispatcher'] = config['_mcp_cpu_dispatcher']
            backend = LocalBackend(store, config, sys.executable)
            backend.exclude_tasks = set(payload['exclude_tasks'])
            if config.get('mcp', {}).get('backend') == 'pool':
                backend.child_cancel = lambda child: store.set_status(child, store.status(child)['state'], cancel_requested=True)
            stopped = False
            def stop(*_):
                nonlocal stopped
                stopped = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            idle_since = time.monotonic()
            heartbeat = 0.
            started = time.time()
            while not stopped and store.root.exists():
                if (store.root / 'scheduler-stop.json').exists():
                    break
                backend.pump()
                active = backend.queue_snapshot
                if active:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 60:
                    break
                if time.monotonic() - heartbeat >= 2:
                    ready = [r for r in active if r.get('phase') == 'waiting_cpu']
                    write_json(store.root / 'scheduler-service.json', {
                        'state': 'running', **config['_mcp_cpu_dispatcher'], 'started': started,
                        'heartbeat': time.time(), 'active_jobs': len(active),
                        'max_live_pipelines': backend.max_live_pipelines,
                        'ready_cpu': len(ready), 'oldest_ready_seconds': max(
                            (time.time() - r.get('cpu_ready_at', r.get('phase_started', time.time())) for r in ready), default=0.),
                        'admission': backend.admission.summary()})
                    heartbeat = time.monotonic()
                time.sleep(.5)
            if store.root.exists():
                write_json(store.root / 'scheduler-service.json', {'state': 'stopped',
                    **config['_mcp_cpu_dispatcher'], 'started': started, 'heartbeat': time.time()})
    return 0


if __name__ == '__main__':
    sys.exit(main())
