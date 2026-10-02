"""Probe this or a remote host, tune ms-pred inference, and save configs/local.yaml."""
from __future__ import annotations

import datetime
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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


def remote_path(path: str) -> str:
    """Quote a remote path for the shell while keeping a leading ~/ expandable."""
    return '~/' + shlex.quote(path[2:]) if path.startswith('~/') else shlex.quote(path)


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
    forwarded = ['setup', '--json', *forwarded_flags(args)]
    body = ' '.join(shlex.quote(part) for part in [args.remote_python or 'python3', '-m',
                                                    'msms_structure_elucidation.cli', *forwarded])
    cmd = remote_command(args, f'cd {remote_path(args.remote_repo)} && {body}')
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    print(done.stdout, end='')
    if done.returncode:
        raise RuntimeError(f'remote setup failed: {done.stderr[-2000:]}')
    report = json.loads([line for line in done.stdout.splitlines() if line.startswith('SETUP_JSON=')][-1]
                        .removeprefix('SETUP_JSON='))
    execution.update(status=report['status'], simulator=report.get('simulator'), probe=report.get('host'),
                     backend=report.get('backend', 'local'))
    if report['status'] == 'slurm_discovered':
        execution['partitions'] = report['partitions']
    save_local({'execution': execution})
    return {**report, 'execution': execution, 'config': str(LOCAL_CONFIG)}


VALUE_FLAGS = ('ms_pred_python', 'ms_pred_dir', 'gen_checkpoint', 'inten_checkpoint', 'frigid_python', 'frigid_dir',
               'mist_checkpoint', 'dlm_checkpoint', 'scheduler', 'slurm_partition', 'slurm_account', 'slurm_qos',
               'slurm_gpus', 'slurm_cpus', 'slurm_mem', 'slurm_time', 'slurm_cpu_cpus', 'slurm_cpu_mem',
               'mcp_job_root')
LIST_FLAGS = ('slurm_setup', 'slurm_arg')
SWITCH_FLAGS = ('no_benchmark', 'slurm_requeue')


def forwarded_flags(args) -> list[str]:
    """Setup options repeated on the remote host; paths there are remote paths."""
    flags = []
    for name in VALUE_FLAGS:
        if getattr(args, name, None):
            flags += ['--' + name.replace('_', '-'), str(getattr(args, name))]
    for name in LIST_FLAGS:
        for value in getattr(args, name, None) or []:
            flags.append(f'--{name.replace("_", "-")}={value}')
    flags += ['--' + name.replace('_', '-') for name in SWITCH_FLAGS if getattr(args, name, False)]
    return flags


def model_paths(args) -> dict:
    """Interpreter, checkout and checkpoint paths given to setup, as configs/local.yaml keys."""
    simulator, denovo = {}, {}
    for section, key, option in ((simulator, 'python', 'ms_pred_python'), (simulator, 'ms_pred_src', 'ms_pred_dir'),
                                 (simulator, 'gen_ckpt', 'gen_checkpoint'), (simulator, 'inten_ckpt', 'inten_checkpoint'),
                                 (denovo, 'frigid_python', 'frigid_python'), (denovo, 'frigid_src', 'frigid_dir'),
                                 (denovo, 'mist_ckpt', 'mist_checkpoint'), (denovo, 'dlm_ckpt', 'dlm_checkpoint')):
        if getattr(args, option, None):
            path = Path(getattr(args, option)).expanduser()
            # A bare interpreter name such as python3 stays a name; anything else is saved absolute.
            section[key] = str(path.absolute()) if path.exists() or '/' in str(path) else str(path)
    return {'simulator': simulator, 'denovo': denovo}


REQUIRED_MODULES = {('simulator', 'python'): ('ms_pred', 'msbuddy', 'rdkit'),
                    ('denovo', 'frigid_python'): ('dlm', 'ms_pred', 'torch')}


