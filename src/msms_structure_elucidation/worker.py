"""Scientific operations run in a Python environment with ms-pred installed."""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path


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
        ev_key = str(round(float(ev), 8))
        if ev_key in mapped:
            raise ValueError(f'Experimental energy labels collapse to {ev_key} eV')
        mapped[ev_key] = spec
        source[ev_key] = raw
    return metadata, mapped, source


def align_energies(experimental: dict, predicted: dict, source: dict,
                   unit: str, max_gap_ev: float = 2.0) -> list[dict]:
    """Match every experimental energy to its nearest available eV spectrum."""
    matched = []
    for exp_key in sorted(experimental.keys(), key=float):
        ev = float(exp_key)
        if not predicted.keys():
            break
        atlas_key = min(predicted.keys(), key=lambda key: (abs(ev-float(key)), float(key)))
        atlas_ev = float(atlas_key)
        gap = abs(ev-atlas_ev)
        if gap > max_gap_ev:
            continue
        matched.append({'experimental_key': exp_key, 'atlas_key': atlas_key,
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


def _explained(experimental, predicted, alignment, ppm=10):
    matched = []
    total = 0.0
    covered = 0.0
    for pair in alignment:
        ce = pair['experimental_key']
        a, b = experimental[ce], predicted[pair['atlas_key']]
        masses = list(map(float, b.masses))
        for mz, intensity in zip(a.masses, a.intens):
            mz, intensity = float(mz), float(intensity)
            total += intensity
            hits = [p for p in masses if abs(p-mz) <= max(0.002, mz * ppm * 1e-6)]
            if hits:
                covered += intensity
                matched.append({'ce': ce, 'mz': mz, 'predicted_mz': min(hits, key=lambda p: abs(p-mz))})
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
    for key, spectra in grouped.items():
        try:
            predicted = CompositeMassSpec(spectra)
            alignment = align_energies(experimental, predicted, source, experimental_unit)
            if not alignment:
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
                'predicted_spectra': _serial_spec(predicted),
                'predicted_fragment_ids': _serial_fragment_ids(predicted, fragment_ids[key])})
        except (ValueError, AssertionError, KeyError, ZeroDivisionError):
            continue
    results.sort(key=lambda c: (c['entropy_similarity'], c['explained_intensity']), reverse=True)
    top = results[:top_k]
    from rdkit import Chem
    for candidate in top:
        candidate['structure_image'] = _structure_image(candidate['smiles'])
        mol = Chem.MolFromSmiles(candidate['smiles'])
        candidate['canonical_smiles'] = Chem.MolToSmiles(mol) if mol else candidate['smiles']
    return {'library_structures': len(grouped), 'scored_structures': len(results),
            'candidates': top}


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
                out.write(f"BEGIN IONS\nTITLE=query_{ce}\nPEPMASS={parsed['parentmass']}\nCHARGE=1+\n")
                for mz, inten in peaks:
                    out.write(f'{mz} {inten}\n')
                out.write('END IONS\n')
        try:
            import msbuddy
            # Use MSBuddy's own annotated MGF importer and formula ranking.
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


