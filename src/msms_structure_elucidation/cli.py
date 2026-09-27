"""Portable MS/MS elucidation CLI. Scientific work is delegated to ms-pred Python."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from msms_structure_elucidation.atlas import download_mgf
from msms_structure_elucidation.report import write_report
from msms_structure_elucidation.spectrum import inspect_ms, mass_tolerance


def model_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get('MS_PRED_PYTHON'):
        return os.environ['MS_PRED_PYTHON']
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
    cmd = [python, '-m', 'msms_structure_elucidation.worker', action]
    for key, value in kwargs.items():
        if value is not None:
            cmd += ['--' + key.replace('_', '-'), str(value)]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    run = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if run.returncode:
        raise RuntimeError(run.stderr[-4000:] or run.stdout[-4000:])
    for line in reversed(run.stdout.splitlines()):
        if line.startswith('RESULT_JSON='):
            return json.loads(line.removeprefix('RESULT_JSON='))
    raise RuntimeError(f'No worker result: {run.stdout[-2000:]}')


def run(args):
    path = Path(args.input).expanduser().resolve()
    if path.suffix.lower() != '.ms':
        raise ValueError('Use a .ms spectrum. For raw or mzML input, run the preprocess/feature-detect skills first.')
    data = inspect_ms(path, args.collision_unit)
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = {'input': str(path), 'parentmass': data['parentmass'], 'adduct': data['adduct'],
              'peaks': data['peaks'], 'spectra': data['spectra'], 'candidates': [], 'warnings': [],
              'collision_unit': data['collision_unit'], 'header_units': data['header_units'],
              'energy_mapping': data['energy_mapping']}
    if data['header_units'] and data['header_units'] != [args.collision_unit]:
        result['warnings'].append(
            f"Collision headers say {', '.join(data['header_units'])}; using user-supplied {args.collision_unit}.")
    tolerance = mass_tolerance(data['metadata'].get('instrumentation'))
    overrides = {k: getattr(args, k, None) for k in ('ms1_ppm', 'ms2_ppm')}
    tolerance.update({k: v for k, v in overrides.items() if v is not None})
    tolerance['source'] = 'user' if any(v is not None for v in overrides.values()) else 'instrument default'
    result['mass_tolerance'] = tolerance
    if tolerance['instrument'] == 'unknown' and tolerance['source'] != 'user':
        result['warnings'].append(
            f"Instrument not recognised in the .ms header; using Q-TOF tolerances ({tolerance['ms1_ppm']:g}/"
            f"{tolerance['ms2_ppm']:g} ppm). Pass --ms1-ppm/--ms2-ppm (Orbitrap: 5/10).")
    python = model_python(args.ms_pred_python)
    formula = args.formula or data['metadata'].get('formula')
    if not formula:
        try:
            inferred = worker(python, 'formula', spectrum=path, experimental_unit=args.collision_unit,
                              ms1_ppm=tolerance['ms1_ppm'], ms2_ppm=tolerance['ms2_ppm'])
            if inferred:
                formula = inferred[0]['formula']
                result['formula_hypotheses'] = inferred
                result['formula_source'] = 'MSBuddy inferred'
        except Exception as exc:
            result['warnings'].append(str(exc))
        if not formula and not args.no_pubchem:
            formula = pubchem_formula(result, data, args)
    else:
        result['formula_source'] = 'provided' if args.formula else 'input'
    result['formula'] = formula
    if formula:
        try:
            mgf = Path(args.atlas_mgf) if args.atlas_mgf else download_mgf(formula, data['adduct'], out / 'atlas' / f'{formula}.mgf', args.atlas_url)
        except ValueError:
            mgf = None
            result['library_structures'] = 0
            result['next_step'] = 'iceberg-pubchem'
            result['warnings'].append(
                f'{formula} has no ICEBERG Atlas entry. Predict its PubChem structures with ICEBERG: '
                f'review --result {out / "retrieval.json"} --proposals pubchem --model iceberg.')
        except Exception as exc:
            mgf = None
            result['next_step'] = 'retry-retrieval'
            result['warnings'].append(f'Atlas retrieval failed: {str(exc).strip().splitlines()[-1][:300]}')
    if formula and mgf is not None:
        try:
            ranked = worker(python, 'rank', spectrum=path, mgf=mgf, formula=formula,
                            top_k=args.top_k, experimental_unit=args.collision_unit)
            result.update(ranked)
            result['atlas_mgf'] = str(mgf)
            scores = [c['entropy_similarity'] for c in result['candidates']]
            result['qc'] = {'experimental_collision_energies': len(data['spectra']),
                'experimental_peaks': data['peaks'], 'library_structures': ranked['library_structures'],
                'scored_structures': ranked['scored_structures'],
                'nonzero_top_scores': sum(score > 0 for score in scores),
                'top_score_range': [min(scores), max(scores)] if scores else None,
                'top_energy_pairs': len(result['candidates'][0]['energy_alignment']) if scores else 0,
                'top_predicted_peaks': sum(len(v) for v in result['candidates'][0]['predicted_spectra'].values()) if scores else 0}
            if result['candidates']:
                best = result['candidates'][0]
                aligned = {p['experimental_key'] for p in best['energy_alignment']}
                unmatched_energies = [e for e in data['spectra'] if e not in aligned]
                result['qc']['unmatched_experimental_energies_ev'] = unmatched_energies
                if unmatched_energies:
                    result['warnings'].append(
                        f"{len(unmatched_energies)} experimental collision energies had no atlas spectrum within 2 eV: {', '.join(unmatched_energies)} eV.")
                matched = {(str(p['ce']), p['mz']) for p in best['matched_peaks']}
                unexplained = []
                for ce, peaks in data['spectra'].items():
                    for mz, intensity in peaks:
                        if (str(ce), mz) not in matched:
                            unexplained.append({'ce': ce, 'mz': mz, 'intensity': intensity})
                result['review_evidence'] = {'best_candidate': best['smiles'],
                    'entropy_similarity': best['entropy_similarity'],
                    'explained_intensity': best['explained_intensity'],
                    'matched_peak_count': len(best['matched_peaks']),
                    'total_peak_count': data['peaks'],
                    'matched_peaks': best['matched_peaks'],
                    'strongest_unexplained_peaks': sorted(unexplained,
                        key=lambda p: p['intensity'], reverse=True)[:20],
                    'weak_match_review_suggested': best['entropy_similarity'] < 0.5 or best['explained_intensity'] < 0.5 or len(best['matched_peaks']) < 10}
            if not result['candidates']:
                result['next_step'] = 'iceberg-atlas'
                result['warnings'].append(
                    f'No comparable atlas candidates: none of the {ranked["library_structures"]} atlas structures has a '
                    f'collision energy within 2 eV of the experiment. Predict them at the experimental energy with ICEBERG: '
                    f'review --result {out / "retrieval.json"} --proposals atlas --model iceberg.')
        except Exception as exc:
            result['next_step'] = 'retry-retrieval'
            result['warnings'].append(f'Atlas retrieval failed: {str(exc).strip().splitlines()[-1][:300]}')
    elif not formula and 'next_step' not in result:
        result['warnings'].append('No formula available; provide --formula to query the formula-indexed public atlas.')
    (out / 'retrieval.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    if not args.no_report:
        write_report(result, out / 'report.html')
    print(json.dumps({'output_dir': str(out), 'formula': formula,
        'candidates': len(result['candidates']), 'warnings': result['warnings']}, indent=2))
    return 0 if result['candidates'] else 2


def pubchem_formula(result: dict, data: dict, args) -> str | None:
    """When MSBuddy proposes nothing, take formulas of PubChem structures matching the precursor mass."""
    from msms_structure_elucidation import pubchem
    try:
        ppm = result['mass_tolerance']['ms1_ppm']
        found = pubchem.formulas_by_mass(data['parentmass'], data['adduct'], ppm, args.formula_elements)
    except Exception as exc:
        result['warnings'].append(f'PubChem mass search failed: {exc}')
        return None
    if not found:
        result['next_step'] = 'review-frigid'
        result['warnings'].append(
            f'No {args.formula_elements} formula of a PubChem structure lies within ±{ppm:g} ppm of the precursor. '
            'Review the spectrum (adduct, in-source fragment, isotope, noise) and decide whether to generate '
            'structures de novo with FRIGID (msms-denovo).')
        return None
    result['formula_hypotheses'] = found
    result['formula_source'] = found[0]['source']
    result['warnings'].append(
        f'MSBuddy proposed no formula; using PubChem mass matches ({found[0]["formula"]}, '
        f'{found[0]["structures"]} structures, {found[0]["ppm"]:+.1f} ppm).')
    return found[0]['formula']


def proposal_file(args, result: dict, result_path: Path, formula: str) -> Path:
    """SMILES to review: a user/agent JSON list, every atlas structure, or PubChem structures."""
    if args.smiles_json:
        return Path(args.smiles_json).resolve()
    if args.proposals == 'atlas':
        mgf = result.get('atlas_mgf')
        if not mgf or not Path(mgf).is_file():
            raise ValueError('No cached atlas MGF in this result; use --proposals pubchem or --smiles-json')
        smiles = []
        with open(mgf) as handle:
            for line in handle:
                if line.upper().startswith('SMILES='):
                    value = line.split('=', 1)[1].strip()
                    if value not in smiles:
                        smiles.append(value)
        smiles = smiles[:args.max_structures]
    else:
        from msms_structure_elucidation import pubchem
        smiles = pubchem.structures_for_formula(formula, args.max_structures)
    path = result_path.with_name(f'proposals_{args.proposals}.json')
    path.write_text(json.dumps(smiles, indent=0))
    return path


def review(args):
    """Validate agent-proposed SMILES, reuse atlas matches, simulate absent structures."""
    result_path = Path(args.result).resolve()
    result = json.loads(result_path.read_text())
    formula = result.get('formula')
    if not formula:
        raise ValueError('Review needs a molecular formula; rerun retrieval with --formula')
    smiles_json = proposal_file(args, result, result_path, formula)
    proposals = json.loads(smiles_json.read_text())
    if not isinstance(proposals, list) or not all(isinstance(x, str) for x in proposals):
        raise ValueError('--smiles-json must contain a JSON array of SMILES strings')
    if not proposals:
        raise ValueError(f'No {args.proposals or "proposed"} structures found for {formula}')
    for name in ('checkpoint', 'gen_checkpoint', 'inten_checkpoint'):
        if getattr(args, name, None):
            setattr(args, name, str(Path(getattr(args, name)).expanduser().resolve()))
    python = model_python(args.ms_pred_python)
    validated = worker(python, 'validate', smiles_json=smiles_json, formula=formula)
    valid = [c['smiles'] for c in validated if c['formula_match']]
    rejected = [c for c in validated if not c['formula_match']]
    if not valid:
        raise ValueError('No proposed SMILES matched the target formula')
    existing = list(result.get('candidates', []))
    if result.get('atlas_mgf') and Path(result['atlas_mgf']).exists():
        atlas = worker(python, 'rank', spectrum=result['input'], mgf=result['atlas_mgf'],
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
        temp = result_path.parent / 'review_smiles.json'
        temp.write_text(json.dumps(missing))
        try:
            added.extend(worker(python, 'simulate', spectrum=result['input'],
                smiles_json=temp, formula=formula, model=args.model,
                checkpoint=args.checkpoint, gen_checkpoint=args.gen_checkpoint,
                inten_checkpoint=args.inten_checkpoint, output_dir=result_path.parent,
                experimental_unit=result['collision_unit']))
        except Exception as exc:
            result.setdefault('warnings', []).append(f'Local {args.model} simulation unavailable: {exc}')
    if added:
        # The retrieval-time hints about missing candidates are resolved now.
        result.pop('next_step', None)
        result['warnings'] = [w for w in result.get('warnings', [])
                              if not (w.startswith('No comparable atlas candidates') or 'has no ICEBERG Atlas entry' in w)]
    result['review'] = {'proposed': len(proposals), 'proposal_source': args.proposals or 'smiles-json',
                        'formula_valid': len(valid),
                        'formula_rejected': rejected, 'atlas_reused': sum(c['source'].startswith('public') for c in added),
                        'simulated': sum(c['source'] in ('GLACIER', 'ICEBERG') for c in added)}
    result['candidates'] = sorted(existing + added,
        key=lambda c: (c['entropy_similarity'], c['explained_intensity']), reverse=True)
    result_path.write_text(json.dumps(result, indent=2, allow_nan=False))
    write_report(result, result_path.parent / 'report.html')
    print(json.dumps(result['review'], indent=2))
    return 0 if valid else 2


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog='msms-structure-elucidation')
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('run', help='rank public atlas candidates for a .ms spectrum')
    command.add_argument('--input', required=True)
    command.add_argument('--collision-unit', required=True, choices=['NCE', 'eV'],
                         help='user-confirmed unit of all experimental collision labels')
    command.add_argument('--output-dir', required=True)
    command.add_argument('--formula', help='neutral molecular formula; otherwise inferred with MSBuddy')
    command.add_argument('--ms-pred-python', help='Python interpreter with ms_pred, msbuddy, RDKit')
    command.add_argument('--atlas-mgf', help='local formula MGF for offline use')
    command.add_argument('--atlas-url', default='https://iceberg-ms.mit.edu')
    command.add_argument('--top-k', type=int, default=10)
    command.add_argument('--no-report', action='store_true')
    command.add_argument('--ms1-ppm', type=float,
                         help='precursor tolerance for MSBuddy and the PubChem fallback '
                              '(default from the .ms instrument: Q-TOF 10, Orbitrap 5 ppm)')
    command.add_argument('--ms2-ppm', type=float,
                         help='fragment tolerance for MSBuddy (default: Q-TOF 20, Orbitrap 10 ppm)')
    command.add_argument('--formula-elements', default='CHNOPS',
                         help='elements allowed in PubChem fallback formulas (default CHNOPS)')
    command.add_argument('--no-pubchem', action='store_true',
                         help='do not search PubChem by mass when MSBuddy proposes no formula')
    reviewer = sub.add_parser('review', help='validate candidate SMILES and rerank via atlas or a local model')
    reviewer.add_argument('--result', required=True, help='retrieval.json from run')
    proposals = reviewer.add_mutually_exclusive_group(required=True)
    proposals.add_argument('--smiles-json', help='JSON array of proposed SMILES')
    proposals.add_argument('--proposals', choices=['atlas', 'pubchem'],
                           help='review every atlas structure of the formula (no atlas energy within 2 eV) '
                                'or PubChem structures of the formula (formula not in the atlas)')
    reviewer.add_argument('--max-structures', type=int, default=500,
                          help='cap on --proposals structures (default 500)')
    reviewer.add_argument('--ms-pred-python')
    reviewer.add_argument('--model', choices=['glacier', 'iceberg'], default='glacier')
    reviewer.add_argument('--checkpoint', help='GLACIER checkpoint')
    reviewer.add_argument('--gen-checkpoint', help='ICEBERG generation checkpoint')
    reviewer.add_argument('--inten-checkpoint', help='ICEBERG intensity checkpoint')
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
                'visualize': visualize}[args.command](args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Error: {exc}\n')

if __name__ == '__main__':
    sys.exit(main())
