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

from msms_structure_elucidation.pubchem import has_negatively_charged_carbon

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
    if metadata.get('stepped_collision_energies'):
        from msms_structure_elucidation.collision import stepped_components
        if len(spectra) != 1:
            raise ValueError('Stepped acquisition requires one measured composite spectrum')
        info = stepped_components(metadata['stepped_collision_energies'], precursor, unit, common.nce_to_ev)
        return metadata, {'stepped': next(iter(spectra.values()))}, {'stepped': info}
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
    if 'stepped' in experimental:
        if 'stepped' not in predicted:
            return []
        info = source['stepped']
        return [{'experimental_key': 'stepped', 'atlas_key': 'stepped', 'prediction_key': 'stepped',
                 'acquisition_mode': 'stepped', 'input_value': None, 'input_values': info['values'],
                 'input_unit': unit, 'experimental_ev': None, 'atlas_ev': None, 'model_energy_ev': None,
                 'model_energies_ev': info['model_energies_ev'], 'component_energies_ev': info['energies_ev'],
                 'merge_method': info['merge_method'], 'weights': info['weights'], 'delta_ev': None}]
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


def model_collision_energies_ev(experimental: dict, source: dict | None = None) -> list[int]:
    """Pass only rounded integer eV values to forward models."""
    if 'stepped' in experimental:
        if not source or 'stepped' not in source:
            raise ValueError('Missing stepped collision-energy provenance')
        return sorted(set(source['stepped']['model_energies_ev']))
    energies = [float(key) for key in experimental.keys()]
    if not energies or any(not math.isfinite(ev) or ev <= 0 for ev in energies):
        raise ValueError('Model collision energies must be positive finite eV values')
    rounded = sorted({int(round(ev)) for ev in energies})
    if rounded[0] <= 0:
        raise ValueError('Rounded model collision energies must be positive integers')
    return rounded


def _scoring_prediction(predicted, source: dict):
    """Merge stepped predictions once, preserving the untouched component spectra."""
    if 'stepped' not in source:
        return predicted
    indexed = {int(round(float(key))): key for key in predicted.keys()}
    requested = source['stepped']['model_energies_ev']
    missing = set(requested) - set(indexed)
    if missing:
        raise ValueError(f'Missing stepped prediction energies: {sorted(missing)}')
    from ms_pred import common
    from functools import cached_property
    import numpy as np

    class _MergeComponent(common.MassSpec):
        # These copies are immutable during this merge. Decode masks once per
        # component instead of rebuilding every mask for every native peak lookup.
        @cached_property
        def int_frags(self) -> list[int] | None:
            return super().int_frags

    components, retained = [], set()
    for key in dict.fromkeys(indexed[ev] for ev in requested):
        spec = predicted[key]
        masses, intens = np.asarray(spec.masses), np.asarray(spec.intens)
        if not np.isfinite(masses).all() or not np.isfinite(intens).all() or (intens < 0).any():
            raise ValueError('Invalid stepped prediction peaks')
        keep = intens > 0
        if not keep.any():
            continue
        # Native merging iterates num_peaks (positive count); remove interleaved zeros first.
        components.append(_MergeComponent(collision_energy=float(key),
            root_canonical_smiles=spec.root_canonical_smiles, adduct=spec.adduct,
            masses=masses[keep].copy(), intens=intens[keep].copy(),
            frags=spec.frags[keep].copy() if spec.frags is not None else None))
        retained.add(key)
    if not components:
        raise ValueError('Stepped prediction has no positive intensity')
    if source['stepped'].get('merge_method') == 'arithmetic_mean':
        cleaned = {int(round(float(s.collision_energy))): s for s in components}
        union = {}
        for energy in requested:
            if energy in cleaned:
                union = cleaned[energy]._merge_spec_to_tup(union, merge_method='sum')
        values = list(union.values())
        first = components[0]
        merged = common.MassSpec(collision_energy=first.collision_energy,
            root_canonical_smiles=first.root_canonical_smiles, adduct=first.adduct,
            masses=np.array([v[0] for v in values], dtype=float),
            intens=np.array([v[1] / len(requested) for v in values], dtype=float),
            frags=np.stack([v[2] for v in values]) if values and values[0][2] is not None else None)
        return {**dict(predicted.items()), 'stepped': merged}
    merged = common.CompositeMassSpec(components).merge_spectra(
        energies=[indexed[ev] for ev in requested if indexed[ev] in retained], merge_method='sum')
    return {**dict(predicted.items()), 'stepped': merged}


