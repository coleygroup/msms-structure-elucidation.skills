"""Scientific operations run in a Python environment with ms-pred installed."""
from __future__ import annotations

import argparse
import base64
import bisect
import json
import math
import sys
import threading
from collections import defaultdict
from pathlib import Path

_MSBUDDY_LOCK = threading.Lock()


def _serial_spec(composite):
    return {ce: [[float(m), float(i)] for m, i in zip(spec.masses, spec.intens)]
            for ce, spec in composite.items()}


def _serial_fragment_ids(composite, atlas_fragments=None):
    """Keep peak-aligned fragment bitmasks as decimal strings for the viewer."""
    result = {}
    for ce, spec in composite.items():
        if atlas_fragments is not None:
            record = atlas_fragments.get(ce)
            if record is None:
                continue
            source_peaks, ids = record
            if len(source_peaks) != len(spec.masses) or any(
                    abs(float(row[0])-float(mz)) > 1e-5 or
                    abs(float(row[1])-float(intensity)) > 1e-6
                    for row, mz, intensity in zip(source_peaks, spec.masses, spec.intens)):
                continue
        else:
            ids = spec.int_frags
        if ids is not None and len(ids) == len(spec.masses):
            result[ce] = [str(int(value)) for value in ids]
    return result


def _structure_image(smiles: str) -> str | None:
    """Return an embeddable RDKit SVG molecule drawing when possible."""
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    drawer = rdMolDraw2D.MolDraw2DSVG(280, 170)
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return 'data:image/svg+xml;base64,' + base64.b64encode(drawer.GetDrawingText().encode()).decode()


def _experimental_spectra(spectrum: str, unit: str):
    """Keep spectra intact while making their energy labels explicit in eV."""
    from ms_pred import common
    if unit not in ('NCE', 'eV'):
        raise ValueError('Supply --experimental-unit as NCE or eV')
    metadata, spectra = common.parse_spectra(spectrum)
    precursor = float(metadata['parentmass'])
    mapped = {}
    source = {}
    for key, spec in spectra.items():
        raw = float(key)
        ev = common.nce_to_ev(raw, precursor) if unit == 'NCE' else raw
        if not math.isfinite(float(ev)) or int(round(float(ev))) <= 0:
            raise ValueError(f'Invalid experimental collision energy: {raw} {unit}')
        ev_key = str(round(float(ev), 8))
        if ev_key in mapped:
            raise ValueError(f'Experimental energy labels collapse to {ev_key} eV')
        mapped[ev_key] = spec
        source[ev_key] = raw
    return metadata, mapped, source


def align_energies(experimental: dict, predicted: dict, source: dict,
                   unit: str) -> list[dict]:
    """Match only equal integer eV labels after rounding both sides."""
    matched = []
    indexed = {int(round(float(key))): key for key in predicted.keys()}
    for exp_key in sorted(experimental.keys(), key=float):
        ev = float(exp_key)
        model_ev = int(round(ev))
        if model_ev not in indexed:
            continue
        atlas_key = indexed[model_ev]
        atlas_ev = float(atlas_key)
        gap = abs(ev-atlas_ev)
        matched.append({'experimental_key': exp_key, 'atlas_key': atlas_key,
            'prediction_key': atlas_key, 'model_energy_ev': model_ev,
            'input_value': source[exp_key], 'input_unit': unit,
            'experimental_ev': ev, 'atlas_ev': atlas_ev, 'delta_ev': gap})
    return matched


def model_collision_energies_ev(experimental: dict) -> list[int]:
    """Pass only rounded integer eV values to forward models."""
    energies = [float(key) for key in experimental.keys()]
    if not energies or any(not math.isfinite(ev) or ev <= 0 for ev in energies):
        raise ValueError('Model collision energies must be positive finite eV values')
    rounded = sorted({int(round(ev)) for ev in energies})
    if rounded[0] <= 0:
        raise ValueError('Rounded model collision energies must be positive integers')
    return rounded


def _score(experimental, predicted, alignment, precursor):
    scores = []
    for pair in alignment:
        score = float(predicted[pair['atlas_key']].entr_sim(
            experimental[pair['experimental_key']], ignore_mass=precursor-1))
        pair['entropy_similarity'] = score
        scores.append(score)
    return sum(scores) / len(scores)


