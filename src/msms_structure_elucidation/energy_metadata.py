"""Read acquisition energy evidence from mzML without loading binary spectra."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from msms_structure_elucidation.collision import energy_values


def scan_metadata(path: Path) -> dict[str, dict]:
    """Read precursor activation CV terms, preserving missing or conflicting metadata."""
    groups, scans = {}, {}
    for _, node in ET.iterparse(path, events=('end',)):
        tag = node.tag.rsplit('}', 1)[-1]
        if tag == 'referenceableParamGroup':
            groups[node.attrib['id']] = [dict(p.attrib) for p in node if p.tag.endswith('Param')]
        if tag != 'spectrum':
            continue
        params = [dict(p.attrib) for p in node.iter() if p.tag.rsplit('}', 1)[-1] in ('cvParam', 'userParam')]
        for ref in node.iter():
            if ref.tag.rsplit('}', 1)[-1] == 'referenceableParamGroupRef':
                params.extend(groups.get(ref.attrib.get('ref'), []))
        if not any(p.get('accession') == 'MS:1000511' and p.get('value') == '2' for p in params):
            node.clear()
            continue
        evidence, settings = [], []
        for activation in node.iter():
            if activation.tag.rsplit('}', 1)[-1] != 'activation':
                continue
            activation_params = [dict(p.attrib) for p in activation if p.tag.rsplit('}', 1)[-1] in ('cvParam', 'userParam')]
            for p in activation:
                if p.tag.rsplit('}', 1)[-1] == 'referenceableParamGroupRef':
                    activation_params.extend(groups.get(p.attrib.get('ref'), []))
            for p in activation_params:
                accession, name = p.get('accession'), p.get('name', '').lower()
                if ('stepp' in name or 'collision energy' in name) and ('weight' in name or 'dwell' in name):
                    evidence.append(p)
                    continue
                if 'collision energy' not in name and 'nce' not in name and accession not in ('MS:1000045', 'MS:1000138'):
                    continue
                evidence.append(p)
                unit = 'NCE' if accession == 'MS:1000138' or 'normalized collision energy' in name or 'nce' in name else (
                    'eV' if p.get('unitAccession') == 'UO:0000266' or p.get('unitName', '').lower() in ('electronvolt', 'electron volt', 'ev') else None)
                if unit == 'NCE' and (accession == 'MS:1000045' or p.get('unitAccession') == 'UO:0000266' or p.get('unitName', '').lower() in ('ev', 'electronvolt', 'electron volt')):
                    unit = None
                raw = p.get('value', '')
                try:
                    values = energy_values(raw)
                except ValueError:
                    continue
                mode = 'stepped' if 'stepped' in name else 'separate'
                if unit and ((len(values) == 1 and mode == 'separate') or (len(values) >= 2 and mode == 'stepped')):
                    settings.append((unit, mode, tuple(values)))
        distinct = set(settings)
        # A stepped setting and an explicitly recorded center energy can coexist.
        stepped = {s for s in distinct if s[1] == 'stepped'}
        if len(stepped) == 1 and all(s[0] == next(iter(stepped))[0] and
                (s[1] == 'stepped' or s[2][0] in next(iter(stepped))[2]) for s in distinct):
            distinct = stepped
        valid = len(distinct) == 1 and len(evidence) > 0
        # Any unreadable activation-energy attribute leaves the setting unresolved.
        if len(settings) != len(evidence):
            valid = False
        chosen = next(iter(distinct)) if valid else None
        precursor = next((p.get('value') for p in params if p.get('accession') == 'MS:1000744'), None)
        rt = next((p for p in params if p.get('accession') == 'MS:1000016'), None)
        scans[node.attrib.get('id', str(len(scans)))] = {
            'energy': chosen[2][0] if chosen and len(chosen[2]) == 1 else None,
            'energies': list(chosen[2]) if chosen else None, 'unit': chosen[0] if chosen else None,
            'acquisition_mode': chosen[1] if chosen else None, 'status': 'resolved' if chosen else 'needs_user',
            'precursor_mz': float(precursor) if precursor else None,
            'rt': float(rt['value']) * (60 if rt.get('unitAccession') == 'UO:0000031' else 1)
                  if rt and rt.get('unitAccession') in ('UO:0000031', 'UO:0000010') else None,
            'source': 'mzML:activation', 'evidence': evidence}
        node.clear()
    return scans


def inspect_energy(path: Path, precursor_mz: float | None = None,
                   rt_seconds: float | None = None, rt_window: float = 30) -> dict:
    """Report scan-scoped settings; never infer stepping from values across scans."""
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() != '.mzml':
        return {'source_file': str(path.resolve()), 'status': 'needs_conversion_or_vendor_reader',
                'reason': 'Inspect vendor acquisition metadata or convert to metadata-preserving mzML; do not infer settings from a filename.'}
    scans = scan_metadata(path)
    selected = {key: row for key, row in scans.items() if
                (precursor_mz is None or row['precursor_mz'] is not None and abs(row['precursor_mz']-precursor_mz) <= max(0.01, precursor_mz*20e-6)) and
                (rt_seconds is None or row['rt'] is not None and abs(row['rt']-rt_seconds) <= rt_window)}
    counts = Counter(json.dumps({k: row[k] for k in ('unit', 'acquisition_mode', 'energies')}, sort_keys=True)
                     for row in selected.values() if row['status'] == 'resolved')
    return {'source_file': str(path.resolve()),
            'status': 'resolved' if selected and all(row['status'] == 'resolved' for row in selected.values()) else 'needs_user',
            'settings': [{**json.loads(key), 'scan_count': value} for key, value in counts.items()], 'scans': selected}
