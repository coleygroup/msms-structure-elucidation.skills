"""PubChem structure lookups for direct mass search, formula fallback and candidate review.

Mass searches retain neutral, single-component structures after correcting for the precursor adduct.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

from msms_structure_elucidation.ions import MODEL_ADDUCTS, NEGATIVE, POSITIVE, PROTON

PUBCHEM = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug'
ADDUCT_SHIFT = {adduct: shift for adduct, shift in {**POSITIVE, **NEGATIVE}.items() if adduct in MODEL_ADDUCTS}
DEFAULT_ELEMENTS = 'CHNOPSClBrFISiBSe'
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


def _search_cids(response: dict) -> list[int]:
    """Resolve a complete PubChem CID search, including asynchronous list keys."""
    for _ in range(30):
        if 'Waiting' not in response:
            break
        list_key = response['Waiting'].get('ListKey')
        if not list_key:
            raise RuntimeError('PubChem asynchronous search did not provide a list key')
        key = urllib.parse.quote(str(list_key))
        time.sleep(2)
        response = _get(f'compound/listkey/{key}/cids/JSON')
        if not response:
            raise RuntimeError('PubChem search list expired before all CIDs were retrieved')
    if not response:
        return []  # The initial mass query returned HTTP 404: no matches.
    identifiers = response.get('IdentifierList', {}).get('CID')
    if not isinstance(identifiers, list) or any(type(cid) is not int or cid <= 0 for cid in identifiers):
        raise RuntimeError('PubChem did not return a complete CID list: ' + str(response)[:500])
    return sorted(set(identifiers))


def mass_candidates(precursor_mz: float, adduct: str = '[M+H]+', ppm: float = 10.0,
                    elements: str = DEFAULT_ELEMENTS) -> dict:
    """Retrieve every eligible mass-matched PubChem structure, grouped by its recorded formula."""
    if adduct not in ADDUCT_SHIFT:
        raise ValueError(f'Unsupported adduct for PubChem mass search: {adduct}; only supported singly charged ions are accepted')
    neutral = precursor_mz - ADDUCT_SHIFT[adduct]
    if not all(math.isfinite(x) and x > 0 for x in (precursor_mz, neutral, ppm)):
        raise ValueError('Precursor mass, neutral mass and ppm tolerance must be positive and finite')
    if not re.fullmatch(r'(?:[A-Z][a-z]?)+', elements):
        raise ValueError('PubChem element filter must be an element list such as CHNOPS or CHNOPSClBr')
    tolerance = neutral * ppm * 1e-6
    lower, upper = neutral - tolerance, neutral + tolerance
    if lower <= 0 or not math.isfinite(upper):
        raise ValueError('PubChem mass window must be positive and finite')
    query = f'compound/monoisotopic_mass/range/{lower}/{upper}/cids/JSON'
    cids = _search_cids(_get(query))
    groups: dict[str, dict[str, dict]] = {}
    eligible = 0
    for start in range(0, len(cids), _BATCH):
        batch = cids[start:start + _BATCH]
        table = _get('compound/cid/property/MolecularFormula,MonoisotopicMass,Charge,SMILES,InChIKey/JSON',
                     {'cid': ','.join(map(str, batch))})
        rows = table.get('PropertyTable', {}).get('Properties', [])
        received = [row.get('CID') for row in rows]
        if len(received) != len(batch) or set(received) != set(batch):
            raise RuntimeError(f'Incomplete PubChem property response: expected {len(batch)} CIDs, received {len(received)}')
        for props in rows:
            required = ('MolecularFormula', 'MonoisotopicMass', 'Charge', 'SMILES', 'InChIKey')
            if any(key not in props for key in required):
                raise RuntimeError(f'Incomplete PubChem properties for CID {props["CID"]}')
            mass = float(props['MonoisotopicMass'])
            if not math.isfinite(mass):
                raise RuntimeError(f'Non-finite PubChem mass for CID {props["CID"]}')
            if not lower <= mass <= upper or not _usable(props, elements):
                continue
            eligible += 1
            by_smiles = groups.setdefault(props['MolecularFormula'], {})
            candidate = by_smiles.setdefault(props['SMILES'], {
                'smiles': props['SMILES'], 'pubchem_cids': [], 'pubchem_inchikeys': [],
                'monoisotopic_mass': mass, 'mass_error_ppm': (mass - neutral) / neutral * 1e6})
            candidate['pubchem_cids'].append(props['CID'])
            if props['InChIKey'] not in candidate['pubchem_inchikeys']:
                candidate['pubchem_inchikeys'].append(props['InChIKey'])
    return {'source': 'PubChem monoisotopic mass search', 'query_url': f'{PUBCHEM}/{query}',
            'queried_at': datetime.now(timezone.utc).isoformat(), 'precursor_mz': precursor_mz,
            'adduct': adduct, 'neutral_mass': neutral, 'ppm_tolerance': ppm, 'mass_range': [lower, upper],
            'elements': elements, 'filters': ['neutral', 'single-component', 'non-isotope-labelled', 'closed-shell'],
            'matched_cids': len(cids), 'eligible_cids': eligible, 'excluded_cids': len(cids) - eligible,
            'candidate_structures': sum(map(len, groups.values())), 'complete': True,
            'formulas': [{'formula': formula, 'candidates': list(by_smiles.values())}
                         for formula, by_smiles in sorted(groups.items())]}


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


def candidates_for_formula(formula: str, precursor_mz: float, adduct: str, ppm: float = 20.) -> dict:
    """Retrieve the complete CID list for one selected formula, without a top-structure cap."""
    cids = _search_cids(_get(f'compound/fastformula/{urllib.parse.quote(formula)}/cids/JSON'))
    if len(cids) >= 2_000_000:
        raise RuntimeError('PubChem formula search reached its record limit; completeness is unverified')
    neutral = precursor_mz - ADDUCT_SHIFT[adduct]
    found = {}
    for start in range(0, len(cids), _BATCH):
        batch = cids[start:start + _BATCH]
        table = _get('compound/cid/property/MolecularFormula,MonoisotopicMass,Charge,SMILES,InChIKey/JSON',
                     {'cid': ','.join(map(str, batch))})
        rows = table.get('PropertyTable', {}).get('Properties', [])
        if len(rows) != len(batch) or {r.get('CID') for r in rows} != set(batch):
            raise RuntimeError('Incomplete PubChem formula candidate property response')
        for row in rows:
            if any(k not in row for k in ('MolecularFormula', 'MonoisotopicMass', 'Charge', 'SMILES', 'InChIKey')):
                raise RuntimeError('Incomplete PubChem formula candidate properties')
            if row['MolecularFormula'] != formula or not _usable(row, ''.join(_elements(formula))):
                continue
            mass = float(row['MonoisotopicMass'])
            if not math.isfinite(mass) or mass <= 0:
                raise RuntimeError('Invalid PubChem monoisotopic mass')
            if abs(mass-neutral) > neutral*ppm*1e-6:
                raise ValueError(f'Formula/adduct precursor mismatch: {formula} differs by '
                                 f'{(mass-neutral)/neutral*1e6:.1f} ppm (tolerance {ppm:g})')
            record = found.setdefault(row['SMILES'], {'smiles': row['SMILES'], 'pubchem_cids': [],
                'pubchem_inchikeys': [], 'monoisotopic_mass': mass,
                'mass_error_ppm': (mass - neutral) / neutral * 1e6})
            record['pubchem_cids'].append(row['CID'])
            if row['InChIKey'] not in record['pubchem_inchikeys']:
                record['pubchem_inchikeys'].append(row['InChIKey'])
    return {'formula': formula, 'candidates': list(found.values()), 'complete': True, 'matched_cids': len(cids)}


_PROPERTIES = 'ConnectivitySMILES,InChIKey,MolecularFormula,MolecularWeight,IUPACName'


def isomers(query: str, query_type: str = 'formula', max_results: int = 20) -> list[dict]:
    """PubChem compounds for a formula or SMILES, with identifiers and basic properties."""
    if query_type not in ('formula', 'smiles'):
        raise ValueError('query_type must be formula or smiles')
    cids = _get(f'compound/{query_type}/{urllib.parse.quote(query)}/cids/JSON')
    for _ in range(10):  # formula searches are asynchronous: poll the ListKey until the list is ready
        if 'Waiting' not in cids:
            break
        time.sleep(2)
        cids = _get(f'compound/listkey/{cids["Waiting"]["ListKey"]}/cids/JSON')
    found = cids.get('IdentifierList', {}).get('CID', [])[:max_results]
    if not found:
        return []
    table = _get(f'compound/cid/{",".join(map(str, found))}/property/{_PROPERTIES}/JSON')
    return table.get('PropertyTable', {}).get('Properties', [])


def compound(identifier: str, id_type: str = 'name') -> dict:
    """One PubChem compound by CID, name, InChIKey or SMILES, with up to ten synonyms; {} when absent."""
    if id_type not in ('cid', 'name', 'inchikey', 'smiles'):
        raise ValueError('id_type must be cid, name, inchikey or smiles')
    encoded = urllib.parse.quote(identifier)
    rows = _get(f'compound/{id_type}/{encoded}/property/{_PROPERTIES}/JSON').get('PropertyTable', {}).get('Properties', [])
    if not rows:
        return {}
    synonyms = _get(f'compound/{id_type}/{encoded}/synonyms/JSON').get('InformationList', {}).get('Information', [{}])
    return {**rows[0], 'Synonyms': synonyms[0].get('Synonym', [])[:10]}