TIE_BAND = 0.03  # entropy difference treated as a tie, broken by explained intensity


def image_floor(scores: list[float], top_k: int | None) -> float:
    """Lowest entropy that can still reach a merged top_k list: sort_candidates puts every
    result below all results scoring more than TIE_BAND above it."""
    ordered = sorted(scores, reverse=True)
    return ordered[top_k - 1] - TIE_BAND if top_k and len(ordered) >= top_k else -math.inf


def sort_candidates(candidates: list[dict], tie_band: float = TIE_BAND) -> list[dict]:
    """Keep entropy primary and use coverage within narrow score bands."""
    ordered = sorted(candidates, key=lambda c: c['entropy_similarity'], reverse=True)
    result = []
    while ordered:
        anchor = ordered[0]['entropy_similarity']
        band = [c for c in ordered if anchor - c['entropy_similarity'] <= tie_band]
        ordered = ordered[len(band):]
        result.extend(sorted(band, key=lambda c: (c['explained_intensity'], c['entropy_similarity']), reverse=True))
    # Flag a candidate when an earlier one has the same formula, identical matched peaks and entropy
    # within tie_band. Every earlier candidate scores at least entropy - tie_band, so the first match is
    # the first earlier entry of that group scoring at most entropy + tie_band: a bisect on running minima.
    groups = {}
    for index, a in enumerate(result):
        a['rank'] = index + 1
        a['ambiguity'] = []
        key = (a.get('formula'), frozenset((p['ce'], round(p['mz'], 4)) for p in a.get('matched_peaks', [])))
        earlier, negated_minima = groups.setdefault(key, ([], []))
        score = a['entropy_similarity']
        # The margin keeps float rounding from skipping a match; the scan applies the exact test.
        for b in earlier[bisect.bisect_left(negated_minima, -(score + tie_band + 1e-9)):]:
            if abs(b['entropy_similarity'] - score) <= tie_band:
                a['ambiguity'].append(f"No diagnostic matched peaks versus rank {b['rank']}; "
                                      'positional isomers may be indistinguishable')
                break
        earlier.append(a)
        negated_minima.append(max(negated_minima[-1], -score) if negated_minima else -score)
    return result


def _explained(experimental, predicted, alignment, ppm=10):
    """Fraction of experimental intensity within ppm of a predicted peak, and the matched peaks."""
    import numpy as np
    matched = []
    total = 0.0
    covered = 0.0
    for pair in alignment:
        ce = pair['experimental_key']
        a, b = experimental[ce], predicted[pair['atlas_key']]
        mzs = np.asarray(a.masses, dtype=float)
        intens = np.asarray(a.intens, dtype=float)
        total += float(intens.sum())
        masses = np.sort(np.asarray(b.masses, dtype=float))
        if not len(masses) or not len(mzs):
            continue
        right = np.minimum(np.searchsorted(masses, mzs), len(masses) - 1)
        left = np.maximum(right - 1, 0)
        nearest = np.where(np.abs(masses[left] - mzs) <= np.abs(masses[right] - mzs), masses[left], masses[right])
        hit = np.abs(nearest - mzs) <= np.maximum(0.002, mzs * ppm * 1e-6)
        covered += float(intens[hit].sum())
        matched.extend({'ce': ce, 'mz': float(mz), 'predicted_mz': float(p)} for mz, p in zip(mzs[hit], nearest[hit]))
    return (covered / total if total else 0.0), matched


