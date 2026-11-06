"""Model tasks run by the MCP job runner, and compact summaries of their results.

Every task reuses the CLI's model plumbing: ms-pred work goes through `cli.worker()` in the
ms-pred interpreter, FRIGID and MIST through their skill scripts in the FRIGID interpreter.
This module imports neither ms_pred nor torch, so the MCP server can load it in a light venv.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from msms_structure_elucidation import cli
from msms_structure_elucidation.config import asset
from msms_structure_elucidation.spectrum import inspect_ms
from msms_structure_elucidation.pubchem import DEFAULT_ELEMENTS

REPO = Path(__file__).resolve().parents[3]
MIST_SCRIPT = REPO / '.agents/skills/msms-mist-fingerprint/scripts/run.py'
SUBFORMULAE_SCRIPT = REPO / '.agents/skills/msms-subformulae/scripts/run.py'
INLINE_SPECTRUM = 'spectrum.ms'
# Pipeline tasks prepare and score on CPU; their internal inference jobs acquire GPUs separately.
KINDS = {'predict_spectra': 'pipeline', 'score_candidates': 'pipeline', 'generate_structures_frigid': 'gpu',
         'predict_fingerprint_mist': 'gpu', 'retrieve_atlas': 'pipeline', 'search_pubchem_mass': 'pipeline',
         'search_pubchem_formulas': 'pipeline', 'infer_formulas': 'cpu', 'model_inference': 'gpu',
         'elucidate_feature': 'pipeline', 'prepare_feature_list': 'cpu', 'score_prepared_feature': 'cpu',
         'publish_prediction_list': 'cpu', 'publish_prediction_group': 'cpu', 'build_prediction_group':'cpu'}


def _section(config: dict, name: str) -> dict:
    return config.get('models', {}).get(name, {}) or {}


def _file(path: str | None, config: dict) -> str | None:
    """Resolve a configured asset path; None when unset or missing."""
    resolved = asset(path, config) if path else None
    return resolved if resolved and Path(resolved).exists() else None


def _interpreter(path: str | None, config: dict) -> str | None:
    """A configured interpreter path, kept unresolved so a venv's symlinked python stays in its venv."""
    if not path:
        return None
    interpreter = Path(path).expanduser()
    if not interpreter.is_absolute():
        interpreter = Path(config['_config_path']).parent / interpreter
    return str(interpreter.absolute()) if interpreter.is_file() else None


def missing_assets(task: str, config: dict, model: str = 'iceberg', num_rounds: int = 0) -> list[str]:
    """Config keys this server still needs for a task; empty when the task can run."""
    mcp = config.get('mcp',{})
    if mcp.get('backend')=='pool' and mcp.get('feature_pipeline') and mcp.get('feature_placement','workers')=='workers':
        from msms_structure_elucidation.mcp.workers import REMOTE_TASKS, request_contract
        if task in REMOTE_TASKS:
            selected = _section(config,'simulator').get('model','iceberg') if task=='infer_formulas' else model
            eligible = [node for node in mcp.get('servers',{}).values() if node.get('enabled',True) and
                        node.get('features') and selected in node.get('models',[])]
            missing = [] if eligible else [f'mcp.servers: features-enabled {selected} worker']
            identity = request_contract({'model':selected},config)
            if not identity.get('weights') or not identity.get('source'):
                missing.append(f'mcp.approved_model_identities.{selected} (or local approved model files)')
            return missing
    sim, denovo = _section(config, 'simulator'), _section(config, 'denovo')
    missing = []
    if task in ('predict_spectra', 'score_candidates', 'search_pubchem_mass', 'search_pubchem_formulas'):
        if not _file(sim.get('ms_pred_src'), config):
            missing.append('models.simulator.ms_pred_src')
        keys = ('glacier_ckpt',) if model == 'glacier' else ('gen_ckpt', 'inten_ckpt')
        missing += [f'models.simulator.{key}' for key in keys if not _file(sim.get(key), config)]
    if task in ('generate_structures_frigid', 'predict_fingerprint_mist'):
        if not _interpreter(denovo.get('frigid_python'), config):
            missing.append('models.denovo.frigid_python')
        keys = ('frigid_src', 'mist_ckpt') + (('dlm_ckpt',) if task == 'generate_structures_frigid' else ())
        missing += [f'models.denovo.{key}' for key in keys if not _file(denovo.get(key), config)]
        if task == 'generate_structures_frigid' and num_rounds:
            for key, fallback in (('iceberg_gen_ckpt', 'gen_ckpt'), ('iceberg_inten_ckpt', 'inten_ckpt')):
                if not (_file(denovo.get(key), config) or _file(sim.get(fallback), config)):
                    missing.append(f'models.denovo.{key}')
    return missing


