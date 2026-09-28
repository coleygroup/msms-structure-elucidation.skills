"""Portable MS/MS elucidation CLI. Scientific work is delegated to ms-pred Python."""
from __future__ import annotations

import argparse
import csv
import concurrent.futures
from collections import Counter
import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from pathlib import Path

from msms_structure_elucidation import hostprobe
from msms_structure_elucidation.atlas import AtlasNoEntry, download_mgf
from msms_structure_elucidation.config import asset, choice, settings
from msms_structure_elucidation.report import write_report
from msms_structure_elucidation.spectrum import inspect_ms, mass_tolerance

_MODEL_SEMAPHORE = None


def _instrument(value: str | None) -> str | None:
    if not value:
        return None
    compact = value.upper().replace('-', '').replace(' ', '')
    if compact.startswith('ORBITRAP'):
        return 'Orbitrap'
    if 'QTOF' in compact:
        return 'QTOF'
    return {'QTOF': 'QTOF', 'ORBITRAP': 'Orbitrap', 'ITFT': 'IT-FT'}.get(compact, value)


def model_python(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.exists() else shutil.which(explicit) or explicit
    if os.environ.get('MS_PRED_PYTHON'):
        return model_python(os.environ['MS_PRED_PYTHON'])
    try:
        import ms_pred  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    for name in ('ms-gen', 'ms-pred'):
        conda = shutil.which('conda')
        if conda:
            probe = subprocess.run([conda, 'run', '-n', name, 'python', '-c',
                'import ms_pred; import sys; print(sys.executable)'], capture_output=True, text=True)
            if probe.returncode == 0:
                return probe.stdout.strip().splitlines()[-1]
    raise RuntimeError('ms-pred is unavailable. Set --ms-pred-python to a Python with ms_pred installed, or run bash setup_envs.sh.')


def worker(python: str, action: str, **kwargs):
    cwd = kwargs.pop('ms_pred_dir', None)
    if cwd:
        cwd = Path(cwd).expanduser().resolve()
        if not (cwd / 'src/ms_pred').is_dir():
            raise FileNotFoundError(f'Not an ms-pred checkout: {cwd}. Clone https://github.com/coleygroup/ms-pred and follow its README.')
    if cwd is None and Path(python).resolve() == Path(sys.executable).resolve():
        from msms_structure_elucidation import worker as science
        if action == 'rank':
            return science.rank(str(kwargs['spectrum']), str(kwargs['mgf']), kwargs['formula'],
                int(kwargs['top_k']), kwargs['experimental_unit'])
        if action == 'formula':
            return science.formula_candidates(str(kwargs['spectrum']), kwargs['experimental_unit'],
                ms1_ppm=float(kwargs.get('ms1_ppm') or 5.0), ms2_ppm=float(kwargs.get('ms2_ppm') or 10.0))
        if action == 'validate':
            return science.validate_smiles(json.loads(Path(kwargs['smiles_json']).read_text()), kwargs['formula'])
        if action == 'atlas-smiles':
            return science.atlas_smiles(str(kwargs['mgf']))
        if action == 'atlas-info':
            return science.atlas_info(str(kwargs['mgf']), [int(x) for x in kwargs['energies'].split(',')],
                str(kwargs['spectrum']), kwargs['formula'], kwargs['experimental_unit'])
    cmd = [python, '-m', 'msms_structure_elucidation.worker', action]
    for key, value in kwargs.items():
        if value is not None:
            cmd += ['--' + key.replace('_', '-'), str(value)]
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(x for x in (
        str(cwd / 'src') if cwd else None, str(Path(__file__).resolve().parents[1]), env.get('PYTHONPATH')) if x)
    run = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    if run.returncode:
        raise RuntimeError(f'{action} failed (exit {run.returncode}):\n{run.stdout[-6000:]}\n{run.stderr[-6000:]}')
    for line in reversed(run.stdout.splitlines()):
        if line.startswith('RESULT_JSON='):
            return json.loads(line.removeprefix('RESULT_JSON='))
    raise RuntimeError(f'No worker result: {run.stdout[-2000:]}')


def _model_options(args, config: dict, default_model='iceberg') -> dict:
    model_cfg = config.get('models', {}).get('simulator', {})
    def path_option(cli_value, env_name, configured):
        if cli_value:
            return str(Path(cli_value).expanduser().resolve())
        if env_name and os.environ.get(env_name):
            return str(Path(os.environ[env_name]).expanduser().resolve())
        return asset(configured, config)
    return {'model': getattr(args, 'model', None) or model_cfg.get('model') or default_model,
        'ms_pred_dir': path_option(getattr(args, 'ms_pred_dir', None), 'MS_PRED_DIR', model_cfg.get('ms_pred_src')),
        'checkpoint': path_option(getattr(args, 'checkpoint', None), None, model_cfg.get('glacier_ckpt')),
        'gen_checkpoint': path_option(getattr(args, 'gen_checkpoint', None), None, model_cfg.get('gen_ckpt')),
        'inten_checkpoint': path_option(getattr(args, 'inten_checkpoint', None), None, model_cfg.get('inten_ckpt')),
        'cuda_devices': choice(getattr(args, 'cuda_devices', None), 'MSMS_CUDA_DEVICES', model_cfg.get('cuda_devices')),
        'batch_size': getattr(args, 'model_batch_size', None) or model_cfg.get('batch_size', 1),
        'num_cpu_workers': getattr(args, 'model_cpu_workers', None) or model_cfg.get('num_cpu_workers', 1),
        'num_gpu_workers': getattr(args, 'model_gpu_workers', None) or model_cfg.get('num_gpu_workers', 1)}


def _save(result: dict, out: Path, no_report=False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'retrieval.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    if not no_report:
        write_report(result, out / 'report.html')


def _simulate_shards(python: str, path: Path, mgf: Path, formula: str, unit: str,
                     out: Path, options: dict, instrument: str | None, shard_size: int,
                     top_k: int, smiles: list[str] | None = None) -> tuple[list[dict], int]:
    from msms_structure_elucidation.worker import sort_candidates
    if not options['ms_pred_dir']:
        raise FileNotFoundError('Set --ms-pred-dir or models.simulator.ms_pred_src to an ms-pred checkout for local model inference')
    if options['model'] != 'iceberg':
        raise ValueError('Atlas energy fallback uses ICEBERG on every atlas structure; set --model iceberg')
    if not all(options.get(k) and Path(options[k]).is_file() for k in ('gen_checkpoint', 'inten_checkpoint')):
        raise FileNotFoundError('ICEBERG generation and intensity checkpoints are required for exact-energy fallback. Set them in configs/default.yaml or --gen-checkpoint/--inten-checkpoint; see ms-pred README for public weights.')
    if smiles is None:
        smiles = worker(python, 'atlas-smiles', mgf=mgf)
    if not smiles:
        return [], 0
    shards = out / 'model_shards' / formula
    shards.mkdir(parents=True, exist_ok=True)
    leaders = []
    input_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for start in range(0, len(smiles), shard_size):
        chunk = smiles[start:start + shard_size]
        checkpoint_versions = {k: (str(options.get(k)), Path(options[k]).stat().st_size, Path(options[k]).stat().st_mtime_ns)
            for k in ('gen_checkpoint', 'inten_checkpoint') if options.get(k)}
        signature = hashlib.sha256(json.dumps([chunk, input_hash, unit, instrument,
            {k: str(options.get(k)) for k in ('model','ms_pred_dir','cuda_devices','batch_size')},
            checkpoint_versions], sort_keys=True).encode()).hexdigest()[:16]
        saved = shards / f'{start:08d}-{signature}.json'
        if saved.is_file():
            candidates = json.loads(saved.read_text())
        else:
            listing = shards / f'{start:08d}-{signature}.smiles.json'
            listing.write_text(json.dumps(chunk))
            if _MODEL_SEMAPHORE is None:
                candidates = worker(python, 'simulate', spectrum=path, smiles_json=listing,
                    formula=formula, experimental_unit=unit, output_dir=out,
                    instrument=instrument, **options)
            else:
                with _MODEL_SEMAPHORE:
                    candidates = worker(python, 'simulate', spectrum=path, smiles_json=listing,
                        formula=formula, experimental_unit=unit, output_dir=out,
                        instrument=instrument, **options)
            temp = saved.with_suffix('.tmp')
            temp.write_text(json.dumps(candidates, allow_nan=False))
            temp.replace(saved)
        leaders = sort_candidates(leaders + candidates)[:top_k]
    return leaders, len(smiles)


def run(args):
    path = Path(args.input).expanduser().resolve()
    if path.suffix.lower() != '.ms':
        raise ValueError('Use a .ms spectrum. For raw or mzML input, run the preprocess/feature-detect skills first.')
    data = inspect_ms(path, args.collision_unit)
    config = settings(getattr(args, 'config', None))
    model_options = _model_options(args, config)
    top_k = getattr(args, 'top_k', None) or config.get('models', {}).get('retrieval', {}).get('top_k', 10)
    shard_size = getattr(args, 'model_shard_size', None) or config.get('models', {}).get('simulator', {}).get('shard_size', 256)
    if shard_size < 1 or top_k < 1:
        raise ValueError('--model-shard-size and --top-k must be positive')
    out = Path(args.output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = {'input': str(path), 'parentmass': data['parentmass'], 'adduct': data['adduct'],
              'peaks': data['peaks'], 'spectra': data['spectra'], 'candidates': [], 'warnings': [],
              'collision_unit': data['collision_unit'], 'header_units': data['header_units'],
              'energy_mapping': data['energy_mapping'], 'status': 'running',
              'instrument': _instrument(getattr(args, 'instrument', None) or data['metadata'].get('instrumentation') or config.get('models', {}).get('simulator', {}).get('instrument')),
              'formula_results': [], 'config': config['_config_path'],
              'model_settings': model_options}
    if data['header_units'] and data['header_units'] != [args.collision_unit]:
        result['warnings'].append(
            f"Collision headers say {', '.join(data['header_units'])}; using user-supplied {args.collision_unit}.")
    tolerance = mass_tolerance(result['instrument'])
    overrides = {k: getattr(args, k, None) for k in ('ms1_ppm', 'ms2_ppm')}
    tolerance.update({k: v for k, v in overrides.items() if v is not None})
    tolerance['source'] = 'user' if any(v is not None for v in overrides.values()) else 'instrument default'
    result['mass_tolerance'] = tolerance
    if tolerance['instrument'] == 'unknown' and tolerance['source'] != 'user':
        result['warnings'].append(
            f"Instrument not recognised; using Q-TOF mass tolerances ({tolerance['ms1_ppm']:g}/{tolerance['ms2_ppm']:g} ppm). "
            'Pass --instrument or --ms1-ppm/--ms2-ppm (Orbitrap: 5/10).')
    python = model_python(getattr(args, 'ms_pred_python', None))
    formulas = []
    if getattr(args, 'formulas_file', None):
        formulas = [x.strip() for x in Path(args.formulas_file).read_text().splitlines() if x.strip() and not x.startswith('#')]
        result['formula_source'] = 'user list'
    elif args.formula or data['metadata'].get('formula'):
        formulas = [args.formula or data['metadata']['formula']]
        result['formula_source'] = 'provided' if args.formula else 'input'
    else:
        try:
            inferred = worker(python, 'formula', spectrum=path, experimental_unit=args.collision_unit,
                              ms1_ppm=tolerance['ms1_ppm'], ms2_ppm=tolerance['ms2_ppm'])
            formulas = [x['formula'] for x in inferred[:3]]
            result['formula_hypotheses'] = inferred
            result['formula_source'] = 'MSBuddy inferred'
        except Exception as exc:
            result['warnings'].append(f'Formula inference unavailable: {exc}')
        if not formulas and not getattr(args, 'no_pubchem', False):
            formulas = pubchem_formulas(result, data, getattr(args, 'formula_elements', None) or 'CHNOPS')
    formulas = list(dict.fromkeys(formulas))
    result['formula'] = formulas[0] if formulas else None
    if not formulas:
        result['status'] = 'needs_formula'
        result['warnings'].append('No formula available. Supply --formula or --formulas-file, or obtain formula hypotheses from isotope/MS1 evidence. FRIGID also requires a formula.')
        if result.get('pubchem_formula_search') == 'none':
            result['next_step'] = 'review-frigid'
    candidates = []
    operational_error = None
    for formula in formulas:
        record = {'formula': formula, 'status': 'running'}
        result['formula_results'].append(record)
        try:
            if args.atlas_mgf and len(formulas) > 1:
                raise ValueError('--atlas-mgf can only be used with one formula')
            cache_dir = Path(getattr(args, 'atlas_cache_dir', None) or out / 'atlas').resolve()
            mgf = Path(args.atlas_mgf).resolve() if args.atlas_mgf else download_mgf(formula, data['adduct'], cache_dir / f'{formula}_{data["adduct"].replace("/", "_")}.mgf',
                getattr(args, 'atlas_url', None) or config.get('models', {}).get('retrieval', {}).get('atlas_url', 'https://iceberg-ms.mit.edu'))
            coverage = worker(python, 'atlas-info', spectrum=path, mgf=mgf, formula=formula,
                experimental_unit=args.collision_unit,
                energies=','.join(str(row['model_energy_ev']) for row in data['energy_mapping']))
            record.update({'atlas_mgf': str(mgf), 'library_structures': coverage['library_structures'],
                'missing_energies_ev': coverage['missing_energies_ev']})
            if coverage['missing_energies_ev'] and coverage['library_structures']:
                record['status'] = 'model_fallback'
                record['reason'] = 'no_exact_atlas_energy'
                record['prediction_source'] = 'local ICEBERG'
                simulated, simulated_count = _simulate_shards(python, path, mgf, formula, args.collision_unit, out,
                    model_options, result['instrument'],
                    shard_size, top_k, coverage['smiles'])
                candidates.extend(simulated)
                record['status'] = 'ranked' if simulated else 'no_model_predictions'
                record['scored_structures'] = simulated_count
            else:
                ranked = worker(python, 'rank', spectrum=path, mgf=mgf, formula=formula,
                    top_k=top_k, experimental_unit=args.collision_unit) if coverage['library_structures'] else {
                    'candidates': [], 'scored_structures': 0}
                record['scored_structures'] = ranked['scored_structures']
                candidates.extend(ranked['candidates'])
                record['status'] = 'ranked' if ranked['candidates'] else 'no_atlas_coverage' if not coverage['library_structures'] else 'no_comparable_candidates'
                record['prediction_source'] = 'public ICEBERG atlas'
        except Exception as exc:
            if isinstance(exc, AtlasNoEntry):
                record['status'] = 'no_atlas_coverage'
                record['reason'] = str(exc)
            elif isinstance(exc, ValueError) and str(exc).startswith('Formula/adduct precursor mismatch'):
                record['status'] = 'formula_incompatible'
                record['reason'] = str(exc)
            else:
                record['status'] = 'blocked_model_assets' if isinstance(exc, FileNotFoundError) and record.get('reason') == 'no_exact_atlas_energy' else 'error'
                record['error'] = str(exc)
                operational_error = exc
                break
    from msms_structure_elucidation.worker import sort_candidates
    result['candidates'] = sort_candidates(candidates)[:top_k]
    if result['candidates']:
        best = result['candidates'][0]
        result['formula'] = best['formula']
        result['atlas_mgf'] = next((r['atlas_mgf'] for r in result['formula_results'] if r['formula'] == best['formula'] and 'atlas_mgf' in r), None)
        scores = [c['entropy_similarity'] for c in result['candidates']]
        result['qc'] = {'experimental_collision_energies': len(data['spectra']), 'experimental_peaks': data['peaks'],
            'library_structures': sum(r.get('library_structures', 0) for r in result['formula_results']),
            'scored_structures': sum(r.get('scored_structures', 0) for r in result['formula_results']),
            'nonzero_top_scores': sum(s > 0 for s in scores), 'top_score_range': [min(scores), max(scores)],
            'top_energy_pairs': len(best['energy_alignment']),
            'top_predicted_peaks': sum(len(v) for v in best['predicted_spectra'].values())}
        matched = {(str(p['ce']), p['mz']) for p in best['matched_peaks']}
        unexplained = [{'ce': ce, 'mz': mz, 'intensity': intensity} for ce, peaks in data['spectra'].items()
            for mz, intensity in peaks if (str(ce), mz) not in matched]
        result['review_evidence'] = {'best_candidate': best['smiles'], 'entropy_similarity': best['entropy_similarity'],
            'explained_intensity': best['explained_intensity'], 'matched_peak_count': len(best['matched_peaks']),
            'total_peak_count': data['peaks'], 'matched_peaks': best['matched_peaks'],
            'strongest_unexplained_peaks': sorted(unexplained, key=lambda p: p['intensity'], reverse=True)[:20],
            'weak_match_review_suggested': best['entropy_similarity'] < 0.5 or best['explained_intensity'] < 0.5 or len(best['matched_peaks']) < 10}
    result['status'] = ('blocked_model_assets' if operational_error and any(r['status'] == 'blocked_model_assets' for r in result['formula_results']) else
        'error' if operational_error else 'ranked' if candidates else
        result['status'] if result['status'] == 'needs_formula' else 'no_atlas_coverage' if
        all(r['status'] == 'no_atlas_coverage' for r in result['formula_results']) else 'no_candidates')
    if result['status'] == 'no_atlas_coverage':
        result['next_step'] = 'iceberg-pubchem'
        result['warnings'].append(
            'No ICEBERG Atlas entry for the searched formulas. Predict their PubChem structures with ICEBERG: '
            f'review --result {out / "retrieval.json"} --proposals pubchem --model iceberg (add --formula to pick one).')
    _save(result, out, args.no_report)
    print(json.dumps({'output_dir': str(out), 'status': result['status'], 'formula': result['formula'],
        'candidates': len(result['candidates']), 'formula_results': result['formula_results'],
        'warnings': result['warnings']}, indent=2))
    if operational_error:
        raise RuntimeError(f'{operational_error} (result: {out / "retrieval.json"})') from operational_error
    return 0


def pubchem_formulas(result: dict, data: dict, elements: str, limit: int = 3) -> list[str]:
    """When MSBuddy proposes nothing, take formulas of PubChem structures matching the precursor mass."""
    from msms_structure_elucidation import pubchem
    ppm = result['mass_tolerance']['ms1_ppm']
    try:
        found = pubchem.formulas_by_mass(data['parentmass'], data['adduct'], ppm, elements, limit=limit)
    except Exception as exc:
        result['warnings'].append(f'PubChem mass search failed: {exc}')
        return []
    if not found:
        result['pubchem_formula_search'] = 'none'
        result['warnings'].append(
            f'No {elements} formula of a PubChem structure lies within ±{ppm:g} ppm of the precursor. '
            'Review the spectrum (adduct, in-source fragment, isotope, noise) and decide whether to generate '
            'structures de novo with FRIGID (msms-denovo).')
        return []
    result['formula_hypotheses'] = found
    result['formula_source'] = found[0]['source']
    result['warnings'].append(
        f'MSBuddy proposed no formula; using PubChem mass matches ({", ".join(f["formula"] for f in found)}).')
    return [f['formula'] for f in found]


def proposal_file(args, result: dict, result_path: Path, formula: str) -> Path:
    """SMILES to review: an agent/user JSON list, or PubChem structures of the formula."""
    if args.smiles_json:
        return Path(args.smiles_json).resolve()
    from msms_structure_elucidation import pubchem
    smiles = pubchem.structures_for_formula(formula, args.max_structures)
    path = result_path.with_name(f'proposals_{args.proposals}_{formula}.json')
    path.write_text(json.dumps(smiles, indent=0))
    return path


def review(args):
    """Validate agent-proposed SMILES, reuse atlas matches, simulate absent structures."""
    result_path = Path(args.result).resolve()
    result = json.loads(result_path.read_text())
    config = settings(getattr(args, 'config', None))
    options = _model_options(args, config, default_model='glacier')
    formula = getattr(args, 'formula', None) or result.get('formula')
    if not formula:
        raise ValueError('Review needs a molecular formula; rerun retrieval with --formula')
    smiles_json = proposal_file(args, result, result_path, formula)
    proposals = json.loads(smiles_json.read_text())
    if not isinstance(proposals, list) or not all(isinstance(x, str) for x in proposals):
        raise ValueError('--smiles-json must contain a JSON array of SMILES strings')
    if not proposals:
        raise ValueError(f'No {getattr(args, "proposals", None) or "proposed"} structures found for {formula}')
    python = model_python(args.ms_pred_python)
    validated = worker(python, 'validate', smiles_json=smiles_json, formula=formula)
    valid = [c['smiles'] for c in validated if c['formula_match']]
    rejected = [c for c in validated if not c['formula_match']]
    if not valid:
        raise ValueError('No proposed SMILES matched the target formula')
    existing = list(result.get('candidates', []))
    atlas_mgf = next((r.get('atlas_mgf') for r in result.get('formula_results', []) if r['formula'] == formula), result.get('atlas_mgf'))
    if atlas_mgf and Path(atlas_mgf).exists() and not any(
            row.get('missing_energies_ev') for row in result.get('formula_results', []) if row['formula'] == formula):
        atlas = worker(python, 'rank', spectrum=result['input'], mgf=atlas_mgf,
                       formula=formula, top_k=100000,
                       experimental_unit=result['collision_unit'])
        atlas_candidates = atlas['candidates']
    else:
        atlas_candidates = []

    def key(candidate):
        # Stereo-insensitive identity: the public atlas stores SMILES without stereochemistry.
        inchikey = candidate.get('inchikey') or ''
        return inchikey[:14] if inchikey else candidate.get('canonical_smiles', candidate['smiles'])
    by_key = {key(c): c for c in atlas_candidates}
    connectivity = {c['smiles']: c.get('connectivity') or c['smiles'] for c in validated if c['formula_match']}
    seen = {key(c) for c in existing}
    added = []
    for smiles in valid:
        k = connectivity[smiles]
        if k in by_key and k not in seen:
            added.append(by_key[k]); seen.add(k)
    missing = []
    for smiles in valid:
        if connectivity[smiles] not in seen:
            missing.append(smiles); seen.add(connectivity[smiles])
    if missing:
        if not options['ms_pred_dir']:
            raise FileNotFoundError('Review simulation requires --ms-pred-dir or models.simulator.ms_pred_src')
        temp = result_path.parent / 'review_smiles.json'
        temp.write_text(json.dumps(missing))
        added.extend(worker(python, 'simulate', spectrum=result['input'],
            smiles_json=temp, formula=formula, output_dir=result_path.parent,
            experimental_unit=result['collision_unit'],
            instrument=_instrument(getattr(args, 'instrument', None) or result.get('instrument') or config.get('models', {}).get('simulator', {}).get('instrument')),
            **options))
    if added and result.get('next_step') == 'iceberg-pubchem':
        # The retrieval-time hint about missing atlas coverage is resolved now.
        result.pop('next_step', None)
        result['warnings'] = [w for w in result.get('warnings', []) if not w.startswith('No ICEBERG Atlas entry')]
    result['review'] = {'proposed': len(proposals), 'proposal_source': getattr(args, 'proposals', None) or 'smiles-json',
                        'formula_valid': len(valid),
                        'formula_rejected': rejected, 'atlas_reused': sum(c['source'].startswith('public') for c in added),
                        'simulated': sum(c['source'] in ('GLACIER', 'ICEBERG') for c in added),
                        'model_settings': options}
    from msms_structure_elucidation.worker import sort_candidates
    result['candidates'] = sort_candidates(existing + added)
    result_path.write_text(json.dumps(result, indent=2, allow_nan=False))
    write_report(result, result_path.parent / 'report.html')
    print(json.dumps(result['review'], indent=2))
    return 0 if valid else 2


def convert_mgf_command(args):
    from msms_structure_elucidation.mgf import convert_mgf
    manifest = convert_mgf(Path(args.input).expanduser().resolve(), Path(args.output_dir).expanduser().resolve(),
        args.collision_unit, args.energy, Path(args.raw_mzxml).expanduser().resolve() if args.raw_mzxml else None,
        args.instrument)
    print(json.dumps({'output_dir': str(Path(args.output_dir).resolve()), 'ready': sum(r['status'] == 'ready' for r in manifest),
        'needs_energy': sum(r['status'] == 'needs_energy' for r in manifest)}, indent=2))
    return 0


def _batch_feature(key, row, context: dict) -> dict:
    """Run one manifest feature; executes in a batch worker process."""
    args, out, cache = context['args'], context['out'], context['cache']
    feature_counts, formula_list_hash = context['feature_counts'], context['formula_list_hash']
    config_hash, asset_versions = context['config_hash'], context['asset_versions']
    feature, path = key
    name = ''.join(c if c.isalnum() or c in '._-' else '_' for c in feature)
    if feature_counts[feature] > 1 or name != feature:
        name += '__' + hashlib.sha256((feature + path).encode()).hexdigest()[:8]
    target = out / name
    try:
        old = json.loads((target / 'batch_state.json').read_text()) if (target / 'batch_state.json').exists() else {}
    except (OSError, ValueError):
        old = {}
    formula = row.get('formula') or None
    unit = row.get('collision_unit') or args.collision_unit
    if not unit:
        return {'feature_id': feature, 'status': 'needs_energy_unit', 'output_dir': str(target)}
    digest = hashlib.sha256()
    try:
        with Path(path).open('rb') as file:
            for block in iter(lambda: file.read(1024 * 1024), b''):
                digest.update(block)
    except OSError as exc:
        return {'feature_id': feature, 'status': 'error', 'output_dir': str(target), 'error': str(exc)}
    file_hash = digest.hexdigest()
    signature = hashlib.sha256(json.dumps([file_hash, unit, formula,
        formula_list_hash, config_hash, asset_versions, args.model, args.ms_pred_dir,
        args.instrument, args.cuda_devices, args.top_k, args.atlas_url], sort_keys=True).encode()).hexdigest()
    if old.get('signature') == signature and (target / 'retrieval.json').is_file():
        previous = json.loads((target / 'retrieval.json').read_text())
        if previous.get('status') in ('ranked', 'needs_formula', 'no_atlas_coverage', 'no_candidates'):
            return {'feature_id': feature, 'status': previous['status'], 'output_dir': str(target), 'resumed': True}
    opts = argparse.Namespace(input=path, collision_unit=unit, output_dir=str(target), formula=formula,
        formulas_file=args.formulas_file, ms_pred_python=args.ms_pred_python, ms_pred_dir=args.ms_pred_dir,
        config=args.config, instrument=row.get('instrument') or args.instrument,
        cuda_devices=args.cuda_devices, model=args.model, checkpoint=args.checkpoint,
        gen_checkpoint=args.gen_checkpoint, inten_checkpoint=args.inten_checkpoint,
        model_batch_size=args.model_batch_size, model_shard_size=args.model_shard_size,
        model_cpu_workers=args.model_cpu_workers, model_gpu_workers=args.model_gpu_workers,
        atlas_mgf=None, atlas_cache_dir=str(cache), atlas_url=args.atlas_url,
        top_k=args.top_k, no_report=args.no_report)
    try:
        run(opts)
        status = json.loads((target / 'retrieval.json').read_text())['status']
    except Exception as exc:
        status = 'error'
        error = str(exc)
    (target / 'batch_state.json').write_text(json.dumps({'signature': signature}))
    return {'feature_id': feature, 'status': status, 'output_dir': str(target), **({'error': error} if status == 'error' else {})}


def _batch_worker_init(model_semaphore) -> None:
    """Share one model-job limit across batch worker processes."""
    global _MODEL_SEMAPHORE
    _MODEL_SEMAPHORE = model_semaphore


def batch(args):
    config = settings(args.config)
    config_hash = config['_config_digest']
    formula_list_hash = hashlib.sha256(Path(args.formulas_file).read_bytes()).hexdigest() if args.formulas_file else None
    model_options = _model_options(args, config)
    asset_versions = {key: (model_options[key], Path(model_options[key]).stat().st_size,
        Path(model_options[key]).stat().st_mtime_ns) for key in ('checkpoint', 'gen_checkpoint', 'inten_checkpoint')
        if model_options.get(key) and Path(model_options[key]).is_file()}
    defaults = config.get('models', {}).get('batch', {})
    max_model_jobs = args.max_model_jobs or defaults.get('max_model_jobs', 1)
    min_free_gb = args.min_free_memory_gb if args.min_free_memory_gb is not None else defaults.get('min_free_memory_gb', 4)
    max_workers = args.max_workers or defaults.get('max_workers') or 'auto'
    if max_workers == 'auto':
        # Several features at once keep the GPU busy while others run formula, atlas and scoring steps.
        max_workers = hostprobe.batch_workers(hostprobe.cpu_threads(), hostprobe.ram_gb()[0],
                                              max_model_jobs, min_free_gb)
    if min(max_workers, max_model_jobs) < 1:
        raise ValueError('Batch worker counts must be positive')
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError('Batch memory checks require the batch extra: pip install .[batch]') from exc
    out = Path(args.output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = out / 'atlas_cache'
    with Path(args.manifest).open(newline='') as file:
        rows = list(csv.DictReader(file))
    jobs = {}
    summary = []
    for row in rows:
        if row.get('status') and row['status'] != 'ready':
            summary.append({'feature_id': row.get('feature_id', ''), 'status': row['status'], 'output_dir': ''})
            continue
        path = row.get('ms_path') or row.get('input')
        if not path:
            continue
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(args.manifest).expanduser().resolve().parent / candidate
        key = (row.get('feature_id') or candidate.stem, str(candidate.resolve()))
        jobs[key] = row
    feature_counts = Counter(feature for feature, _ in jobs)
    context = {'args': args, 'out': out, 'cache': cache, 'feature_counts': feature_counts,
               'formula_list_hash': formula_list_hash, 'config_hash': config_hash, 'asset_versions': asset_versions}
    # Processes, not threads: formula inference and atlas scoring are CPU-bound Python, and MSBuddy keeps
    # global state. Each worker loads ms-pred once and serves many features; spawn avoids forking threads.
    spawn = multiprocessing.get_context('spawn')
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers, mp_context=spawn,
            initializer=_batch_worker_init, initargs=(spawn.BoundedSemaphore(max_model_jobs),)) as pool:
        pending = iter(jobs.items())
        active = {}
        exhausted = False
        while active or not exhausted:
            while len(active) < max_workers and not exhausted:
                available_gb = psutil.virtual_memory().available / 1024**3
                if available_gb - hostprobe.FEATURE_RAM_GB * len(active) < min_free_gb:
                    if not active:
                        for key, _ in pending:
                            summary.append({'feature_id': key[0], 'status': 'deferred_memory', 'output_dir': ''})
                        exhausted = True
                    break
                try:
                    key, row = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                active[pool.submit(_batch_feature, key, row, context)] = key
            if active:
                done = next(concurrent.futures.as_completed(active))
                try:
                    summary.append(done.result())
                except Exception as exc:
                    summary.append({'feature_id': active[done][0], 'status': 'error',
                        'output_dir': '', 'error': str(exc)})
                del active[done]
    (out / 'batch_summary.json').write_text(json.dumps(summary, indent=2))
    with (out / 'batch_summary.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['feature_id', 'status', 'output_dir', 'resumed', 'error'])
        writer.writeheader(); writer.writerows(summary)
    print(json.dumps({'features': len(summary), 'max_workers': max_workers, 'status_counts': {s: sum(r['status'] == s for r in summary) for s in {r['status'] for r in summary}},
        'summary': str(out / 'batch_summary.csv')}, indent=2))
    return 1 if any(r['status'] == 'error' for r in summary) else 0


def denovo(args):
    """Run subformula assignment and the FRIGID adapter with explicit assets."""
    result_path = Path(args.result).resolve()
    result = json.loads(result_path.read_text())
    formula = result.get('formula')
    if not formula:
        raise ValueError('FRIGID requires a formula; rerun retrieval with --formula')
    repo = Path(__file__).resolve().parents[2]
    model_py = model_python(args.ms_pred_python)
    frigid_py = Path(args.frigid_python).resolve()
    frigid_dir = Path(args.frigid_dir).resolve()
    for path in (frigid_py, frigid_dir / 'scripts/spec2mol_scaling.py',
                 Path(args.mist_ckpt), Path(args.dlm_ckpt)):
        if not path.is_file():
            raise FileNotFoundError(f'Missing FRIGID asset: {path}. Run its setup script or supply a valid path.')
    if args.num_rounds:
        for path in (args.iceberg_gen_ckpt, args.iceberg_inten_ckpt):
            if not path or not Path(path).is_file():
                raise FileNotFoundError('ICEBERG refinement requires both checkpoint files')
    subform = result_path.parent / 'subformulae'
    command = [model_py, str(repo / '.agents/skills/msms-subformulae/scripts/run.py'),
        '--spectrum', result['input'], '--formula', formula,
        '--adduct', result['adduct'], '--output-dir', str(subform)]
    subprocess.run(command, cwd=repo, check=True)
    output = result_path.parent / 'denovo.json'
    command = [str(frigid_py), str(repo / '.agents/skills/msms-denovo/scripts/run.py'),
        '--spectrum', result['input'], '--formula', formula,
        '--adduct', result['adduct'], '--subform-dir', str(subform),
        '--frigid-dir', str(frigid_dir), '--frigid-python', str(frigid_py),
        '--mist-ckpt', str(Path(args.mist_ckpt).resolve()),
        '--dlm-ckpt', str(Path(args.dlm_ckpt).resolve()),
        '--num-rounds', str(args.num_rounds), '--top-k', str(args.top_k),
        '--output', str(output)]
    if args.iceberg_gen_ckpt:
        command += ['--iceberg-gen-ckpt', str(Path(args.iceberg_gen_ckpt).resolve())]
    if args.iceberg_inten_ckpt:
        command += ['--iceberg-inten-ckpt', str(Path(args.iceberg_inten_ckpt).resolve())]
    subprocess.run(command, cwd=repo, check=True)
    generated = json.loads(output.read_text())
    result['denovo_candidates'] = generated['candidates']
    result['denovo_source'] = 'FRIGID'
    result_path.write_text(json.dumps(result, indent=2, allow_nan=False))
    write_report(result, result_path.parent / 'report.html')
    print(json.dumps({'denovo_candidates': len(generated['candidates']), 'output': str(output)}, indent=2))
    return 0


def visualize(args):
    """Open the separate, localhost-only fragment review skill."""
    python = model_python(args.ms_pred_python)
    command = [python, '-m', 'msms_structure_elucidation.visualize',
               '--result', *[str(Path(r).expanduser().resolve())
                             for r in ([args.result] if isinstance(args.result, str) else args.result)],
               '--port', str(args.port)]
    if args.atlas_mgf:
        command.extend(['--atlas-mgf', str(Path(args.atlas_mgf).expanduser().resolve())])
    if getattr(args, 'export', None):
        command.extend(['--export', str(Path(args.export).expanduser().resolve())])
        for flag in ('split_data', 'demo_reviews'):
            if getattr(args, flag, False):
                command.append('--' + flag.replace('_', '-'))
        if getattr(args, 'title', None):
            command.extend(['--title', args.title])
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    try:
        return subprocess.run(command, env=env).returncode
    except KeyboardInterrupt:
        return 0


def setup_command(args):
    from msms_structure_elucidation.hostsetup import setup
    config = {} if args.remote else settings(None)
    options = _model_options(args, config) if config else {}
    report = setup(args, options, model_python, config.get('models', {}).get('batch', {}))
    if args.json:
        print('SETUP_JSON=' + json.dumps(report, allow_nan=False))
    else:
        print(json.dumps(report, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='msms-structure-elucidation')
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('run', help='rank public atlas candidates for a .ms spectrum')
    command.add_argument('--input', required=True)
    command.add_argument('--collision-unit', required=True, choices=['NCE', 'eV'],
                         help='user-confirmed unit of all experimental collision labels')
    command.add_argument('--output-dir', required=True)
    command.add_argument('--formula', help='neutral molecular formula; otherwise inferred with MSBuddy')
    command.add_argument('--formulas-file', help='one neutral formula per line; all are searched')
    command.add_argument('--ms-pred-python', help='Python interpreter with ms_pred, msbuddy, RDKit')
    command.add_argument('--ms-pred-dir', help='ms-pred source checkout used as model working directory')
    command.add_argument('--config', help='shared YAML settings (default configs/default.yaml)')
    command.add_argument('--instrument', help='model instrument; otherwise .ms >instrumentation, then config')
    command.add_argument('--cuda-devices', help='GPU IDs, for example 0; or MSMS_CUDA_DEVICES')
    command.add_argument('--model', choices=['iceberg'], default=None, help='exact-energy atlas fallback')
    command.add_argument('--checkpoint')
    command.add_argument('--gen-checkpoint')
    command.add_argument('--inten-checkpoint')
    command.add_argument('--model-batch-size', type=int)
    command.add_argument('--model-cpu-workers', type=int, help='ms-pred num_cpu_workers')
    command.add_argument('--model-gpu-workers', type=int, help='ms-pred num_gpu_workers (ICEBERG model copies per job)')
    command.add_argument('--model-shard-size', type=int)
    command.add_argument('--atlas-mgf', help='local formula MGF for offline use')
    command.add_argument('--atlas-cache-dir', help='shared atlas cache, useful in batch runs')
    command.add_argument('--atlas-url')
    command.add_argument('--top-k', type=int)
    command.add_argument('--no-report', action='store_true')
    command.add_argument('--ms1-ppm', type=float,
                         help='precursor tolerance for MSBuddy and the PubChem fallback '
                              '(default from the instrument: Q-TOF 10, Orbitrap 5 ppm)')
    command.add_argument('--ms2-ppm', type=float,
                         help='fragment tolerance for MSBuddy (default: Q-TOF 20, Orbitrap 10 ppm)')
    command.add_argument('--formula-elements', default='CHNOPS',
                         help='elements allowed in PubChem fallback formulas (default CHNOPS)')
    command.add_argument('--no-pubchem', action='store_true',
                         help='do not search PubChem by mass when MSBuddy proposes no formula')
    reviewer = sub.add_parser('review', help='validate candidate SMILES and rerank via atlas or a local model')
    reviewer.add_argument('--result', required=True, help='retrieval.json from run')
    proposal_source = reviewer.add_mutually_exclusive_group(required=True)
    proposal_source.add_argument('--smiles-json', help='JSON array of proposed SMILES')
    proposal_source.add_argument('--proposals', choices=['pubchem'],
                                 help='predict PubChem structures of the formula (formula not in the ICEBERG Atlas)')
    reviewer.add_argument('--max-structures', type=int, default=500,
                          help='cap on --proposals structures (default 500)')
    reviewer.add_argument('--ms-pred-python')
    reviewer.add_argument('--ms-pred-dir')
    reviewer.add_argument('--config')
    reviewer.add_argument('--formula', help='target formula if result contains several hypotheses')
    reviewer.add_argument('--instrument')
    reviewer.add_argument('--cuda-devices')
    reviewer.add_argument('--model-batch-size', type=int)
    reviewer.add_argument('--model-cpu-workers', type=int, help='ms-pred num_cpu_workers')
    reviewer.add_argument('--model-gpu-workers', type=int, help='ms-pred num_gpu_workers (ICEBERG model copies per job)')
    reviewer.add_argument('--model', choices=['glacier', 'iceberg'], default='glacier')
    reviewer.add_argument('--checkpoint', help='GLACIER checkpoint')
    reviewer.add_argument('--gen-checkpoint', help='ICEBERG generation checkpoint')
    reviewer.add_argument('--inten-checkpoint', help='ICEBERG intensity checkpoint')
    converter = sub.add_parser('convert-mgf', help='convert GNPS/MZmine MS2 entries to .ms and a batch manifest')
    converter.add_argument('--input', required=True)
    converter.add_argument('--output-dir', required=True)
    converter.add_argument('--collision-unit', required=True, choices=['NCE', 'eV'])
    converter.add_argument('--energy', type=float, help='confirmed energy override for entries without MS2 energy')
    converter.add_argument('--raw-mzxml', help='read collisionEnergy from referenced MS2 scans')
    converter.add_argument('--instrument')
    batcher = sub.add_parser('batch', help='run a feature manifest with shared atlas cache and bounded model jobs')
    batcher.add_argument('--manifest', required=True)
    batcher.add_argument('--output-dir', required=True)
    batcher.add_argument('--collision-unit', choices=['NCE', 'eV'], help='fallback when manifest has no unit')
    batcher.add_argument('--formulas-file')
    batcher.add_argument('--ms-pred-python')
    batcher.add_argument('--ms-pred-dir')
    batcher.add_argument('--config')
    batcher.add_argument('--instrument')
    batcher.add_argument('--cuda-devices')
    batcher.add_argument('--model', choices=['iceberg'])
    batcher.add_argument('--checkpoint')
    batcher.add_argument('--gen-checkpoint')
    batcher.add_argument('--inten-checkpoint')
    batcher.add_argument('--model-batch-size', type=int)
    batcher.add_argument('--model-cpu-workers', type=int, help='ms-pred num_cpu_workers')
    batcher.add_argument('--model-gpu-workers', type=int, help='ms-pred num_gpu_workers (ICEBERG model copies per job)')
    batcher.add_argument('--model-shard-size', type=int)
    batcher.add_argument('--atlas-url')
    batcher.add_argument('--top-k', type=int)
    batcher.add_argument('--max-workers', type=int,
                         help='features processed at once (default: models.batch.max_workers, or auto from CPU threads and RAM)')
    batcher.add_argument('--max-model-jobs', type=int)
    batcher.add_argument('--min-free-memory-gb', type=float)
    batcher.add_argument('--no-report', action='store_true')
    generator = sub.add_parser('denovo', help='run optional FRIGID fallback for a retrieval result')
    generator.add_argument('--result', required=True)
    generator.add_argument('--ms-pred-python')
    generator.add_argument('--frigid-dir', required=True)
    generator.add_argument('--frigid-python', required=True)
    generator.add_argument('--mist-ckpt', required=True)
    generator.add_argument('--dlm-ckpt', required=True)
    generator.add_argument('--iceberg-gen-ckpt')
    generator.add_argument('--iceberg-inten-ckpt')
    generator.add_argument('--num-rounds', type=int, default=0)
    generator.add_argument('--top-k', type=int, default=10)
    installer = sub.add_parser('setup', help='probe this or a remote host, tune model inference, save configs/local.yaml')
    installer.add_argument('--ms-pred-python', help='interpreter with ms_pred for the benchmark')
    installer.add_argument('--ms-pred-dir', help='ms-pred checkout; saved to configs/local.yaml')
    installer.add_argument('--gen-checkpoint', help='ICEBERG generator; saved to configs/local.yaml')
    installer.add_argument('--inten-checkpoint', help='ICEBERG intensity model; saved to configs/local.yaml')
    installer.add_argument('--no-benchmark', action='store_true', help='save heuristic settings without timing ICEBERG')
    installer.add_argument('--json', action='store_true', help='print one SETUP_JSON= line')
    installer.add_argument('--remote', help='ssh destination (user@host or ~/.ssh/config alias) that runs the models')
    installer.add_argument('--remote-repo', help='this repository checked out on the remote host')
    installer.add_argument('--remote-python', help='remote interpreter with this package installed (default python3)')
    installer.add_argument('--remote-prefix', help='shell text run before remote commands, for example '
                           '"source ~/miniforge3/bin/activate ms-pred &&" or "srun --gres=gpu:1"')
    installer.add_argument('--ssh-option', action='append', help='extra ssh -o option; repeatable')
    viewer = sub.add_parser('visualize', help='open a local interactive fragment review webpage')
    viewer.add_argument('--result', required=True, nargs='+',
                        help='retrieval.json produced by run or review; pass several to review all unknowns on one page')
    viewer.add_argument('--atlas-mgf', help='atlas MGF for legacy results without saved fragment IDs')
    viewer.add_argument('--ms-pred-python', help='Python with ms_pred, RDKit, and Flask')
    viewer.add_argument('--port', type=int, default=0, help='localhost port; 0 chooses a free port')
    viewer.add_argument('--export', help='write one self-contained read-only review page (HTML) and exit')
    viewer.add_argument('--split-data', action='store_true',
                        help='with --export: put the data in <name>.data.json.gz beside the page (for hosts with size limits)')
    viewer.add_argument('--demo-reviews', action='store_true',
                        help='with --export: let viewers try the review controls; notes stay in their browser')
    viewer.add_argument('--title', help='with --export: page title')
    args = parser.parse_args(argv)
    try:
        return {'run': run, 'review': review, 'denovo': denovo,
                'visualize': visualize, 'convert-mgf': convert_mgf_command,
                'batch': batch, 'setup': setup_command}[args.command](args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Error: {exc}\n')

if __name__ == '__main__':
    sys.exit(main())