def rank(spectrum: str, mgf: str, formula: str, top_k: int,
         experimental_unit: str) -> dict:
    from ms_pred import common
    from ms_pred.common import CompositeMassSpec, MassSpec
    metadata, experimental, source = _experimental_spectra(spectrum, experimental_unit)
    precursor = float(metadata['parentmass'])
    expected = common.formula_mass(formula) + common.ion2mass[metadata.get('ionization', '[M+H]+')]
    if abs(expected - precursor) > max(0.01, precursor * 20e-6):
        raise ValueError(f'Formula/adduct precursor mismatch: {formula} predicts {expected:.4f}, observed {precursor:.4f}')
    grouped = defaultdict(list)
    fragment_ids = defaultdict(dict)
    info = {}
    for meta, peaks in common.parse_spectra_mgf(mgf):
        smiles = meta.get('SMILES')
        ce = meta.get('COLLISION_ENERGY')
        if not smiles or not ce or len(peaks) == 0:
            continue
        try:
            ce = float(str(ce).split()[0])
        except ValueError:
            continue
        key = meta.get('INCHIKEY') or smiles
        frag_tokens = str(meta.get('FRAGS', '')).replace(',', ' ').split()
        ce_key = f'{ce:.0f}'
        if len(frag_tokens) == len(peaks):
            try:
                fragment_ids[key][ce_key] = (peaks, [str(int(token)) for token in frag_tokens])
            except ValueError:
                pass
        grouped[key].append(MassSpec(collision_energy=ce, masses=peaks[:, 0],
                                     intens=peaks[:, 1], root_canonical_smiles=smiles))
        info[key] = meta
    results = []
    expected_energies = set(model_collision_energies_ev(experimental))
    missing_energies = set()
    errors = []
    for key, spectra in grouped.items():
        try:
            predicted = CompositeMassSpec(spectra)
            alignment = align_energies(experimental, predicted, source, experimental_unit)
            paired = {pair['model_energy_ev'] for pair in alignment}
            missing_energies.update(expected_energies - paired)
            if len(alignment) != len(experimental):
                continue
            similarity = _score(experimental, predicted, alignment, precursor)
            if not math.isfinite(float(similarity)):
                continue
            explained, matched = _explained(experimental, predicted, alignment)
            results.append({'smiles': info[key]['SMILES'], 'inchikey': info[key].get('INCHIKEY'),
                'formula': formula, 'source': 'public ICEBERG 2.1 PubChem atlas',
                'entropy_similarity': float(similarity), 'explained_intensity': explained,
                'collision_energies': [p['input_value'] for p in alignment],
                'energy_alignment': alignment, 'matched_peaks': matched,
                '_predicted': (predicted, fragment_ids[key])})
        except (ValueError, AssertionError, KeyError, ZeroDivisionError) as exc:
            errors.append(str(exc))
            continue
    if grouped and not results and not missing_energies and errors:
        raise RuntimeError(f'All {len(grouped)} atlas structures failed scoring; first error: {errors[0]}')
    results = sort_candidates(results)
    top = results[:top_k]
    for index, candidate in enumerate(results):
        predicted, atlas_fragments = candidate.pop('_predicted')
        if index < top_k:  # serializing every structure's spectra is slow for large formulas
            candidate['predicted_spectra'] = _serial_spec(predicted)
            candidate['predicted_fragment_ids'] = _serial_fragment_ids(predicted, atlas_fragments)
    from rdkit import Chem
    for candidate in top:
        candidate['structure_image'] = _structure_image(candidate['smiles'])
        mol = Chem.MolFromSmiles(candidate['smiles'])
        candidate['canonical_smiles'] = Chem.MolToSmiles(mol) if mol else candidate['smiles']
    return {'library_structures': len(grouped), 'scored_structures': len(results),
            'candidates': top, 'atlas_energies_ev': sorted({int(round(float(s.collision_energy))) for spectra in grouped.values() for s in spectra}),
            'missing_energies_ev': sorted(missing_energies)}