def _input_energies(alignment: list[dict]) -> list[float]:
    """Keep component settings for stepped data and scalar labels for separate spectra."""
    return [value for pair in alignment for value in
            (pair['input_values'] if pair.get('acquisition_mode') == 'stepped' else [pair['input_value']])]


def _score(experimental, predicted, alignment, precursor):
    scores = []
    for pair in alignment:
        score = float(predicted[pair['atlas_key']].entr_sim(
            experimental[pair['experimental_key']], ignore_mass=precursor-1))
        pair['entropy_similarity'] = score
        scores.append(score)
    return sum(scores) / len(scores)


# Entropy difference within which candidates sharing a formula and matched peaks are flagged as
# possibly indistinguishable. It does not reorder candidates: on NIST20/NIST'23 retrieval benchmarks,
# reordering such near-ties by explained intensity lowered top-1 accuracy for ICEBERG and GLACIER.
AMBIGUITY_BAND = 0.03


def image_floor(scores: list[float], top_k: int | None) -> float:
    """Lowest entropy that can still reach a merged top_k list, which is ordered by entropy."""
    ordered = sorted(scores, reverse=True)
    return ordered[top_k - 1] if top_k and len(ordered) >= top_k else -math.inf


def sort_candidates(candidates: list[dict], ambiguity_band: float = AMBIGUITY_BAND) -> list[dict]:
    """Rank by mean entropy similarity (precursor excluded); explained intensity breaks exact ties only."""
    result = sorted(candidates, key=lambda c: (c['entropy_similarity'], c['explained_intensity']), reverse=True)
    # Flag a candidate when an earlier one has the same formula, identical matched peaks and entropy
    # within ambiguity_band. Every earlier candidate scores at least entropy - ambiguity_band, so the first match is
    # the first earlier entry of that group scoring at most entropy + ambiguity_band: a bisect on running minima.
    groups = {}
    for index, a in enumerate(result):
        a['rank'] = index + 1
        a['ambiguity'] = []
        key = (a.get('formula'), frozenset((p['ce'], round(p['mz'], 4)) for p in a.get('matched_peaks', [])))
        earlier, negated_minima = groups.setdefault(key, ([], []))
        score = a['entropy_similarity']
        # The margin keeps float rounding from skipping a match; the scan applies the exact test.
        for b in earlier[bisect.bisect_left(negated_minima, -(score + ambiguity_band + 1e-9)):]:
            if abs(b['entropy_similarity'] - score) <= ambiguity_band:
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
        if not smiles or not ce or len(peaks) == 0 or has_negatively_charged_carbon(smiles):
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
    expected_energies = set(model_collision_energies_ev(experimental, source))
    missing_energies = set()
    errors = []
    for key, spectra in grouped.items():
        try:
            predicted = CompositeMassSpec(spectra)
            available = {int(round(float(ce))) for ce in predicted.keys()}
            missing_energies.update(expected_energies - available)
            if expected_energies - available:
                continue
            predicted = _scoring_prediction(predicted, source)
            alignment = align_energies(experimental, predicted, source, experimental_unit)
            paired = {ev for pair in alignment for ev in (pair.get('model_energies_ev') or [pair['model_energy_ev']])}
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
                'collision_energies': _input_energies(alignment),
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
        from msms_structure_elucidation.config import settings
        options = settings().get('pipeline', {}).get('formula_search', {})
        if options.get('backend') == 'native':
            from ms_pred.common.buddy import form_from_mgf_buddy
            ranked = form_from_mgf_buddy(str(mgf), top_k_buddy_preds=max_candidates,
                profile=None, ms1_ppm=ms1_ppm, ms2_ppm=ms2_ppm, fdr_cutoff=1.0,
                halogen=options.get('halogen', True), backend='native',
                buddy_runtime=options.get('buddy_runtime'), n_cpu=options.get('n_cpu', 1))
            hypotheses = []
            for entries in ranked.values():
                for entry in sorted(entries, key=lambda row: row['rnk']):
                    if entry['form'] and entry['form'] not in [h['formula'] for h in hypotheses]:
                        hypotheses.append({'formula': entry['form'], 'rank': len(hypotheses) + 1,
                            'source': 'BUDDY native inferred', 'backend': 'native',
                            'attribution': 'Mingxun Wang @ UCR'})
                    if len(hypotheses) >= max_candidates:
                        return hypotheses
            return hypotheses
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
                if len(formulas) >= max_candidates:
                    break
            return [{'formula': f, 'source': 'MSBuddy inferred'} for f in formulas]
        except (ImportError, AttributeError, ValueError) as exc:
            raise RuntimeError(f'MSBuddy formula inference failed: {exc}. Supply --formula.') from exc


