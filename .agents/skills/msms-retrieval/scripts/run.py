"""Retrieve public ICEBERG atlas candidates for an ms-pred .ms spectrum."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spectrum', required=True)
    parser.add_argument('--collision-unit', required=True, choices=['NCE', 'eV'])
    parser.add_argument('--formula')
    parser.add_argument('--atlas-mgf', '--db_path', dest='atlas_mgf')
    parser.add_argument('--top-k', '--top_k', type=int, default=10)
    parser.add_argument('--ms-pred-python')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    cmd = [sys.executable, '-m', 'msms_structure_elucidation.cli', 'run',
           '--input', args.spectrum, '--collision-unit', args.collision_unit,
           '--output-dir', str(output.parent), '--top-k', str(args.top_k)]
    if args.formula:
        cmd += ['--formula', args.formula]
    if args.atlas_mgf:
        cmd += ['--atlas-mgf', args.atlas_mgf]
    if args.ms_pred_python:
        cmd += ['--ms-pred-python', args.ms_pred_python]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[4] / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    run = subprocess.run(cmd, env=env)
    source = output.parent / 'retrieval.json'
    if source.exists() and source != output:
        output.write_text(source.read_text())
    return run.returncode

if __name__ == '__main__':
    sys.exit(main())
