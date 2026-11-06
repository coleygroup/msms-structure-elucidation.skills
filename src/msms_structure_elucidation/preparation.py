"""Atomic CPU preparation manifests consumed by the backend prediction queue."""
from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
import shutil
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import read_json, write_json
from msms_structure_elucidation.prediction_cache import (
    cache_root, digest, file_fingerprint, source_fingerprint)
from msms_structure_elucidation.spectrum import inspect_ms


def record_request(out: Path, spectrum: Path, formula: str, unit: str,
                   instrument: str, options: dict, smiles: list[str], python: str | None = None) -> None:
    """Save validated candidates and exact model conditions without acquiring a GPU."""
    if python and Path(python).absolute() != Path(sys.executable).absolute():
        request = out / ('prepare-' + formula + '.json')
        write_json(request, {'out': str(out), 'spectrum': str(spectrum), 'formula': formula,
            'unit': unit, 'instrument': instrument, 'options': options, 'smiles': smiles})
        env = {**os.environ, 'PYTHONPATH': str(Path(options['ms_pred_dir']) / 'src') + os.pathsep +
               str(Path(__file__).parents[1]) + os.pathsep + os.environ.get('PYTHONPATH', '')}
        subprocess.run([python, '-m', __name__, '--request', str(request)], env=env, check=True)
        return
    from ms_pred import common
    # Match the existing prediction worker's cache keys before preparing entries.
    # A second SMILES parse can normalize aromatic notation even after validation.
    smiles = list(dict.fromkeys(common.rm_stereo(smi) for smi in smiles))
    from msms_structure_elucidation import prediction_cache as cache
    data = inspect_ms(spectrum, unit)
    energies = (data['stepped_collision']['model_energies_ev'] if data.get('acquisition_mode') == 'stepped'
                else [row['model_energy_ev'] for row in data['energy_mapping']])
    required = ('checkpoint',) if options['model'] == 'glacier' else ('gen_checkpoint', 'inten_checkpoint')
    root = cache_root(options.get('prediction_cache'))
    conditions = {'model': options['model'], 'adduct': data['adduct'], 'instrument': instrument,
                  'collision_unit': 'eV', 'collision_energies': sorted(set(energies)), 'include_fragments': False}
    identity = {'weights': {key: file_fingerprint(Path(options[key]), root) for key in required},
                'native_source': source_fingerprint(Path(options['ms_pred_dir']) / 'src/ms_pred'),
                'adapter_source': source_fingerprint(Path(__file__).parent),
                'sparse_k': options.get('sparse_k', 100), 'cache': options.get('prediction_cache')}
    key = digest([conditions, identity])
    raw_identity = cache.model_identity(options['model'], [options[k] for k in required],
        Path(options['ms_pred_dir']) / 'src/ms_pred', root, data['adduct'],
        common.normalize_instrument(instrument), options.get('sparse_k', 100))
    raw_identity['adapter_sha256'] = cache.adapter_fingerprint(Path(__file__).with_name('worker.py'))
    arguments = dict(smiles=[], energies=conditions['collision_energies'], nce=False,
        adduct=data['adduct'], instrument=instrument, cuda_devices=None, force_recompute=True,
        **{k:options.get(k) for k in ('model','checkpoint','gen_checkpoint','inten_checkpoint','batch_size',
                                      'num_cpu_workers','num_gpu_workers')},
        **{k:options.get(k, default) for k,default in {'num_writers':2,'num_h5_chunks':4,
            'feat_cache_size':256,'sparse_k':100}.items()})
    if options['model'] == 'iceberg':
        devices = str(options.get('cuda_devices') or '0').split(',')
        arguments['num_gpu_workers'] = max(1,int(arguments['num_gpu_workers'])//len(devices))
    prepared_cache = root / 'prepared_entries'
    prepared_cache.mkdir(parents=True, exist_ok=True)
    from msms_structure_elucidation.mcp.raw_outputs import missing_groups
    missing = sorted({smi for needed,molecules in missing_groups(root,raw_identity,smiles,arguments['energies']).items()
                      if needed for smi in molecules})
    prepared = prepared_cache / (digest([key, missing, options]) + '.json')
    with cache.file_lock(prepared.with_suffix('.lock')):
        if not prepared.exists():
            native = None
            if not missing:
                payload = {'model':options['model'], 'entries':[], 'options':{}} if options['model']=='glacier' else {
                    'model':'iceberg','rows':[]}
            elif options['model'] == 'glacier':
                from msms_structure_elucidation import glacier, worker
                worker._check_glacier_features(options['checkpoint'])
                from msms_structure_elucidation.config import settings
                count = settings().get('mcp',{}).get('cpu_preparation_workers','auto')
                if count == 'auto':
                    count = min(options['num_cpu_workers'], int(os.environ.get('SLURM_CPUS_PER_TASK') or
                        (len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count() or 1)))
                count = int(count)
                if count < 0:
                    raise ValueError('CPU preparation workers must be nonnegative or auto')
                native = glacier.prepare(smiles=missing, energies=arguments['energies'], nce=False,
                    adduct=data['adduct'], instrument=instrument, checkpoint=options['checkpoint'], cuda_devices=None,
                    batch_size=options['batch_size'], num_cpu_workers=count, num_writers=arguments['num_writers'],
                    num_h5_chunks=arguments['num_h5_chunks'], feat_cache_size=arguments['feat_cache_size'],
                    sparse_k=arguments['sparse_k'], work_dir=prepared_cache)
                payload = read_json(native)
                payload['options']['num_cpu_workers'] = options['num_cpu_workers']
            else:
                from msms_structure_elucidation import iceberg
                native = iceberg.prepare({**arguments, 'smiles': missing}, prepared_cache)
                payload = read_json(native)
            write_json(prepared, payload)
            if native:
                shutil.rmtree(native.parent)
    folder = out / 'preparation'
    folder.mkdir(exist_ok=True)
    write_json(folder / (digest([formula, key]) + '.json'), {
        'schema': 1, 'formula': formula, 'condition_id': key, 'conditions': conditions,
        'identity': identity, 'smiles': sorted(set(smiles)), 'native_prepared': str(prepared),
        'model_smiles_normalized': True,
        'raw_cache_identity': raw_identity,
        'inference': {'arguments': arguments, 'python': sys.executable,
            'ms_pred_dir': options['ms_pred_dir'],
            'weights': {options[k]:identity['weights'][k] for k in required},
            'native_source':identity['native_source'], 'adapter_source':identity['adapter_source'],
            'publish_raw_records': True}})


def publish(out: Path, result: dict, args: argparse.Namespace) -> None:
    """Publish ready.json last so dispatchers only see complete preparation files."""
    requests = sorted((out / 'preparation').glob('*.json'))
    formulas = {read_json(path)['formula'] for path in requests}
    for row in result['formula_results']:
        if row['formula'] in formulas:
            row.update(status='prepared', scored_structures=0)
    if requests:
        result.update(status='prepared', prediction_complete=False, scored_structures=0)
    write_json(out / 'prepared.json', {'schema': 1, 'result': result,
        'requests': [str(path) for path in requests], 'top_k': args.top_k or 10,
        'shard_size': getattr(args, 'model_shard_size', None) or 4096,
        'no_report': getattr(args, 'score_no_report', args.no_report), 'config': getattr(args, 'config', None)})
    write_json(out / 'ready.json', {'schema': 1, 'prepared': str(out / 'prepared.json'),
                                  'sha256': file_fingerprint(out / 'prepared.json', out / 'fingerprints')})


def score(prepared: Path, python: str) -> dict:
    """Score only validated cached predictions; a cache miss cannot launch inference."""
    from msms_structure_elucidation import cli
    saved = read_json(prepared)
    if saved.get('config'):
        os.environ['MSMS_CONFIG'] = saved['config']
    result = saved['result']
    if saved['requests'] and not result['model_settings'].get('prediction_cache'):
        raise ValueError('Prepared scoring requires a configured shared prediction cache')
    out = prepared.parent
    data = inspect_ms(Path(result['input']), result['collision_unit'])
    candidates = list(result.get('candidates', []))
    previous = os.environ.get('MSMS_PREDICTION_CACHE_ONLY')
    os.environ['MSMS_PREDICTION_CACHE_ONLY'] = '1'
    try:
        for path in saved['requests']:
            request = read_json(Path(path))
            formula = request['formula']
            record = next(row for row in result['formula_results'] if row['formula'] == formula)
            rows, count = cli._simulate_shards(python, Path(result['input']), None, formula,
                result['collision_unit'], out, result['model_settings'], result['instrument'],
                saved['shard_size'], saved['top_k'], request['smiles'])
            if record.get('candidate_manifest'):
                from ms_pred import common
                provenance = {common.rm_stereo(row['smiles']): row
                              for row in read_json(Path(record['candidate_manifest']))}
                for row in rows:
                    row.update({key: value for key, value in provenance[row['smiles']].items() if key != 'smiles'})
            candidates.extend(rows)
            record.update(scored_structures=count, status='ranked' if rows else 'no_candidates')
    finally:
        if previous is None:
            os.environ.pop('MSMS_PREDICTION_CACHE_ONLY', None)
        else:
            os.environ['MSMS_PREDICTION_CACHE_ONLY'] = previous
    if result['status'] == 'prepared':
        result['status'] = 'running'
    cli._finalize_result(result, data, candidates, None, result['search_mode'] == 'pubchem-mass',
        result['search_mode'] == 'pubchem-formulas', [], {}, saved['top_k'], out)
    cli._save(result, out, saved['no_report'])
    from msms_structure_elucidation.config import settings
    if settings().get('mcp',{}).get('feature_worker'):
        from msms_structure_elucidation.visualize import write_structure_cache
        write_structure_cache(result,out/'viewer_structures.json')
    return {**result, 'retrieval_path': str(out / 'retrieval.json'), 'report_path': str(out / 'report.html')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    values = read_json(Path(parser.parse_args().request))
    values.update(out=Path(values['out']), spectrum=Path(values['spectrum']))
    record_request(**values)