def simulate(spectrum: str, smiles: list[str], formula: str, model: str,
             checkpoint: str | None, gen_checkpoint: str | None,
             inten_checkpoint: str | None, output_dir: str,
             experimental_unit: str) -> list[dict]:
    """Run ms-pred only for structures unavailable in the precomputed atlas."""
    metadata, experimental, source = _experimental_spectra(spectrum, experimental_unit)
    model_energies = model_collision_energies_ev(experimental)
    # GPU ids for ms-pred (e.g. "0"); unset runs on CPU.
    cuda_devices = os.environ.get('MSMS_CUDA_DEVICES') or None
    device_kwargs = (dict(cuda_devices=cuda_devices, num_gpu_workers=1, num_cpu_workers=4, batch_size=8)
                     if cuda_devices else dict(cuda_devices=None, num_gpu_workers=1, num_cpu_workers=1, batch_size=1))
    instrument = 'QTOF' if 'tof' in metadata.get('instrumentation', '').lower() else 'Orbitrap'
    # ms-pred launches its prediction scripts by paths relative to the checkout root.
    import ms_pred
    package_file = getattr(ms_pred, '__file__', None)
    checkout = Path(package_file).resolve().parents[2] if package_file else None
    if checkout and (checkout / 'src' / 'ms_pred').is_dir():
        os.chdir(checkout)
    if model == 'glacier':
        if not checkpoint or not Path(checkpoint).is_file():
            raise FileNotFoundError('GLACIER checkpoint required; see ms-pred README for public MassSpecGym weights or provide licensed NIST weights')
        from ms_pred.glacier.glacier_elucidation import glacier_prediction, load_pred_spec
        prediction = glacier_prediction(candidate_smiles=smiles, collision_energies=model_energies,
            nce=False, adduct=metadata.get('ionization', '[M+H]+'),
            instrument=instrument, python_path=sys.executable, ckpt=checkpoint, **device_kwargs)
    else:
        if not gen_checkpoint or not inten_checkpoint or not Path(gen_checkpoint).is_file() or not Path(inten_checkpoint).is_file():
            raise FileNotFoundError('ICEBERG generation and intensity checkpoints required; see ms-pred README for public MassSpecGym weights')
        from ms_pred.iceberg.iceberg_elucidation import iceberg_prediction, load_pred_spec
        prediction = iceberg_prediction(candidate_smiles=smiles, collision_energies=model_energies,
            nce=False, adduct=metadata.get('ionization', '[M+H]+'),
            instrument=instrument, python_path=sys.executable, gen_ckpt=gen_checkpoint,
            inten_ckpt=inten_checkpoint, **device_kwargs)
    save_dir = prediction[0]
    try:  # model versions are tracked separately from the ms-pred package version
        from ms_pred.model_registry import MODEL_REGISTRY
        model_version = MODEL_REGISTRY.get(model, {}).get('version')
    except ImportError:
        model_version = None
    ms_pred_version = getattr(ms_pred, '__version__', None)
    predicted_smiles, predicted_spectra = load_pred_spec(save_dir)
    results = []
    for smi, pred in zip(predicted_smiles, predicted_spectra):
        try:
            alignment = align_energies(experimental, pred, source, experimental_unit)
            if not alignment:
                continue
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
                'structure_image': _structure_image(smi)})
        except ValueError:
            continue
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['rank', 'formula', 'validate', 'simulate'])
    parser.add_argument('--spectrum')
    parser.add_argument('--mgf')
    parser.add_argument('--formula')
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--smiles-json')
    parser.add_argument('--model', choices=['glacier', 'iceberg'], default='glacier')
    parser.add_argument('--checkpoint')
    parser.add_argument('--gen-checkpoint')
    parser.add_argument('--inten-checkpoint')
    parser.add_argument('--output-dir')
    parser.add_argument('--experimental-unit', choices=['NCE', 'eV'])
    parser.add_argument('--ms1-ppm', type=float, default=5.0)
    parser.add_argument('--ms2-ppm', type=float, default=10.0)
    args = parser.parse_args()
    if args.action in ('rank', 'formula', 'simulate') and not args.experimental_unit:
        parser.error('--experimental-unit is required for experimental spectra')
    if args.action == 'rank':
        result = rank(args.spectrum, args.mgf, args.formula, args.top_k, args.experimental_unit)
    elif args.action == 'formula':
        result = formula_candidates(args.spectrum, args.experimental_unit,
                                    ms1_ppm=args.ms1_ppm, ms2_ppm=args.ms2_ppm)
    elif args.action == 'validate':
        result = validate_smiles(json.loads(Path(args.smiles_json).read_text()), args.formula)
    else:
        result = simulate(args.spectrum, json.loads(Path(args.smiles_json).read_text()),
            args.formula, args.model, args.checkpoint, args.gen_checkpoint,
            args.inten_checkpoint, args.output_dir, args.experimental_unit)
    print('RESULT_JSON=' + json.dumps(result, allow_nan=False))

if __name__ == '__main__':
    main()
