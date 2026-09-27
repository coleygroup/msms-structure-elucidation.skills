"""PubChem lookups used when formula inference or the public atlas cannot supply candidates.

Only single-component, uncharged structures are kept: those are what the [M+H]+ precursor can be.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

PUBCHEM = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug'
PROTON = 1.007276
ADDUCT_SHIFT = {'[M+H]+': PROTON, '[M+Na]+': 22.989218, '[M+K]+': 38.963158, '[M+NH4]+': 18.033823,
                '[M-H]-': -PROTON}
DEFAULT_ELEMENTS = 'CHNOPS'
_BATCH = 500


def _get(path: str, data: dict | None = None, retries: int = 3) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(f'{PUBCHEM}/{path}', data=body), timeout=120) as r:
                time.sleep(0.25)  # PubChem asks for at most 5 requests per second
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            if attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    return {}


def _elements(formula: str) -> set[str]:
    return set(re.findall(r'[A-Z][a-z]?', formula))


def _closed_shell(formula: str) -> bool:
    """Ring-plus-double-bond equivalents must be a non-negative integer for a neutral, closed-shell molecule."""
    counts = {}
    for element, n in re.findall(r'([A-Z][a-z]?)(\d*)', formula):
        counts[element] = counts.get(element, 0) + (int(n) if n else 1)
    halogens = sum(counts.get(x, 0) for x in ('F', 'Cl', 'Br', 'I'))
    rdbe = counts.get('C', 0) + counts.get('Si', 0) - (counts.get('H', 0) + halogens) / 2 \
        + (counts.get('N', 0) + counts.get('P', 0) + counts.get('B', 0)) / 2 + 1
    # Parity decides closed-shell; pentavalent P (phosphates) adds one equivalent per P.
    return float(rdbe).is_integer() and rdbe + counts.get('P', 0) >= 0


def _usable(props: dict, elements: str) -> bool:
    smiles = props.get('SMILES', '')
    formula = props.get('MolecularFormula', '')
    allowed = set(re.findall(r'[A-Z][a-z]?', elements))
    # Isotope-labelled records (e.g. [2H]) list the unlabelled formula but a heavier mass.
    return (smiles and '.' not in smiles and not re.search(r'\[\d', smiles) and int(props.get('Charge', 0)) == 0
            and not re.search(r'[+-]', formula) and _elements(formula) <= allowed and _closed_shell(formula))


def formulas_by_mass(precursor_mz: float, adduct: str = '[M+H]+', ppm: float = 10.0,
                     elements: str = DEFAULT_ELEMENTS, limit: int = 5) -> list[dict]:
    """Molecular formulas of PubChem structures whose monoisotopic mass matches the precursor."""
    if adduct not in ADDUCT_SHIFT:
        raise ValueError(f'Unsupported adduct for PubChem mass search: {adduct}')
    neutral = precursor_mz - ADDUCT_SHIFT[adduct]
    tol = neutral * ppm * 1e-6
    cids = _get(f'compound/monoisotopic_mass/range/{neutral - tol:.5f}/{neutral + tol:.5f}/cids/JSON')
    cids = cids.get('IdentifierList', {}).get('CID', [])
    found: dict[str, dict] = {}
    for start in range(0, len(cids), _BATCH):
        table = _get('compound/cid/property/MolecularFormula,MonoisotopicMass,Charge,SMILES/JSON',
                     {'cid': ','.join(map(str, cids[start:start + _BATCH]))})
        for props in table.get('PropertyTable', {}).get('Properties', []):
            if not _usable(props, elements):
                continue
            entry = found.setdefault(props['MolecularFormula'], {
                'formula': props['MolecularFormula'], 'structures': 0,
                'ppm': round((float(props['MonoisotopicMass']) - neutral) / neutral * 1e6, 2)})
            entry['structures'] += 1
    ranked = sorted(found.values(), key=lambda f: (-f['structures'], abs(f['ppm'])))
    return [{**f, 'source': f'PubChem mass match (±{ppm:g} ppm, {elements})'} for f in ranked[:limit]]


def structures_for_formula(formula: str, limit: int = 500) -> list[str]:
    """Isomeric SMILES of single-component, uncharged PubChem structures with this formula."""
    table = _get(f'compound/fastformula/{urllib.parse.quote(formula)}/property/MolecularFormula,Charge,SMILES/JSON'
                 f'?MaxRecords={int(limit) * 2}')
    smiles = []
    for props in table.get('PropertyTable', {}).get('Properties', []):
        if props.get('MolecularFormula') == formula and _usable(props, ''.join(sorted(_elements(formula)))):
            if props['SMILES'] not in smiles:
                smiles.append(props['SMILES'])
        if len(smiles) >= limit:
            break
    return smiles