def formula_candidates(spectrum: str, experimental_unit: str, max_candidates: int = 3,
                       ms1_ppm: float = 5.0, ms2_ppm: float = 10.0) -> list[dict]:
    """Use ms-pred's MSBuddy bridge, retaining evidence that formula is inferred."""
    from ms_pred import common
    metadata, _ = common.parse_spectra(spectrum)
    if metadata.get('formula'):
        return [{'formula': metadata['formula'], 'source': 'input'}]
    # MSBuddy accepts MGF. Convert each .ms collision block to one MGF spectrum.
    from msms_structure_elucidation.spectrum import inspect_ms
    from tempfile import TemporaryDirectory
    parsed = inspect_ms(Path(spectrum), experimental_unit)
    with TemporaryDirectory() as temp:
        mgf = Path(temp) / 'query.mgf'
        with mgf.open('w') as out:
            for ce, peaks in parsed['spectra'].items():
                charge = '1-' if parsed['adduct'].endswith('-') else '1+'
                out.write(f"BEGIN IONS\nTITLE=query_{ce}\nPEPMASS={parsed['parentmass']}\nCHARGE={charge}\n"
                          f"ADDUCT={parsed['adduct']}\n")
                for mz, inten in peaks:
                    out.write(f'{mz} {inten}\n')
                out.write('END IONS\n')
        try:
            import msbuddy
            # Use MSBuddy's own annotated MGF importer and formula ranking. MSBuddy keeps its database
            # in a module global that every instance resets, so concurrent batch features take turns.
            with _MSBUDDY_LOCK:
                engine = msbuddy.Msbuddy(msbuddy.MsbuddyConfig(ppm=True, ms1_tol=ms1_ppm, ms2_tol=ms2_ppm))
                engine.load_mgf(str(mgf))
                engine.annotate_formula()
            formulas = []
            for data in engine.data:
                for entry in getattr(data, 'candidate_formula_list', []) or []:
                    formula = str(getattr(entry, 'formula', ''))
                    if formula and formula not in formulas:
                        formulas.append(formula)
                    if len(formulas) >= max_candidates:
                        break
            return [{'formula': f, 'source': 'MSBuddy inferred'} for f in formulas]
        except (ImportError, AttributeError, ValueError) as exc:
            raise RuntimeError(f'MSBuddy formula inference failed: {exc}. Supply --formula.') from exc


def validate_smiles(smiles: list[str], formula: str) -> list[dict]:
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors
    out = []
    seen = set()
    for entry in smiles:
        mol = Chem.MolFromSmiles(entry)
        if mol is None:
            out.append({'smiles': entry, 'formula': None, 'formula_match': False,
                        'invalid_smiles': True})
            continue
        canonical = Chem.MolToSmiles(mol)
        if canonical in seen:
            continue
        seen.add(canonical)
        found = rdMolDescriptors.CalcMolFormula(mol)
        try:  # connectivity block of the InChIKey: matches atlas entries stored without stereochemistry
            connectivity = Chem.MolToInchiKey(mol)[:14] or None
        except Exception:
            connectivity = None
        out.append({'smiles': canonical, 'formula': found, 'formula_match': found == formula,
                    'connectivity': connectivity})
    return out


def atlas_smiles(mgf: str) -> list[str]:
    """Return every distinct atlas structure for a formula, without a candidate cap."""
    from ms_pred import common
    seen = set()
    result = []
    for meta, _ in common.parse_spectra_mgf(mgf):
        smiles = meta.get('SMILES')
        key = meta.get('INCHIKEY') or smiles
        if smiles and key not in seen:
            seen.add(key)
            result.append(smiles)
    return result


def atlas_info(mgf: str, expected_energies: list[int], spectrum: str, formula: str,
               experimental_unit: str) -> dict:
    """Scan atlas coverage without retaining its spectra in memory."""
    from ms_pred import common
    metadata, _, _ = _experimental_spectra(spectrum, experimental_unit)
    precursor = float(metadata['parentmass'])
    expected_mass = common.formula_mass(formula) + common.ion2mass[metadata.get('ionization', '[M+H]+')]
    if abs(expected_mass - precursor) > max(0.01, precursor * 20e-6):
        raise ValueError(f'Formula/adduct precursor mismatch: {formula} predicts {expected_mass:.4f}, observed {precursor:.4f}')
    by_structure = {}
    for meta, _ in common.parse_spectra_mgf(mgf):
        smiles = meta.get('SMILES')
        label = meta.get('COLLISION_ENERGY')
        if not smiles or not label:
            continue
        try:
            ev = int(round(float(str(label).split()[0])))
        except ValueError:
            continue
        key = meta.get('INCHIKEY') or smiles
        if key not in by_structure:
            by_structure[key] = [smiles, set()]
        by_structure[key][1].add(ev)
    expected = set(expected_energies)
    missing = set()
    for _, energies in by_structure.values():
        missing.update(expected - energies)
    return {'library_structures': len(by_structure),
        'missing_energies_ev': sorted(missing),
        'smiles': [row[0] for row in by_structure.values()]}