def check_interpreters(paths: dict, run=subprocess.run) -> list[str]:
    """Problems with the configured model interpreters: each must import what its models need."""
    problems = []
    for (section, key), modules in REQUIRED_MODULES.items():
        python = paths.get(section, {}).get(key)
        if not python:
            continue
        code = 'import importlib.util as u; print(",".join(m for m in %r if u.find_spec(m) is None))' % (modules,)
        done = run([python, '-c', code], capture_output=True, text=True, timeout=300)
        missing = done.stdout.strip().splitlines()[-1] if done.returncode == 0 and done.stdout.strip() else ''
        if done.returncode:
            problems.append(f'models.{section}.{key} ({python}) does not run: {done.stderr.strip()[-300:]}')
        elif missing:
            problems.append(f'models.{section}.{key} ({python}) cannot import {missing}; install them in that environment')
    return problems


def slurm_partitions(run=subprocess.run) -> list[dict]:
    """Partitions with their GPU gres, time limit, CPUs and memory per node, from sinfo."""
    done = run(['sinfo', '-h', '-o', '%P|%G|%l|%c|%m|%a|%D'], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f'sinfo failed; run setup on the cluster login node: {done.stderr.strip()[-500:]}')
    partitions: dict[str, dict] = {}
    for line in done.stdout.splitlines():
        fields = line.strip().split('|')
        if len(fields) != 7:
            continue
        name, gres, limit, cpus, mem, avail, nodes = fields
        row = partitions.setdefault(name.rstrip('*'), {'partition': name.rstrip('*'), 'default': name.endswith('*'),
            'gpus': [], 'time_limit': limit, 'cpus_per_node': 0, 'mem_mb_per_node': 0, 'nodes': 0, 'available': avail})
        for item in gres.split(','):
            if item.startswith('gpu') and item not in row['gpus']:
                row['gpus'].append(item)
        row['cpus_per_node'] = max(row['cpus_per_node'], int(cpus.rstrip('+')) if cpus.rstrip('+').isdigit() else 0)
        row['mem_mb_per_node'] = max(row['mem_mb_per_node'], int(mem.rstrip('+')) if mem.rstrip('+').isdigit() else 0)
        row['nodes'] += int(nodes) if nodes.isdigit() else 0
    return list(partitions.values())


def mcp_paths(args) -> dict:
    root = getattr(args, 'mcp_job_root', None)
    return {'job_root': str(Path(root).expanduser().absolute())} if root else {}


def slurm_settings(args) -> dict:
    return {'partition': args.slurm_partition, 'account': args.slurm_account, 'qos': args.slurm_qos,
            'gpus': args.slurm_gpus, 'cpus_per_task': args.slurm_cpus, 'mem': args.slurm_mem, 'time': args.slurm_time,
            'requeue': bool(args.slurm_requeue), 'extra_args': list(args.slurm_arg or []),
            'setup': list(args.slurm_setup or []), 'cpu': {'cpus_per_task': args.slurm_cpu_cpus, 'mem': args.slurm_cpu_mem}}


SETUP_JOB_TIMEOUT = 5 * 3600  # below the 6 h ssh timeout of remote setup