def validate_smiles(smiles: list[str], formula: str, keep_duplicates: bool = False,
                    strip_stereo: bool = False) -> list[dict]:
    """Validate formulas and canonicalize structures, optionally retaining aliases for provenance."""
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors
    from ms_pred import common
    out = []
    seen = set()
    for entry in smiles:
        mol = Chem.MolFromSmiles(entry)
        if mol is None:
            out.append({'smiles': entry, 'input_smiles': entry, 'formula': None, 'formula_match': False,
                        'invalid_smiles': True})
            continue
        if has_negatively_charged_carbon(entry):
            out.append({'smiles': entry, 'input_smiles': entry, 'formula': rdMolDescriptors.CalcMolFormula(mol),
                        'formula_match': False, 'negatively_charged_carbon': True})
            continue
        unsupported = sorted({atom.GetSymbol() for atom in mol.GetAtoms()} - set(common.VALID_ELEMENTS))
        if unsupported:
            out.append({'smiles': entry, 'input_smiles': entry, 'formula': rdMolDescriptors.CalcMolFormula(mol),
                        'formula_match': False, 'unsupported_elements': unsupported})
            continue
        if strip_stereo:
            Chem.RemoveStereochemistry(mol)
        canonical = Chem.MolToSmiles(mol)
        if canonical in seen and not keep_duplicates:
            continue
        seen.add(canonical)
        found = rdMolDescriptors.CalcMolFormula(mol)
        try:  # connectivity block of the InChIKey: matches atlas entries stored without stereochemistry
            inchikey = Chem.MolToInchiKey(mol) or None
        except Exception:
            inchikey = None
        out.append({'smiles': canonical, 'input_smiles': entry, 'formula': found, 'formula_match': found == formula,
                    'connectivity': inchikey[:14] if inchikey else None,
                    **({'model_inchikey': inchikey} if strip_stereo else {})})
    return out


def atlas_smiles(mgf: str) -> list[str]:
    """Return every distinct atlas structure for a formula, without a candidate cap."""
    from ms_pred import common
    seen = set()
    result = []
    for meta, _ in common.parse_spectra_mgf(mgf):
        smiles = meta.get('SMILES')
        key = meta.get('INCHIKEY') or smiles
        if smiles and key not in seen and not has_negatively_charged_carbon(smiles):
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
        if not smiles or not label or has_negatively_charged_carbon(smiles):
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


