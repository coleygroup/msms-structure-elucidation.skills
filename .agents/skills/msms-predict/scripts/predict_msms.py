#!/usr/bin/env python3
"""Predict MS/MS spectra and fragments from a SMILES with GLACIER or ICEBERG."""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Run the shared prediction backend with the selected model, environment and weights."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smiles', required=True)
    parser.add_argument('--model', choices=['glacier', 'iceberg'], required=True)
    parser.add_argument('--collision-energies', type=float, nargs='+', required=True)
    parser.add_argument('--collision-unit', choices=['NCE', 'eV'], required=True,
                        help='user-confirmed unit of the supplied collision energies')
    parser.add_argument('--stepped', action='store_true', help='combine the component predictions into one stepped spectrum')
    parser.add_argument('--adduct', default='[M+H]+')
    parser.add_argument('--instrument', help='model instrument; otherwise models.simulator.instrument')
    parser.add_argument('--config')
    parser.add_argument('--ms-pred-python')
    parser.add_argument('--ms-pred-dir')
    parser.add_argument('--checkpoint', help='GLACIER checkpoint')
    parser.add_argument('--gen-checkpoint', help='ICEBERG generator checkpoint')
    parser.add_argument('--inten-checkpoint', help='ICEBERG intensity checkpoint')
    parser.add_argument('--cuda-devices')
    parser.add_argument('--model-batch-size', type=int)
    parser.add_argument('--model-cpu-workers', type=int)
    parser.add_argument('--model-gpu-workers', type=int)
    parser.add_argument('--no-fragments', action='store_true', help='skip atom-level fragment annotations')
    parser.add_argument('--output', required=True, type=Path, help='prediction JSON file')
    args = parser.parse_args(argv)
    if not args.smiles.strip():
        parser.error('--smiles must not be empty')
    if any(not math.isfinite(value) or value <= 0 for value in args.collision_energies):
        parser.error('collision energies must be positive and finite')

    from msms_structure_elucidation import cli
    from msms_structure_elucidation.config import asset, settings

    config = settings(args.config)
    simulator = config.get('models', {}).get('simulator', {})
    options = cli._model_options(args, config)
    for key, option in (('batch_size', 'model_batch_size'), ('num_cpu_workers', 'model_cpu_workers'),
                        ('num_gpu_workers', 'model_gpu_workers')):
        if getattr(args, option) is not None:
            options[key] = getattr(args, option)
    if not options['ms_pred_dir']:
        raise FileNotFoundError('Set --ms-pred-dir or models.simulator.ms_pred_src to the ms-pred checkout')
    required = ('checkpoint',) if args.model == 'glacier' else ('gen_checkpoint', 'inten_checkpoint')
    for key in required:
        if not options.get(key) or not Path(options[key]).is_file():
            raise FileNotFoundError(f'{args.model.upper()} needs a valid {key}; set the corresponding CLI option or configs/local.yaml path')
    instrument = cli._instrument(args.instrument or simulator.get('instrument'))
    if not instrument:
        raise ValueError('Set --instrument or models.simulator.instrument to the intended instrument')
    if options['batch_size'] < 1 or options['num_cpu_workers'] < 0 or options['num_gpu_workers'] < 1:
        raise ValueError('Batch size and GPU workers must be positive; CPU workers must be non-negative')
    if args.model == 'glacier':
        options['num_gpu_workers'] = 1
    python = cli.model_python(args.ms_pred_python or asset(simulator.get('python'), config))
    with tempfile.TemporaryDirectory(prefix='msms-predict-') as temporary:
        listing = Path(temporary) / 'smiles.json'
        listing.write_text(json.dumps([args.smiles]))
        predictions = cli.worker(python, 'predict', smiles_json=listing,
            collision_energies=','.join(str(value) for value in args.collision_energies),
            collision_unit=args.collision_unit, adduct=args.adduct, instrument=instrument,
            fragments=not args.no_fragments, force_recompute=True,
            **({'stepped': True} if args.stepped else {}), **options)
    if len(predictions) != 1:
        raise RuntimeError(f'{args.model.upper()} returned {len(predictions)} predictions for one input structure')
    result = {'model': args.model.upper(), 'collision_unit': args.collision_unit,
              'model_settings': options, 'predictions': predictions}
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_suffix(output.suffix + '.tmp')
    staged.write_text(json.dumps(result, indent=2, allow_nan=False))
    staged.replace(output)
    print(f'Predictions saved: {output}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f'Error: {exc}')