def setup_slurm(args) -> dict:
    """Discover partitions; with a partition chosen, save mcp.slurm and probe and tune inside a Slurm job."""
    if not args.slurm_partition:
        return {'status': 'slurm_discovered', 'backend': 'slurm', 'partitions': slurm_partitions(),
                'next': 'choose partition, GPU request, account and environment setup lines with the user, '
                        'then rerun setup with --slurm-partition and the other --slurm-* options'}
    from msms_structure_elucidation.mcp.backends import REPO, parse_job_id, sbatch_script, scheduler_state
    slurm = slurm_settings(args)
    save_local({'mcp': {'backend': 'slurm', 'slurm': slurm, **mcp_paths(args)}, 'models': model_paths(args)})
    logs = REPO / 'results' / 'setup'
    logs.mkdir(parents=True, exist_ok=True)
    # The compute node probes and benchmarks itself; only the ms-pred options matter there.
    node = SimpleNamespace(**{name: getattr(args, name, None) for name in
                              ('ms_pred_python', 'ms_pred_dir', 'gen_checkpoint', 'inten_checkpoint', 'no_benchmark')})
    inner = [sys.executable, '-m', 'msms_structure_elucidation.cli', 'setup', '--json', *forwarded_flags(node)]
    options, script = sbatch_script(slurm, 'gpu', inner)
    (logs / 'setup_job.sh').write_text(script)
    done = subprocess.run(['sbatch', '--parsable', '--job-name=msms-setup', f'--output={logs}/setup-%j.log',
                           f'--chdir={REPO}', *options, str(logs / 'setup_job.sh')], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f'sbatch failed: {done.stderr.strip()[-2000:]}')
    job = parse_job_id(done.stdout)
    print(f'setup job {job} submitted; waiting for it to finish (log: {logs}/setup-{job}.log)', file=sys.stderr, flush=True)
    deadline = time.monotonic() + SETUP_JOB_TIMEOUT
    state = None
    while time.monotonic() < deadline:
        state = scheduler_state(job)
        if state and state not in ('PENDING', 'CONFIGURING', 'RUNNING', 'COMPLETING', 'REQUEUED', 'RESIZING'):
            break
        time.sleep(15)
    log = (logs / f'setup-{job}.log')
    lines = [line for line in (log.read_text().splitlines() if log.is_file() else []) if line.startswith('SETUP_JSON=')]
    if not lines:
        raise RuntimeError(f'setup job {job} ended as {state} without a result; see {log}')
    report = json.loads(lines[-1].removeprefix('SETUP_JSON='))
    return {**report, 'backend': 'slurm', 'slurm_job_id': job, 'slurm': slurm}


def client_entry(execution: dict | None, python: str | None = None) -> dict:
    """An .mcp.json server entry: stdio here, or stdio through ssh to the configured model host."""
    if not execution or execution.get('mode') != 'remote':
        return {'command': python or sys.executable, 'args': ['-m', 'msms_structure_elucidation.mcp']}
    if not execution.get('repo'):
        raise ValueError('The remote checkout is not set up yet; rerun setup with --remote-repo')
    remote = SimpleNamespace(remote=execution['host'], remote_prefix=execution.get('prefix'),
                             ssh_option=execution.get('ssh_options') or [])
    body = ' '.join(shlex.quote(part) for part in [execution.get('python') or 'python3', '-m', 'msms_structure_elucidation.mcp'])
    command = remote_command(remote, f'cd {remote_path(execution["repo"])} && {body}')
    return {'command': command[0], 'args': command[1:]}


def write_client_config(path: Path, name: str, entry: dict) -> dict:
    """Merge one server into an .mcp.json file, keeping the other servers."""
    data = json.loads(path.read_text()) if path.is_file() else {}
    data.setdefault('mcpServers', {})[name] = entry
    path.write_text(json.dumps(data, indent=2) + '\n')
    return data


def setup(args, model_options: dict, find_python, batch_defaults: dict | None = None) -> dict:
    """Probe this host, benchmark ICEBERG when possible, and save configs/local.yaml."""
    if args.remote:
        return setup_remote(args)
    if getattr(args, 'scheduler', None) == 'slurm':
        return setup_slurm(args)
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
    if mcp_paths(args):
        update['mcp'] = mcp_paths(args)
    paths = model_paths(args)
    notes += check_interpreters(paths)
    update['models']['simulator'].update(paths['simulator'])
    if paths['denovo']:
        update['models']['denovo'] = paths['denovo']
    update['host_profile'] = {'hostname': host['hostname'], 'gpus': [g['name'] for g in host['gpus']],
        'cpu_threads': host['cpu_threads'], 'ram_total_gb': host['ram_total_gb'], 'status': status,
        'configured': datetime.datetime.now().isoformat(timespec='seconds'), 'notes': notes,
        'benchmark': {str(t['batch_size']): t.get('spectra_per_second') if fits(t) else 'failed' for t in trials}}
    save_local(update)
    return {'status': status, 'host': host, 'simulator': simulator, 'batch_max_workers': features,
            'notes': notes, 'trials': trials,
            'config': str(LOCAL_CONFIG)}
