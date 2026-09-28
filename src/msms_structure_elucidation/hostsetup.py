"""Probe this or a remote host, tune ms-pred inference, and save configs/local.yaml."""
from __future__ import annotations

import datetime
import json
import math
import os
import shlex
import signal
import subprocess
from pathlib import Path

from msms_structure_elucidation import hostprobe
from msms_structure_elucidation.config import LOCAL_CONFIG, overlay, read_yaml

MEMORY_LIMIT = 0.8  # peak GPU memory fraction a tuned batch size may reach
MAX_TRIALS = 6
MAX_BATCH = 512
TRIAL_TIMEOUT = 3600
WARMUP_MOLECULES = 24
ENERGIES = 5  # benchmark.ENERGIES


def workload(start: int, gpu_workers: int) -> int:
    """Unique molecules per trial: fixed for a tuning run so trials compare fairly."""
    return min(800, max(96, math.ceil(start * 4 * max(1, gpu_workers) * 3 / ENERGIES)))


def fits(result: dict) -> bool:
    fraction = result.get('peak_memory_fraction')
    return result['ok'] and (fraction is None or fraction <= MEMORY_LIMIT)


def tune_batch_size(run_trial, start: int) -> tuple[int | None, list[dict]]:
    """Halve from start until a trial fits; otherwise double while each step is at least 5% faster."""
    trials = [run_trial(start)]
    size = start
    while not fits(trials[-1]):
        if size == 1 or len(trials) >= MAX_TRIALS:
            return None, trials
        size //= 2
        trials.append(run_trial(size))
    best = trials[-1]
    if size == start:
        while len(trials) < MAX_TRIALS and size * 2 <= MAX_BATCH:
            size *= 2
            trials.append(run_trial(size))
            candidate = trials[-1]
            if not fits(candidate):
                break
            # Keep the smaller batch unless the larger one is clearly faster; it leaves memory headroom.
            if candidate['spectra_per_second'] < 1.05 * best['spectra_per_second']:
                break
            best = candidate
    return best['batch_size'], trials


def benchmark_trial(python: str, ms_pred_dir: str, gen: str, inten: str, settings: dict, num_smiles: int):
    """Return a callable that runs one benchmark subprocess for a batch size."""
    script = Path(__file__).with_name('benchmark.py')

    def run(batch_size: int, molecules: int = num_smiles) -> dict:
        cmd = [python, str(script), '--gen-checkpoint', gen, '--inten-checkpoint', inten,
               '--cuda-devices', settings['cuda_devices'] or '', '--batch-size', str(batch_size),
               '--num-gpu-workers', str(settings['num_gpu_workers']),
               '--num-cpu-workers', str(settings['num_cpu_workers']), '--num-smiles', str(molecules)]
        print(f'benchmark: batch_size={batch_size} molecules={molecules}', flush=True)
        # A new session lets a timeout stop ms-pred's own worker processes too.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=ms_pred_dir, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=TRIAL_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        done = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        for line in reversed(done.stdout.splitlines()):
            if line.startswith('RESULT_JSON='):
                result = json.loads(line.removeprefix('RESULT_JSON='))
                result['ok'] = result['ok'] and done.returncode == 0
                break
        else:
            result = {'batch_size': batch_size, 'ok': False, 'error': done.stderr[-2000:]}
        print(f'  -> {json.dumps(result)}', flush=True)
        return result
    return run


def dump_yaml(data, indent: int = 0) -> str:
    """Write nested mappings of JSON scalars and lists; readable by PyYAML and config._simple_yaml."""
    lines = []
    for key, value in data.items():
        prefix = ' ' * indent + f'{key}:'
        if isinstance(value, dict) and value:
            lines.append(prefix)
            lines.append(dump_yaml(value, indent + 2).rstrip('\n'))
        else:
            lines.append(f'{prefix} {json.dumps(value)}')
    return '\n'.join(line for line in lines if line) + '\n'


def save_local(update: dict, path: Path | None = None) -> dict:
    """Merge update into configs/local.yaml, keeping keys the user set by hand."""
    path = path or LOCAL_CONFIG
    data = read_yaml(path) if path.is_file() else {}
    overlay(data, update)
    header = ('# Written by `msms-structure-elucidation setup` for this host; overrides configs/default.yaml.\n'
              '# Not committed. Rerun setup after hardware changes.\n')
    path.write_text(header + dump_yaml(data))
    return data


def remote_command(args, body: str) -> list[str]:
    """ssh argv running a shell command on the remote host, after the optional environment prefix."""
    options = [part for option in args.ssh_option or [] for part in ('-o', option)]
    prefix = f'{args.remote_prefix} ' if args.remote_prefix else ''
    # BatchMode fails fast instead of waiting for a password prompt that an agent cannot answer.
    return ['ssh', '-o', 'BatchMode=yes', *options, args.remote, prefix + body]