def _require_cuda(cuda_devices: str | None) -> None:
    """Fail fast when GPUs are configured but CUDA is unavailable (driver reset, WSL GPU suspended):
    ms-pred started with --gpu then waits indefinitely instead of exiting."""
    if cuda_devices in (None, ''):
        return
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(f'cuda_devices={cuda_devices} is set but CUDA is not available to this process; '
                           'restore GPU access (check nvidia-smi) or unset cuda_devices to run on CPU')


def _run_model(smiles: list[str], energies: list, nce: bool, adduct: str, model: str,
               checkpoint: str | None, gen_checkpoint: str | None, inten_checkpoint: str | None,
               instrument: str | None, cuda_devices: str | None, batch_size: int,
               num_cpu_workers: int, num_gpu_workers: int, force_recompute: bool = False,
               num_writers: int = 2, num_h5_chunks: int = 4, feat_cache_size: int = 256,
               sparse_k: int = 100) -> Path:
    """Run GLACIER or ICEBERG on SMILES and return the directory holding preds*.hdf5."""
    arguments = locals().copy()
    from msms_structure_elucidation.mcp import pipeline
    if pipeline.context() is not None:
        return pipeline.run_model(arguments)
    _require_cuda(cuda_devices)
    if model == 'glacier':
        if not checkpoint or not Path(checkpoint).is_file():
            raise FileNotFoundError('GLACIER checkpoint required; see ms-pred README for public MassSpecGym weights or provide licensed NIST weights')
        _check_glacier_features(checkpoint)
        if min(batch_size, num_writers, num_h5_chunks, sparse_k) < 1 or num_cpu_workers < 0 or feat_cache_size < 0:
            raise ValueError('Invalid GLACIER batch, worker, HDF5, cache or sparse-peak settings')
        from msms_structure_elucidation.glacier import run
        return run(smiles, energies, nce, adduct, instrument, checkpoint, cuda_devices,
                   batch_size, num_cpu_workers, num_writers, num_h5_chunks, feat_cache_size, sparse_k)
    else:
        if not gen_checkpoint or not inten_checkpoint or not Path(gen_checkpoint).is_file() or not Path(inten_checkpoint).is_file():
            raise FileNotFoundError('ICEBERG generation and intensity checkpoints required; see ms-pred README for public MassSpecGym weights')
        from ms_pred.iceberg.iceberg_elucidation import iceberg_prediction
        prediction = iceberg_prediction(candidate_smiles=smiles, collision_energies=energies,
            nce=nce, adduct=adduct, instrument=instrument, python_path=sys.executable, gen_ckpt=gen_checkpoint,
            inten_ckpt=inten_checkpoint, cuda_devices=cuda_devices, num_gpu_workers=num_gpu_workers,
            force_recompute=force_recompute,
            num_cpu_workers=num_cpu_workers, batch_size=batch_size)
    save_dir = Path(prediction[0])
    marker = save_dir / f'{model}_run_successful'
    if not marker.is_file() or not list(save_dir.glob('preds*.hdf5')):
        raise RuntimeError(f'{model.upper()} subprocess failed or produced no spectra at {save_dir}; check model logs and checkpoint compatibility')
    return save_dir


def _model_versions(model: str) -> tuple[str | None, str | None]:
    import ms_pred
    try:  # model versions are tracked separately from the ms-pred package version
        from ms_pred.model_registry import MODEL_REGISTRY
        model_version = MODEL_REGISTRY.get(model, {}).get('version')
    except ImportError:
        model_version = None
    return model_version, getattr(ms_pred, '__version__', None)


