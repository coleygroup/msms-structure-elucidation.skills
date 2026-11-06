"""Read formula-specific SMILES/InChIKey records from the public PubChem index."""
from __future__ import annotations

import json
import argparse
import hashlib
import math
import os
import re
from pathlib import Path

from msms_structure_elucidation.ions import NEGATIVE, POSITIVE
from msms_structure_elucidation.prediction_cache import cache_root, file_fingerprint, file_lock
from msms_structure_elucidation.mcp.jobs import read_json, write_json

DATASET_URL = 'https://zenodo.org/records/18502041/files/pubchem_formulae_inchikey.hdf5'


def formula_candidates(formula: str, formula_map: str, precursor_mz: float, adduct: str,
                       ppm: float = 5., prediction_cache: str | None = None) -> dict | None:
    """Return all eligible snapshot candidates; None means the formula is not indexed."""
    import h5py
    from rdkit import Chem, rdBase
    from rdkit.Chem import rdMolDescriptors
    from msms_structure_elucidation.pubchem import _closed_shell, has_negatively_charged_carbon
    from msms_structure_elucidation.worker import validate_smiles
    from ms_pred import common

    if not re.fullmatch(r'(?:[A-Z][a-z]?\d*)+', formula):
        raise ValueError('Invalid formula for the PubChem index')
    if adduct not in {**POSITIVE, **NEGATIVE} or not all(math.isfinite(v) and v > 0 for v in (precursor_mz, ppm)):
        raise ValueError('The formula lookup requires a supported adduct and positive mass/ppm')
    path = Path(formula_map).expanduser().resolve()
    root = cache_root(prediction_cache)
    digest = file_fingerprint(path, root / 'formula-index')
    validation = {'worker_sha256': hashlib.sha256(Path(__file__).with_name('worker.py').read_bytes()).hexdigest(),
                  'pubchem_sha256': hashlib.sha256(Path(__file__).with_name('pubchem.py').read_bytes()).hexdigest(),
                  'rdkit_version': rdBase.rdkitVersion, 'valid_elements': sorted(common.VALID_ELEMENTS)}
    validation_key = hashlib.sha256(json.dumps(validation, sort_keys=True).encode()).hexdigest()
    target = root / 'formula-index' / digest / validation_key / (formula + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target.with_suffix('.lock')):
        cached = read_json(target)
        if cached is None:
            access = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
            metadata = access.get_mdc_config()
            metadata.set_initial_size = 1
            metadata.initial_size = metadata.max_size = 128 * 1024**2
            metadata.min_size = 16 * 1024**2
            access.set_mdc_config(metadata)
            with h5py.File(h5py.h5f.open(os.fsencode(path), h5py.h5f.ACC_RDONLY, fapl=access)) as handle:
                if formula not in handle:
                    return None
                value = handle[formula][0]
                records = json.loads(value.decode() if isinstance(value, bytes) else value)
            candidates, excluded = {}, {}
            for record in records:
                if not isinstance(record, list) or len(record) != 2 or not all(isinstance(x, str) for x in record):
                    raise ValueError('The PubChem formula dataset must contain [SMILES, InChIKey] pairs')
                smiles, key = record
                if not re.fullmatch(r'[A-Z]{14}(?:-[A-Z]{10}-[A-Z])?', key):
                    raise ValueError('Invalid InChIKey in the PubChem formula index')
                mol = Chem.MolFromSmiles(smiles)
                reason = None
                if mol is None:
                    reason = 'invalid_smiles'
                elif has_negatively_charged_carbon(smiles):
                    reason = 'negatively_charged_carbon'
                elif '.' in smiles or Chem.GetFormalCharge(mol) or any(atom.GetIsotope() for atom in mol.GetAtoms()):
                    reason = 'charge_component_or_isotope'
                elif rdMolDescriptors.CalcMolFormula(mol) != formula or not _closed_shell(formula):
                    reason = 'formula_or_closed_shell'
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                    continue
                candidate = candidates.setdefault(smiles, {'smiles': smiles, 'pubchem_cids': [],
                    'pubchem_inchikeys': [], 'monoisotopic_mass': rdMolDescriptors.CalcExactMolWt(mol)})
                if key not in candidate['pubchem_inchikeys']:
                    candidate['pubchem_inchikeys'].append(key)
            cached = {'formula': formula, 'candidates': list(candidates.values()), 'index_records': len(records),
                      'excluded_records': excluded, 'formula_index_sha256': digest,
                      'validated_structures': validate_smiles(list(candidates), formula, keep_duplicates=True,
                                                              strip_stereo=True),
                      'validation_source': validation}
            write_json(target, cached)
    neutral = precursor_mz - {**POSITIVE, **NEGATIVE}[adduct]
    if neutral <= 0:
        raise ValueError('The adduct-corrected neutral mass must be positive')
    candidates = []
    for candidate in cached['candidates']:
        error = (candidate['monoisotopic_mass'] - neutral) / neutral * 1e6
        if abs(error) > ppm:
            raise ValueError(f'Formula/adduct precursor mismatch: {formula} differs by {error:.1f} ppm (tolerance {ppm:g})')
        candidates.append({**candidate, 'mass_error_ppm': error})
    return {**cached, 'candidates': candidates, 'complete': True,
            'coverage_scope': 'configured PubChem index snapshot', 'source': 'PubChem formula index',
            'formula_index': str(path), 'dataset_url': DATASET_URL, 'matched_cids': None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formula', required=True)
    parser.add_argument('--formula-map', required=True)
    parser.add_argument('--precursor-mz', type=float, required=True)
    parser.add_argument('--adduct', required=True)
    parser.add_argument('--ms1-ppm', type=float, default=5.)
    parser.add_argument('--prediction-cache')
    args = parser.parse_args()
    result = formula_candidates(args.formula, args.formula_map, args.precursor_mz, args.adduct,
                                args.ms1_ppm, args.prediction_cache)
    print('RESULT_JSON=' + json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
