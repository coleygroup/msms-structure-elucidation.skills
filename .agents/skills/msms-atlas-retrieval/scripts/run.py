"""Rank ICEBERG atlas candidates or predict every PubChem mass match for a .ms spectrum."""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Forward portable retrieval and direct mass-search options to the shared CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spectrum', required=True)
    parser.add_argument('--collision-unit', required=True, choices=['NCE', 'eV'])
    parser.add_argument('--formula')
    parser.add_argument('--formulas-file')
    parser.add_argument('--mass-only', action='store_true')
    parser.add_argument('--ms1-ppm', type=float)
    parser.add_argument('--formula-elements', default='CHNOPS')
    parser.add_argument('--model', choices=['iceberg', 'glacier'])
    parser.add_argument('--checkpoint')
    parser.add_argument('--gen-checkpoint')
    parser.add_argument('--inten-checkpoint')
    parser.add_argument('--atlas-mgf', '--db_path', dest='atlas_mgf')
    parser.add_argument('--top-k', '--top_k', type=int, default=10)
    parser.add_argument('--ms-pred-python')
    parser.add_argument('--ms-pred-dir')
    parser.add_argument('--config')
    parser.add_argument('--instrument')
    parser.add_argument('--cuda-devices')
    parser.add_argument('--model-batch-size', type=int)
    parser.add_argument('--model-cpu-workers', type=int)
    parser.add_argument('--model-gpu-workers', type=int)
    parser.add_argument('--model-shard-size', type=int)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    cmd = [sys.executable, '-m', 'msms_structure_elucidation.cli', 'run',
           '--input', args.spectrum, '--collision-unit', args.collision_unit,
           '--output-dir', str(output.parent), '--top-k', str(args.top_k)]
    if args.mass_only:
        cmd.append('--mass-only')
    for key in ('formula', 'formulas_file', 'ms1_ppm', 'formula_elements', 'model', 'checkpoint',
                'gen_checkpoint', 'inten_checkpoint', 'atlas_mgf', 'ms_pred_python', 'ms_pred_dir',
                'config', 'instrument', 'cuda_devices', 'model_batch_size', 'model_cpu_workers',
                'model_gpu_workers', 'model_shard_size'):
        value = getattr(args, key)
        if value is not None:
            cmd += ['--' + key.replace('_', '-'), str(value)]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[4] / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    source = output.parent / 'retrieval.json'
    previous = source.read_bytes() if source.is_file() else None
    run = subprocess.run(cmd, env=env)
    if source.is_file() and source != output and (run.returncode == 0 or source.read_bytes() != previous):
        output.write_bytes(source.read_bytes())
    return run.returncode


if __name__ == '__main__':
    sys.exit(main())