def _model_predictions(smiles, energies, nce, adduct, model, checkpoint, gen_checkpoint,
                       inten_checkpoint, instrument, cuda_devices, batch_size, num_cpu_workers,
                       num_gpu_workers, force_recompute, prediction_cache, runtime):
    """Yield native spectra, reusing only raw model outputs under matching scientific settings."""
    from ms_pred import common
    generated = []
    def runs(molecules, settings, normalized=False):
        from msms_structure_elucidation.mcp import pipeline
        if pipeline.context() is not None:
            arguments = dict(smiles=molecules, energies=settings, nce=nce and not normalized,
                adduct=adduct, model=model, checkpoint=checkpoint, gen_checkpoint=gen_checkpoint,
                inten_checkpoint=inten_checkpoint, instrument=instrument, cuda_devices=cuda_devices,
                batch_size=batch_size, num_cpu_workers=num_cpu_workers, num_gpu_workers=num_gpu_workers,
                force_recompute=True, num_writers=runtime.get('num_writers', 2),
                num_h5_chunks=runtime.get('num_h5_chunks', 4), feat_cache_size=runtime.get('feat_cache_size', 256),
                sparse_k=runtime.get('sparse_k', 100))
            for path in pipeline.run_groups(arguments):
                generated.append(path)
                yield path
            return
        path = _run_model(molecules, settings, nce and not normalized, adduct, model,
            checkpoint, gen_checkpoint, inten_checkpoint, instrument, cuda_devices, batch_size,
            num_cpu_workers, num_gpu_workers, force_recompute or prediction_cache is not None, **runtime)
        generated.append(path)
        yield path
    def cleanup():
        import shutil
        for path in generated:
            if model == 'glacier' and (path.name.startswith('msms-glacier-') or
                    (path.name == 'native_outputs' and path.parent.name.startswith('model_inference-'))):
                shutil.rmtree(path)
        generated.clear()
    if prediction_cache is None:
        for folder in runs(smiles, energies):
            db = common.PredSpecDB(folder / 'preds.hdf5')
            try:
                for item in db.get_all_specs():
                    yield item[-1], None
            finally:
                db.close()
        cleanup()
        return
    from msms_structure_elucidation import prediction_cache as cache
    import ms_pred
    import numpy as np
    root = cache.cache_root(prediction_cache)
    checkpoints = [checkpoint] if model == 'glacier' else [gen_checkpoint, inten_checkpoint]
    instrument = common.normalize_instrument(instrument)
    identity = cache.model_identity(model, checkpoints, Path(ms_pred.__file__).parent, root,
                                    adduct, instrument, runtime.get('sparse_k', 100))
    identity['adapter_sha256'] = cache.adapter_fingerprint(Path(__file__))
    groups = defaultdict(list)
    for smi in dict.fromkeys(common.rm_stereo(s) for s in smiles):
        precursor = common.mass_from_smi(smi) + common.ion2mass[adduct]
        ev = [common.nce_to_ev(e, precursor) if nce and not common.is_gc_instrument(instrument) else e
              for e in energies]
        groups[tuple(sorted({int(round(float(e))) for e in ev}))].append(smi)
    def produce(molecules, ev):
        records = []
        for folder in runs(molecules, ev, normalized=True):
            db = common.PredSpecDB(folder / 'preds.hdf5')
            try:
                records.extend({'smiles': p.root_canonical_smiles, 'predicted_spectra': _serial_spec(p),
                                'predicted_fragment_ids': _serial_fragment_ids(p)}
                               for item in db.get_all_specs() for p in [item[-1]])
            finally:
                db.close()
        return records
    for ev, molecules in groups.items():
        records, receipt = cache.predictions(root, identity, molecules, list(ev), produce)
        cleanup()
        for record in records:
            spectra = []
            for energy, peaks in record['predicted_spectra'].items():
                ids = record['predicted_fragment_ids'].get(energy)
                spectra.append(common.MassSpec(collision_energy=float(energy),
                    root_canonical_smiles=record['smiles'], adduct=adduct,
                    masses=np.array([p[0] for p in peaks], dtype=float),
                    intens=np.array([p[1] for p in peaks], dtype=float),
                    int_frags=[int(i) for i in ids] if ids is not None else None))
            yield common.CompositeMassSpec(spectra), receipt