def _ms_pred_python(config: dict) -> str:
    return cli.model_python(_section(config, 'simulator').get('python') or None)


def _scientific_call(command: list[str], config: dict, log, preparation: bool = False) -> dict:
    """Run CPU science in its configured interpreter with the current package source."""
    env = {**os.environ,'CUDA_VISIBLE_DEVICES':''}
    if preparation:
        env['MSMS_FEATURE_PREPARATION'] = '1'
    native = asset(_section(config,'simulator').get('ms_pred_src'),config)
    env['PYTHONPATH'] = str(Path(__file__).parents[2])+os.pathsep+env.get('PYTHONPATH','')
    if native:
        env['PYTHONPATH'] = str(Path(native)/'src')+os.pathsep+env['PYTHONPATH']
    done = subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=log,text=True)
    log.write(done.stdout);log.flush()
    if done.returncode:
        raise RuntimeError(f'Scientific CPU task exited {done.returncode}; inspect log.txt')
    for line in reversed(done.stdout.splitlines()):
        if line.startswith('RESULT_JSON='):
            return json.loads(line.removeprefix('RESULT_JSON='))
    raise RuntimeError('Scientific CPU task produced no result')


def _simulator_options(config: dict, model: str, devices: str | None) -> dict:
    """The CLI's model options, with the GPUs this job was given instead of the tuned device list."""
    options = cli._model_options(argparse.Namespace(model=model), config)
    tuned = cli._devices(_section(config, 'simulator').get('cuda_devices'))
    per_gpu = max(1, int(options['num_gpu_workers']) // max(1, len(tuned.split(',')) if tuned else 1))
    options['cuda_devices'] = devices
    options['num_gpu_workers'] = per_gpu * len(devices.split(',')) if devices and model != 'glacier' else 1
    if not devices and model != 'glacier' and os.environ.get('MSMS_PIPELINE_JOB'):
        options['num_gpu_workers'] = per_gpu
    return options


def _spectrum(params: dict, job_dir: Path) -> Path:
    inline = job_dir / INLINE_SPECTRUM
    path = inline if inline.is_file() else Path(params['spectrum_path'])
    if not path.is_file():
        raise FileNotFoundError(f'Spectrum not found on the server: {path}')
    return path


def _instrument(params: dict, metadata: dict, config: dict) -> str:
    """The forward-model instrument: the request, then the spectrum, then the config."""
    instrument = cli._instrument(params.get('instrument') or metadata.get('instrumentation')
                                 or _section(config, 'simulator').get('instrument'))
    if not instrument:
        raise ValueError('ICEBERG and GLACIER need an instrument: pass instrument (Orbitrap, QTOF, IT-FT or Unknown), '
                         'add >instrumentation to the spectrum, or set models.simulator.instrument')
    return instrument


def predict_spectra(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    listing = job_dir / 'smiles.json'
    listing.write_text(json.dumps(params['smiles']))
    options = _simulator_options(config, params['model'], devices)
    predictions = cli.worker(_ms_pred_python(config), 'predict', smiles_json=listing,
        collision_energies=','.join(str(value) for value in params['collision_energies']),
        collision_unit=params['collision_unit'], adduct=params['adduct'],
        instrument=_instrument(params, {}, config), fragments=bool(params.get('include_fragments')),
        **({'stepped': True} if params.get('stepped') else {}),
        **options)
    return {'model': params['model'].upper(), 'predictions': predictions}


def score_candidates(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    native_score = config.get('_mcp_native_score',False)
    if config.get('mcp',{}).get('feature_worker') and not native_score:
        return _scientific_call([_ms_pred_python(config),'-m','msms_structure_elucidation.mcp.scoring',
                                '--job',str(job_dir)],config,log)
    path = _spectrum(params, job_dir)
    data = inspect_ms(path, params['collision_unit'])
    python = _ms_pred_python(config)
    smiles, rejected = params['smiles'], []
    observed = {key: data[key] for key in ('parentmass', 'adduct', 'spectra', 'collision_unit', 'energy_mapping')}
    observed['input'] = str(path)
    if data.get('acquisition_mode') == 'stepped':
        observed.update(acquisition_mode='stepped', stepped_collision=data['stepped_collision'])
    formula = params.get('formula') or data['metadata'].get('formula')
    if formula:
        listing = job_dir / 'proposed.json'
        listing.write_text(json.dumps(smiles))
        if native_score:
            from msms_structure_elucidation.worker import validate_smiles
            checked = validate_smiles(smiles,formula)
        else:
            checked = cli.worker(python, 'validate', smiles_json=listing, formula=formula)
        smiles = [row['smiles'] for row in checked if row['formula_match']]
        rejected = [row for row in checked if not row['formula_match']]
    if not smiles:
        return {**observed, 'status': 'no_candidates', 'formula': formula, 'candidates': [], 'rejected': rejected}
    listing = job_dir / 'smiles.json'
    listing.write_text(json.dumps(smiles))
    if native_score:
        from msms_structure_elucidation.worker import simulate
        options = _simulator_options(config,params['model'],devices)
        options.pop('ms_pred_dir',None)
        candidates = simulate(str(path),smiles,formula or '',experimental_unit=params['collision_unit'],
            output_dir=str(job_dir),instrument=_instrument(params,data['metadata'],config),top_k=params['top_k'],**options)
    else:
        candidates = cli.worker(python, 'simulate', spectrum=path, smiles_json=listing, formula=formula or '',
            experimental_unit=params['collision_unit'], output_dir=job_dir, instrument=_instrument(params, data['metadata'], config),
            top_k=params['top_k'], limit_images=True, **_simulator_options(config, params['model'], devices))
    result = {**observed, 'status': 'ranked' if candidates else 'no_candidates',
              'formula': formula, 'candidates': candidates, 'rejected': rejected}
    if native_score:
        from msms_structure_elucidation.visualize import write_structure_cache
        write_structure_cache(result,job_dir/'viewer_structures.json')
    return result


def retrieve_atlas(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    from msms_structure_elucidation.mcp.pipeline import group_size
    out = job_dir / 'retrieval'
    command = [sys.executable, '-m', 'msms_structure_elucidation.cli', 'run', '--input', str(_spectrum(params, job_dir)),
               '--collision-unit', params['collision_unit'], '--output-dir', str(out), '--no-report',
               '--ms-pred-python', _ms_pred_python(config), '--cuda-devices', devices or '']
    command += ['--model-shard-size', str(group_size(config, 'iceberg'))]
    for key in ('formula', 'top_k', 'instrument'):
        if params.get(key):
            command += ['--' + key.replace('_', '-'), str(params[key])]
    done = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    result_path = out / 'retrieval.json'
    if not result_path.is_file():
        raise RuntimeError(f'Atlas retrieval exited with {done.returncode} and wrote no retrieval.json; see log.txt')
    return json.loads(result_path.read_text())


def search_pubchem_mass(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    """Run mass or explicitly selected formula candidates using this job's allocated devices."""
    path = _spectrum(params, job_dir)
    metadata = inspect_ms(path, params['collision_unit'])['metadata']
    out = job_dir / 'retrieval'
    command = [sys.executable, '-m', 'msms_structure_elucidation.cli', 'run',
               '--input', str(path), '--collision-unit', params['collision_unit'], '--output-dir', str(out),
               '--ms-pred-python', _ms_pred_python(config), '--ms1-ppm', str(params.get('ppm', 5)),
               '--formula-elements', params.get('elements', DEFAULT_ELEMENTS), '--top-k', str(params['top_k']),
               '--instrument', _instrument(params, metadata, config)]
    if params.get('formulas'):
        listing = job_dir / 'formulas.txt'
        listing.write_text('\n'.join(params['formulas'])+'\n')
        command += ['--formulas-file', str(listing)]
    else:
        command.append('--mass-only')
    names = {'batch_size': 'model-batch-size', 'num_cpu_workers': 'model-cpu-workers',
             'num_gpu_workers': 'model-gpu-workers'}
    options = _simulator_options(config, params['model'], devices)
    for key, value in options.items():
        if value is not None:
            command += ['--' + names.get(key, key.replace('_', '-')), str(value)]
    if options['prediction_cache'] is None:
        command.append('--no-prediction-cache')
    # An empty device list explicitly selects CPU even when the host config names GPUs.
    if devices is None:
        command += ['--cuda-devices', '']
    from msms_structure_elucidation.mcp.pipeline import group_size
    shard_size = params.get('shard_size') or group_size(config, params['model'])
    command += ['--model-shard-size', str(shard_size)]
    done = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    result_path = out / 'retrieval.json'
    if done.returncode or not result_path.is_file():
        raise RuntimeError(f'PubChem mass search exited with {done.returncode}; see {result_path} and log.txt')
    return {**json.loads(result_path.read_text()), 'retrieval_path': str(result_path),
            'report_path': str(out / 'report.html')}


def infer_formulas(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    """Return BUDDY's top three hypotheses without candidate retrieval or spectral prediction."""
    from msms_structure_elucidation.spectrum import mass_tolerance
    path = _spectrum(params, job_dir)
    data = inspect_ms(path, params['collision_unit'])
    instrument = params.get('instrument') or data['metadata'].get('instrumentation')
    tolerance = mass_tolerance(instrument)
    for key in ('ms1_ppm', 'ms2_ppm'):
        if params.get(key) is not None:
            tolerance[key] = params[key]
    hypotheses = cli.worker(_ms_pred_python(config), 'formula', spectrum=path,
        experimental_unit=params['collision_unit'], ms1_ppm=tolerance['ms1_ppm'], ms2_ppm=tolerance['ms2_ppm'])
    return {'input': str(path), 'formula_hypotheses': hypotheses[:3], 'mass_tolerance': tolerance,
            'status': 'ranked' if hypotheses else 'needs_formula'}


def _adduct(params: dict, metadata: dict) -> str:
    return params.get('adduct') or metadata.get('ionization') or '[M+H]+'


def generate_structures_frigid(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    path = _spectrum(params, job_dir)
    metadata = inspect_ms(path, 'eV')['metadata']
    sim, denovo = _section(config, 'simulator'), _section(config, 'denovo')
    return cli.run_denovo(str(path), params['formula'], _adduct(params, metadata), job_dir / 'denovo',
        ms_pred_python=_ms_pred_python(config), frigid_python=_interpreter(denovo['frigid_python'], config),
        frigid_dir=_file(denovo['frigid_src'], config), mist_ckpt=_file(denovo['mist_ckpt'], config),
        dlm_ckpt=_file(denovo['dlm_ckpt'], config), num_rounds=params['num_rounds'], top_k=params['top_k'],
        iceberg_gen_ckpt=_file(denovo.get('iceberg_gen_ckpt'), config) or _file(sim.get('gen_ckpt'), config),
        iceberg_inten_ckpt=_file(denovo.get('iceberg_inten_ckpt'), config) or _file(sim.get('inten_ckpt'), config),
        instrument=params.get('instrument'), cuda_devices=devices or '', stdout=log)


_READ_MIST = '''import h5py, json, sys
with h5py.File(sys.argv[1]) as h5:
    probs = h5['fp_probs'][()]
    print('RESULT_JSON=' + json.dumps([round(float(p), 5) for p in probs[0]] if len(probs) else None))
'''


def predict_fingerprint_mist(params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    path = _spectrum(params, job_dir)
    metadata = inspect_ms(path, 'eV')['metadata']
    adduct, denovo = _adduct(params, metadata), _section(config, 'denovo')
    frigid_python = _interpreter(denovo['frigid_python'], config)
    subform = job_dir / 'subformulae'
    subprocess.run([_ms_pred_python(config), str(SUBFORMULAE_SCRIPT), '--spectrum', str(path), '--formula',
                    params['formula'], '--adduct', adduct, '--output-dir', str(subform)],
                   cwd=REPO, check=True, stdout=log, stderr=subprocess.STDOUT)
    output = job_dir / 'mist_fingerprint.hdf5'
    command = [frigid_python, str(MIST_SCRIPT), '--spectrum', str(path), '--formula', params['formula'],
               '--subform-dir', str(subform), '--adduct', adduct, '--fp-threshold', str(params['threshold']),
               '--frigid-dir', _file(denovo['frigid_src'], config), '--mist-ckpt', _file(denovo['mist_ckpt'], config),
               # MIST runs torch in-process, where the job's visible GPU is always device 0.
               '--cuda-devices', '0' if devices else '',
               '--output', str(output), '--failure-log', str(job_dir / 'mist_failures.log')]
    if params.get('instrument'):
        command += ['--instrument', params['instrument']]
    subprocess.run(command, cwd=REPO, check=True, stdout=log, stderr=subprocess.STDOUT)
    read = subprocess.run([frigid_python, '-c', _READ_MIST, str(output)], capture_output=True, text=True, check=True)
    probs = json.loads(read.stdout.strip().splitlines()[-1].removeprefix('RESULT_JSON='))
    if probs is None:
        failures = (job_dir / 'mist_failures.log').read_text().strip()
        raise RuntimeError(f'MIST produced no fingerprint: {failures or "see log.txt"}')
    return {'formula': params['formula'], 'adduct': adduct, 'threshold': params['threshold'],
            'fingerprint_bits': len(probs), 'probabilities': probs,
            'on_bits': [bit for bit, p in enumerate(probs) if p >= params['threshold']],
            'hdf5': str(output)}


RUNNERS = {'predict_spectra': predict_spectra, 'score_candidates': score_candidates,
           'retrieve_atlas': retrieve_atlas, 'generate_structures_frigid': generate_structures_frigid,
           'predict_fingerprint_mist': predict_fingerprint_mist, 'search_pubchem_mass': search_pubchem_mass,
           'infer_formulas': infer_formulas, 'search_pubchem_formulas': search_pubchem_mass}


def run(task: str, params: dict, job_dir: Path, config: dict, devices: str | None, log) -> dict:
    if task in ('prepare_feature_list', 'publish_prediction_list', 'publish_prediction_group', 'score_prepared_feature'):
        if task=='publish_prediction_group':
            command = [_ms_pred_python(config), '-m', 'msms_structure_elucidation.mcp.raw_outputs',
                       '--publish-job',str(job_dir)]
        else:
            action = 'score' if task == 'score_prepared_feature' else 'prepare'
            command = [_ms_pred_python(config), '-m', 'msms_structure_elucidation.mcp.features', action]
            command += ['--job', str(job_dir)] if action == 'prepare' else ['--prepared', params['prepared']]
        return _scientific_call(command,config,log,preparation=True)
    if task == 'model_inference':
        from msms_structure_elucidation.mcp.pipeline import inference
        return inference(params, job_dir, config, devices, log)
    return RUNNERS[task](params, job_dir, config, devices, log)


def _top_peaks(peaks: list, limit: int) -> list:
    rows = sorted(peaks, key=lambda row: row[1], reverse=True)[:limit]
    return [[round(row[0], 5), round(row[1], 4), *row[2:]] for row in sorted(rows, key=lambda row: row[0])]


def _candidate(row: dict) -> dict:
    keep = ('smiles', 'formula', 'source', 'entropy_similarity', 'explained_intensity', 'collision_energies', 'name',
            'pubchem_cids', 'pubchem_inchikeys', 'pubchem_smiles', 'mass_error_ppm', 'monoisotopic_mass')
    summary = {key: row[key] for key in keep if key in row}
    if 'matched_peaks' in row:
        summary['matched_peaks'] = len(row['matched_peaks'])
    return summary


def summarize(task: str, result: dict, params: dict) -> dict:
    """A compact view of a task result for the MCP client; the full result stays in result.json."""
    if task == 'predict_spectra':
        limit = params.get('max_peaks', 50)
        rows = []
        for prediction in result['predictions']:
            peaks = prediction.get('annotated_peaks') or prediction['predicted_spectra']
            rows.append({'smiles': prediction['smiles'], 'adduct': prediction['adduct'],
                         'peak_columns': ['mz', 'intensity', 'fragment_formula', 'fragment_smiles']
                         if prediction.get('annotated_peaks') else ['mz', 'intensity'],
                         'spectra': {ce: _top_peaks(values, limit) for ce, values in peaks.items()}})
        return {'model': result['model'], 'collision_unit': params['collision_unit'], 'predictions': rows}
    if task == 'score_candidates':
        return {'formula': result['formula'], 'candidates': [_candidate(row) for row in result['candidates']],
                'rejected': result['rejected']}
    if task in ('retrieve_atlas', 'search_pubchem_mass', 'search_pubchem_formulas', 'elucidate_feature', 'score_prepared_feature'):
        evidence = dict(result.get('review_evidence') or {})
        evidence.pop('matched_peaks', None)
        evidence['strongest_unexplained_peaks'] = evidence.get('strongest_unexplained_peaks', [])[:10]
        return {'status': result['status'], 'formula': result.get('formula'), 'warnings': result.get('warnings', []),
                'formula_results': result.get('formula_results', []), 'next_step': result.get('next_step'),
                'candidates': [_candidate(row) for row in result.get('candidates', [])], 'review_evidence': evidence,
                **{key: result[key] for key in ('pubchem_mass_search', 'prediction_complete', 'scored_structures',
                                               'retrieval_path', 'report_path') if key in result}}
    if task == 'predict_fingerprint_mist':
        ranked = sorted(range(len(result['probabilities'])), key=lambda bit: result['probabilities'][bit], reverse=True)
        return {key: result[key] for key in ('formula', 'adduct', 'threshold', 'fingerprint_bits', 'on_bits', 'hdf5')} | {
            'top_bits': [[bit, result['probabilities'][bit]] for bit in ranked[:50]]}
    if task == 'prepare_feature_list':
        return {key:len(result.get(key,[])) for key in
                ('prepared_features','processed_features','scored_jobs','held_features','held_scores')}
    return result