def setup_remote(args) -> dict:
    """Probe a remote host; when its checkout is given, run the whole setup there."""
    stamp = datetime.datetime.now().isoformat(timespec='seconds')
    execution = {'mode': 'remote', 'host': args.remote, 'repo': args.remote_repo,
                 'python': args.remote_python, 'prefix': args.remote_prefix,
                 'ssh_options': args.ssh_option or [], 'configured': stamp}
    if not args.remote_repo:
        python = shlex.quote(args.remote_python or 'python3')
        done = subprocess.run(remote_command(args, f'{python} -'), input=Path(hostprobe.__file__).read_text(),
                              capture_output=True, text=True, timeout=600)
        if done.returncode:
            raise RuntimeError(f'remote probe failed: {done.stderr[-2000:]}')
        report = json.loads(done.stdout[done.stdout.index('{'):])
        execution.update(status='probed_not_installed', probe=report['host'], recommended=report['recommended'])
        save_local({'execution': execution})
        return {'status': 'probed_not_installed', 'execution': execution, 'config': str(LOCAL_CONFIG)}
    forwarded = ['setup', '--json']
    for flag in ('ms_pred_python', 'ms_pred_dir', 'gen_checkpoint', 'inten_checkpoint'):
        if getattr(args, flag):
            forwarded += ['--' + flag.replace('_', '-'), getattr(args, flag)]
    if args.no_benchmark:
        forwarded.append('--no-benchmark')
    body = ' '.join(shlex.quote(part) for part in [args.remote_python or 'python3', '-m',
                                                    'msms_structure_elucidation.cli', *forwarded])
    cmd = remote_command(args, f'cd {shlex.quote(args.remote_repo)} && {body}')
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    print(done.stdout, end='')
    if done.returncode:
        raise RuntimeError(f'remote setup failed: {done.stderr[-2000:]}')
    report = json.loads([line for line in done.stdout.splitlines() if line.startswith('SETUP_JSON=')][-1]
                        .removeprefix('SETUP_JSON='))
    execution.update(status=report['status'], simulator=report['simulator'], probe=report['host'])
    save_local({'execution': execution})
    return {'status': report['status'], 'execution': execution, 'config': str(LOCAL_CONFIG)}


def setup(args, model_options: dict, find_python, batch_defaults: dict | None = None) -> dict:
    """Probe this host, benchmark ICEBERG when possible, and save configs/local.yaml."""
    if args.remote:
        return setup_remote(args)
    host = hostprobe.probe()
    recommended = hostprobe.recommend(host)
    notes = recommended.pop('notes')
    simulator = dict(recommended)
    status, trials = 'heuristic', []
    ready = model_options.get('ms_pred_dir') and all(model_options.get(k) and Path(model_options[k]).is_file()
                                                      for k in ('gen_checkpoint', 'inten_checkpoint'))
    if args.no_benchmark:
        notes.append('benchmark skipped (--no-benchmark)')
    elif not simulator['cuda_devices']:
        notes.append('no usable GPU; CPU settings are heuristic')
    elif not ready:
        notes.append('ICEBERG checkpoints or ms-pred checkout not configured; rerun setup after adding them to benchmark')
    else:
        run_trial = benchmark_trial(find_python(args.ms_pred_python), model_options['ms_pred_dir'],
                                    model_options['gen_checkpoint'], model_options['inten_checkpoint'], simulator,
                                    workload(simulator['batch_size'], simulator['num_gpu_workers']))
        # Untimed warm-up: the first run pays for cold imports and checkpoint reads, which would favour later trials.
        run_trial(simulator['batch_size'], WARMUP_MOLECULES)
        best, trials = tune_batch_size(run_trial, simulator['batch_size'])
        if best is None:
            raise RuntimeError('every benchmark trial failed; check the ms-pred environment and checkpoints:\n'
                               + json.dumps(trials[-1], indent=2))
        simulator['batch_size'] = best
        simulator['shard_size'] = min(2048, max(256, 8 * best))
        status = 'benchmarked'
    batch_defaults = batch_defaults or {}
    features = hostprobe.batch_workers(host['cpu_threads'], host['ram_total_gb'],
                                       batch_defaults.get('max_model_jobs', 1), batch_defaults.get('min_free_memory_gb', 4))
    update = {'models': {'simulator': simulator, 'batch': {'max_workers': features}}}
    for key, option in (('ms_pred_src', 'ms_pred_dir'), ('gen_ckpt', 'gen_checkpoint'),
                        ('inten_ckpt', 'inten_checkpoint')):
        if getattr(args, option, None):
            update['models']['simulator'][key] = str(Path(getattr(args, option)).expanduser().resolve())
    update['host_profile'] = {'hostname': host['hostname'], 'gpus': [g['name'] for g in host['gpus']],
        'cpu_threads': host['cpu_threads'], 'ram_total_gb': host['ram_total_gb'], 'status': status,
        'configured': datetime.datetime.now().isoformat(timespec='seconds'), 'notes': notes,
        'benchmark': {str(t['batch_size']): t.get('spectra_per_second') if fits(t) else 'failed' for t in trials}}
    save_local(update)
    return {'status': status, 'host': host, 'simulator': simulator, 'batch_max_workers': features,
            'notes': notes, 'trials': trials,
            'config': str(LOCAL_CONFIG)}