def _fragment_smiles(engine, fragment_id: int) -> str | None:
    """SMILES of the atoms and bonds a fragment bitmask keeps."""
    from rdkit import Chem
    drawing = engine.get_draw_dict(fragment_id)
    if not drawing['hatoms']:
        return None
    return Chem.MolFragmentToSmiles(drawing['mol'], atomsToUse=list(drawing['hatoms']),
                                    bondsToUse=list(drawing['hbonds']) or None, canonical=True)


def _peak_annotations(smiles: str, spectra: dict, fragment_ids: dict, adduct: str) -> dict:
    """Per energy: [m/z, intensity, fragment formula, fragment SMILES] for every predicted peak."""
    from msms_structure_elucidation.visualize import _fragment_engine, _fragment_formula
    engine = _fragment_engine(smiles)
    annotated = {}
    for ce, peaks in spectra.items():
        ids = fragment_ids.get(ce) or [None] * len(peaks)
        rows = []
        for (mz, intensity), fragment_id in zip(peaks, ids):
            formula = fragment = None
            if fragment_id is not None:
                formula = _fragment_formula(engine, int(fragment_id), mz, adduct)[0] or None
                fragment = _fragment_smiles(engine, int(fragment_id))
            rows.append([mz, intensity, formula, fragment])
        annotated[ce] = rows
    return annotated


def predict(smiles: list[str], collision_energies: list[float], collision_unit: str, adduct: str,
            model: str, checkpoint: str | None, gen_checkpoint: str | None,
            inten_checkpoint: str | None, instrument: str | None = None,
            cuda_devices: str | None = None, batch_size: int = 1, num_cpu_workers: int = 1,
            num_gpu_workers: int = 1, fragments: bool = False, force_recompute: bool = False,
            stepped: bool = False, prediction_cache: str | None = None,
            num_writers: int = 2, num_h5_chunks: int = 4, feat_cache_size: int = 256,
            sparse_k: int = 100) -> list[dict]:
    """Forward-predict spectra and fragment IDs for SMILES, without an experimental spectrum."""
    if collision_unit not in ('NCE', 'eV'):
        raise ValueError('collision_unit must be NCE or eV')
    if not smiles or not collision_energies:
        raise ValueError('predict needs at least one SMILES and one collision energy')
    from msms_structure_elucidation.collision import energy_values
    collision_energies = energy_values(collision_energies)
    if stepped and len(collision_energies) < 2:
        raise ValueError('Stepped prediction requires at least two component energies')
    # ms-pred expects integer energies; keep a fractional value only when it was given.
    collision_energies = [int(value) if float(value).is_integer() else float(value) for value in collision_energies]
    from ms_pred import common
    model_version, ms_pred_version = _model_versions(model)
    results = []
    runtime = dict(num_writers=num_writers, num_h5_chunks=num_h5_chunks,
                   feat_cache_size=feat_cache_size, sparse_k=sparse_k) if model == 'glacier' else {}
    for pred, receipt in _model_predictions(smiles, collision_energies, collision_unit == 'NCE',
            adduct, model, checkpoint, gen_checkpoint, inten_checkpoint, instrument, cuda_devices,
            batch_size, num_cpu_workers, num_gpu_workers, force_recompute, prediction_cache, runtime):
        smi = pred.root_canonical_smiles
        info = None
        if stepped:
            from msms_structure_elucidation.collision import stepped_components
            precursor = common.mass_from_smi(smi) + common.ion2mass[adduct]
            info = stepped_components(collision_energies, precursor, collision_unit, common.nce_to_ev)
            pred = _scoring_prediction(pred, {'stepped': info})
        spectra, fragment_ids = _serial_spec(pred), _serial_fragment_ids(pred)
        results.append({'smiles': smi, 'adduct': adduct,
            **({'acquisition_mode': 'stepped', 'stepped_collision': info} if stepped else {}),
            'collision_energies': collision_energies, 'collision_unit': collision_unit,
            'predicted_spectra': spectra, 'predicted_fragment_ids': fragment_ids,
            'annotated_peaks': _peak_annotations(smi, spectra, fragment_ids, adduct)
            if fragments else None,
            'model_name': model.upper(), 'model_version': model_version, 'ms_pred_version': ms_pred_version,
            'model_instrument': instrument,
            'model_checkpoint': checkpoint if model == 'glacier' else [gen_checkpoint, inten_checkpoint],
            **({'prediction_cache': receipt} if receipt else {})})
    return results