def _check_glacier_features(checkpoint: str) -> None:
    """Reject known incompatible atom feature widths before costly inference."""
    import torch
    from ms_pred.glacier.dataset import TreeProcessor
    raw = torch.load(checkpoint, map_location='cpu', weights_only=False)
    hparams = raw.get('hyper_parameters', {})
    expected = hparams.get('node_feats')
    if expected is None:
        raise RuntimeError('GLACIER checkpoint has no node_feats metadata; compatibility cannot be verified')
    processor = TreeProcessor(root_encode=hparams.get('root_encode') or 'graphormer',
        embed_elem_group=hparams.get('embed_elem_group', False),
        pe_embed_k=hparams.get('pe_embed_k', 0),
        multi_hop_max_dist=hparams.get('multi_hop_max_dist', 5))
    actual = processor.get_node_feats()
    if int(expected) != actual:
        raise RuntimeError(f'GLACIER checkpoint expects {expected} atom features, but this ms-pred checkout produces {actual}. Use a compatible ms-pred commit or checkpoint.')


def simulate(spectrum: str, smiles: list[str], formula: str, model: str,
             checkpoint: str | None, gen_checkpoint: str | None,
             inten_checkpoint: str | None, output_dir: str,
             experimental_unit: str, instrument: str | None = None,
             cuda_devices: str | None = None, batch_size: int = 1,
             num_cpu_workers: int = 1, num_gpu_workers: int = 1,
             top_k: int | None = None) -> list[dict]:
    """Run ms-pred only for structures unavailable in the precomputed atlas.

    With top_k, structure images are drawn only where a merged top_k list can reach: a result
    is always ranked below every result scoring more than TIE_BAND above it."""
    metadata, experimental, source = _experimental_spectra(spectrum, experimental_unit)
    model_energies = model_collision_energies_ev(experimental)
    if model == 'glacier':
        if not checkpoint or not Path(checkpoint).is_file():
            raise FileNotFoundError('GLACIER checkpoint required; see ms-pred README for public MassSpecGym weights or provide licensed NIST weights')
        _check_glacier_features(checkpoint)
        from ms_pred.glacier.glacier_elucidation import glacier_prediction
        prediction = glacier_prediction(candidate_smiles=smiles, collision_energies=model_energies,
            nce=False, adduct=metadata.get('ionization', '[M+H]+'),
            instrument=instrument, python_path=sys.executable, ckpt=checkpoint,
            cuda_devices=cuda_devices, num_gpu_workers=1,  # >1 shards GLACIER output across GPUs
            num_cpu_workers=num_cpu_workers, batch_size=batch_size)
    else:
        if not gen_checkpoint or not inten_checkpoint or not Path(gen_checkpoint).is_file() or not Path(inten_checkpoint).is_file():
            raise FileNotFoundError('ICEBERG generation and intensity checkpoints required; see ms-pred README for public MassSpecGym weights')
        from ms_pred.iceberg.iceberg_elucidation import iceberg_prediction
        prediction = iceberg_prediction(candidate_smiles=smiles, collision_energies=model_energies,
            nce=False, adduct=metadata.get('ionization', '[M+H]+'),
            instrument=instrument, python_path=sys.executable, gen_ckpt=gen_checkpoint,
            inten_ckpt=inten_checkpoint, cuda_devices=cuda_devices, num_gpu_workers=num_gpu_workers,
            num_cpu_workers=num_cpu_workers, batch_size=batch_size)
    save_dir = Path(prediction[0])
    marker = save_dir / f'{model}_run_successful'
    if not marker.is_file() or not list(save_dir.glob('preds*.hdf5')):
        raise RuntimeError(f'{model.upper()} subprocess failed or produced no spectra at {save_dir}; check model logs and checkpoint compatibility')
    import ms_pred
    from ms_pred import common
    try:  # model versions are tracked separately from the ms-pred package version
        from ms_pred.model_registry import MODEL_REGISTRY
        model_version = MODEL_REGISTRY.get(model, {}).get('version')
    except ImportError:
        model_version = None
    ms_pred_version = getattr(ms_pred, '__version__', None)
    pred_db = common.PredSpecDB(save_dir / 'preds.hdf5')
    results = []
    try:
        for item in pred_db.get_all_specs():
            pred = item[-1]
            smi = pred.root_canonical_smiles
            alignment = align_energies(experimental, pred, source, experimental_unit)
            if len(alignment) != len(experimental):
                raise RuntimeError(f'{model.upper()} output for {smi} lacks one or more requested energies')
            score = _score(experimental, pred, alignment, float(metadata['parentmass']))
            explained, matched = _explained(experimental, pred, alignment)
            results.append({'smiles': smi, 'formula': formula, 'source': model.upper(),
                'entropy_similarity': float(score), 'explained_intensity': explained,
                'collision_energies': [p['input_value'] for p in alignment],
                'energy_alignment': alignment, 'matched_peaks': matched,
                'predicted_spectra': _serial_spec(pred),
                'predicted_fragment_ids': _serial_fragment_ids(pred),
                'model_collision_energies_ev': model_energies, 'model_instrument': instrument,
                'model_name': model.upper(), 'model_version': model_version, 'ms_pred_version': ms_pred_version,
                'model_checkpoint': checkpoint if model == 'glacier' else [gen_checkpoint, inten_checkpoint],
                'structure_image': None})
    finally:
        pred_db.close()
    if len(results) != len(smiles):
        raise RuntimeError(f'{model.upper()} returned {len(results)} spectra for {len(smiles)} structures')
    results = sort_candidates(results)
    floor = image_floor([c['entropy_similarity'] for c in results], top_k)
    for candidate in results:
        if candidate['entropy_similarity'] >= floor:
            candidate['structure_image'] = _structure_image(candidate['smiles'])
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['rank', 'formula', 'validate', 'simulate', 'atlas-smiles', 'atlas-info'])
    parser.add_argument('--spectrum')
    parser.add_argument('--mgf')
    parser.add_argument('--formula')
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--limit-images', action='store_true', help='simulate: draw structures for --top-k only')
    parser.add_argument('--smiles-json')
    parser.add_argument('--model', choices=['glacier', 'iceberg'], default='glacier')
    parser.add_argument('--checkpoint')
    parser.add_argument('--gen-checkpoint')
    parser.add_argument('--inten-checkpoint')
    parser.add_argument('--output-dir')
    parser.add_argument('--experimental-unit', choices=['NCE', 'eV'])
    parser.add_argument('--instrument')
    parser.add_argument('--cuda-devices')
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--num-cpu-workers', type=int, default=1)
    parser.add_argument('--num-gpu-workers', type=int, default=1)
    parser.add_argument('--energies')
    parser.add_argument('--ms1-ppm', type=float, default=5.0)
    parser.add_argument('--ms2-ppm', type=float, default=10.0)
    args = parser.parse_args()
    if args.action in ('rank', 'formula', 'simulate') and not args.experimental_unit:
        parser.error('--experimental-unit is required for experimental spectra')
    if args.action == 'rank':
        result = rank(args.spectrum, args.mgf, args.formula, args.top_k, args.experimental_unit)
    elif args.action == 'atlas-smiles':
        result = atlas_smiles(args.mgf)
    elif args.action == 'atlas-info':
        result = atlas_info(args.mgf, [int(value) for value in args.energies.split(',')],
            args.spectrum, args.formula, args.experimental_unit)
    elif args.action == 'formula':
        result = formula_candidates(args.spectrum, args.experimental_unit,
                                    ms1_ppm=args.ms1_ppm, ms2_ppm=args.ms2_ppm)
    elif args.action == 'validate':
        result = validate_smiles(json.loads(Path(args.smiles_json).read_text()), args.formula)
    else:
        result = simulate(args.spectrum, json.loads(Path(args.smiles_json).read_text()),
            args.formula, args.model, args.checkpoint, args.gen_checkpoint,
            args.inten_checkpoint, args.output_dir, args.experimental_unit,
            args.instrument, args.cuda_devices, args.batch_size,
            args.num_cpu_workers, args.num_gpu_workers, args.top_k if args.limit_images else None)
    print('RESULT_JSON=' + json.dumps(result, allow_nan=False))

if __name__ == '__main__':
    main()