def simulate(spectrum: str, smiles: list[str], formula: str, model: str,
             checkpoint: str | None, gen_checkpoint: str | None,
             inten_checkpoint: str | None, output_dir: str,
             experimental_unit: str, instrument: str | None = None,
             cuda_devices: str | None = None, batch_size: int = 1,
             num_cpu_workers: int = 1, num_gpu_workers: int = 1,
             top_k: int | None = None, force_recompute: bool = False,
             prediction_cache: str | None = None, num_writers: int = 2, num_h5_chunks: int = 4,
             feat_cache_size: int = 256, sparse_k: int = 100) -> list[dict]:
    """Predict supplied structures with ms-pred and score every experimental energy.

    With top_k, structure images are drawn only where a merged top_k list can reach: results
    are ranked by entropy similarity."""
    metadata, experimental, source = _experimental_spectra(spectrum, experimental_unit)
    model_energies = model_collision_energies_ev(experimental, source)
    model_version, ms_pred_version = _model_versions(model)
    results = []
    runtime = dict(num_writers=num_writers, num_h5_chunks=num_h5_chunks,
                   feat_cache_size=feat_cache_size, sparse_k=sparse_k) if model == 'glacier' else {}
    for pred, receipt in _model_predictions(smiles, model_energies, False,
            metadata.get('ionization', '[M+H]+'), model, checkpoint, gen_checkpoint, inten_checkpoint,
            instrument, cuda_devices, batch_size, num_cpu_workers, num_gpu_workers,
            force_recompute, prediction_cache, runtime):
        smi = pred.root_canonical_smiles
        pred = _scoring_prediction(pred, source)
        alignment = align_energies(experimental, pred, source, experimental_unit)
        if len(alignment) != len(experimental):
            raise RuntimeError(f'{model.upper()} output for {smi} lacks one or more requested energies')
        score = _score(experimental, pred, alignment, float(metadata['parentmass']))
        explained, matched = _explained(experimental, pred, alignment)
        results.append({'smiles': smi, 'formula': formula, 'source': model.upper(),
            'entropy_similarity': float(score), 'explained_intensity': explained,
            'collision_energies': _input_energies(alignment),
            'energy_alignment': alignment, 'matched_peaks': matched,
            'predicted_spectra': _serial_spec(pred),
            'predicted_fragment_ids': _serial_fragment_ids(pred),
            'model_collision_energies_ev': model_energies, 'model_instrument': instrument,
            'model_name': model.upper(), 'model_version': model_version, 'ms_pred_version': ms_pred_version,
            'model_checkpoint': checkpoint if model == 'glacier' else [gen_checkpoint, inten_checkpoint],
            'structure_image': None, **({'prediction_cache': receipt} if receipt else {})})
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
    parser.add_argument('action', choices=['rank', 'formula', 'validate', 'simulate', 'predict', 'atlas-smiles', 'atlas-info', 'run-model'])
    parser.add_argument('--model-request')
    parser.add_argument('--spectrum')
    parser.add_argument('--mgf')
    parser.add_argument('--formula')
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--limit-images', action='store_true', help='simulate: draw structures for --top-k only')
    parser.add_argument('--keep-duplicates', action='store_true', help='validate: retain canonical aliases for provenance')
    parser.add_argument('--strip-stereo', action='store_true', help='validate: use the models\' stereo-free structures')
    parser.add_argument('--force-recompute', action='store_true', help='predict/simulate: bypass ms-pred\'s internal prediction cache')
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
    parser.add_argument('--prediction-cache')
    parser.add_argument('--num-writers', type=int, default=2)
    parser.add_argument('--num-h5-chunks', type=int, default=4)
    parser.add_argument('--feat-cache-size', type=int, default=256)
    parser.add_argument('--sparse-k', type=int, default=100)
    parser.add_argument('--energies')
    parser.add_argument('--collision-energies', help='predict: comma-separated energies')
    parser.add_argument('--collision-unit', choices=['NCE', 'eV'], help='predict: unit of --collision-energies')
    parser.add_argument('--adduct', default='[M+H]+')
    parser.add_argument('--stepped', action='store_true', help='predict: merge the requested energies before final normalization')
    parser.add_argument('--fragments', action='store_true', help='predict: annotate peaks with fragment formula and SMILES')
    parser.add_argument('--ms1-ppm', type=float, default=5.0)
    parser.add_argument('--ms2-ppm', type=float, default=10.0)
    args = parser.parse_args()
    if args.action in ('rank', 'formula', 'simulate') and not args.experimental_unit:
        parser.error('--experimental-unit is required for experimental spectra')
    if args.action == 'run-model':
        payload = json.loads(Path(args.model_request).read_text())
        arguments = payload['arguments']
        if payload.get('prepared'):
            _require_cuda(arguments['cuda_devices'])
            if arguments['model'] == 'glacier':
                from msms_structure_elucidation.glacier import run_prepared
                folder = run_prepared(Path(payload['prepared']), arguments['cuda_devices'])
            else:
                from msms_structure_elucidation.iceberg import run_prepared
                folder = run_prepared(Path(payload['prepared']), arguments)
        else:
            folder = _run_model(**arguments)
        if payload.get('publish_dir'):
            # A Slurm CPU orchestrator may run on another node. Publish complete
            # native files after local scratch writes, before returning their path.
            import shutil
            destination = Path(payload['publish_dir'])
            shutil.copytree(folder, destination, dirs_exist_ok=True)
            if payload.get('prepared') and folder.name.startswith(('msms-glacier-', 'msms-iceberg-')):
                shutil.rmtree(folder)
            folder = destination
        if payload.get('publish_dir'):
            import shutil
            logs = Path(payload['publish_dir']).parent / 'native_logs'
            logs.mkdir(exist_ok=True)
            for log_path in folder.glob('*.log'):
                shutil.copy2(log_path, logs / log_path.name)
        result = {'save_dir': str(folder)}
    elif args.action == 'rank':
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
        result = validate_smiles(json.loads(Path(args.smiles_json).read_text()), args.formula,
                                 args.keep_duplicates, args.strip_stereo)
    elif args.action == 'predict':
        if not args.collision_energies or not args.collision_unit:
            parser.error('predict requires --collision-energies and --collision-unit')
        result = predict(json.loads(Path(args.smiles_json).read_text()),
            [float(value) for value in args.collision_energies.split(',')], args.collision_unit,
            args.adduct, args.model, args.checkpoint, args.gen_checkpoint, args.inten_checkpoint,
            args.instrument, args.cuda_devices, args.batch_size, args.num_cpu_workers, args.num_gpu_workers,
            args.fragments, args.force_recompute, args.stepped, args.prediction_cache,
            args.num_writers, args.num_h5_chunks, args.feat_cache_size, args.sparse_k)
    else:
        result = simulate(args.spectrum, json.loads(Path(args.smiles_json).read_text()),
            args.formula, args.model, args.checkpoint, args.gen_checkpoint,
            args.inten_checkpoint, args.output_dir, args.experimental_unit,
            args.instrument, args.cuda_devices, args.batch_size,
            args.num_cpu_workers, args.num_gpu_workers, args.top_k if args.limit_images else None,
            args.force_recompute, args.prediction_cache,
            args.num_writers, args.num_h5_chunks, args.feat_cache_size, args.sparse_k)
    print('RESULT_JSON=' + json.dumps(result, allow_nan=False))

if __name__ == '__main__':
    main()
