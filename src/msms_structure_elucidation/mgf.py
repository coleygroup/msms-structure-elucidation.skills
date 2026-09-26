"""Conservative GNPS/MZmine MGF to ms-pred .ms conversion."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def entries(path: Path):
    meta, peaks = None, None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.upper() == 'BEGIN IONS':
            meta, peaks = {}, []
        elif line.upper() == 'END IONS':
            if meta is not None:
                yield meta, peaks
            meta, peaks = None, None
        elif meta is not None and line:
            if '=' in line:
                key, value = line.split('=', 1)
                meta[key.strip().upper()] = value.strip()
            else:
                bits = line.split()
                if len(bits) >= 2:
                    try:
                        peaks.append((float(bits[0]), float(bits[1])))
                    except ValueError:
                        pass


def _raw_energies(path: Path) -> dict[str, float]:
    """Read explicit MS2 collisionEnergy from mzXML scan tags, without loading peaks."""
    found = {}
    for _, node in ET.iterparse(path, events=('end',)):
        if node.tag.rsplit('}', 1)[-1] == 'scan':
            if node.attrib.get('msLevel') == '2':
                energy = node.attrib.get('collisionEnergy')
                if energy is None:
                    precursor = next((child for child in node if child.tag.rsplit('}', 1)[-1] == 'precursorMz'), None)
                    energy = precursor.attrib.get('collisionEnergy') if precursor is not None else None
                if energy is not None:
                    found[node.attrib['num']] = float(energy)
            node.clear()
    return found


def _energy(meta: dict, raw: dict[str, float], override: float | None) -> tuple[float | None, str, str | None]:
    for field in ('COLLISION_ENERGY', 'COLLISIONENERGY', 'CE', 'NCE'):
        if meta.get(field):
            match = re.search(r'[-+]?\d+(?:\.\d+)?', meta[field])
            if match:
                label = meta[field].upper()
                header_unit = 'NCE' if field == 'NCE' or 'NCE' in label else 'eV' if 'EV' in label else None
                return float(match.group()), f'mgf:{field}', header_unit
    scans = set(re.findall(r'\d+', meta.get('MERGED_SCANS', '') or meta.get('MS2_SCAN', '') or meta.get('MS2_SCANS', '')))
    values = {raw[s] for s in scans if s in raw}
    if len(values) == 1:
        return values.pop(), 'mzXML:MS2_scan', None
    if override is not None:
        return override, 'user_override', None
    return None, 'ambiguous_MS2_scans' if len(values) > 1 else 'missing_MS2_energy', None


def convert_mgf(input_path: Path, output_dir: Path, unit: str,
                energy: float | None = None, raw_mzxml: Path | None = None,
                instrument: str | None = None) -> list[dict]:
    if unit not in ('NCE', 'eV'):
        raise ValueError('Confirm --collision-unit NCE or eV with the spectrum provider')
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = _raw_energies(raw_mzxml) if raw_mzxml else {}
    grouped = defaultdict(list)
    manifest = []
    for index, (meta, peaks) in enumerate(entries(input_path), 1):
        try:
            level = int(float(meta.get('MSLEVEL', '2')))
        except ValueError:
            level = 0
        if level != 2 or not peaks:
            continue
        feature = meta.get('FEATURE_ID') or meta.get('SPECTRUMID') or meta.get('TITLE') or str(index)
        ce, provenance, header_unit = _energy(meta, raw, energy)
        base = {'feature_id': feature, 'mgf_entry': index, 'collision_unit': unit,
            'collision_energy': ce, 'energy_source': provenance,
            'header_collision_unit': header_unit, 'unit_conflict': bool(header_unit and header_unit != unit),
            'source_scan': meta.get('SOURCE_SCAN') or meta.get('SCANS'),
            'source_scan_role': 'feature_or_MS1_apex_not_MS2',
            'ms2_scans': meta.get('MERGED_SCANS') or meta.get('MS2_SCAN') or meta.get('MS2_SCANS'),
            'parentmass': (meta.get('PEPMASS') or '').split()[0],
            'formula': meta.get('FORMULA'), 'instrument': instrument or meta.get('INSTRUMENT') or meta.get('INSTRUMENTATION')}
        try:
            parentmass = float(base['parentmass'])
        except ValueError:
            parentmass = 0.0
        if ce is None or not math.isfinite(ce) or ce <= 0 or not math.isfinite(parentmass) or parentmass <= 0:
            manifest.append({**base, 'status': 'needs_energy' if ce is None else 'needs_parentmass', 'ms_path': ''})
            continue
        grouped[feature].append((base, peaks, meta))
    for feature, records in grouped.items():
        masses = [float(row[0]['parentmass']) for row in records]
        if max(masses) - min(masses) > max(0.01, min(masses) * 20e-6):
            manifest.extend({**row, 'status': 'inconsistent_precursor', 'ms_path': ''} for row, _, _ in records)
            continue
        charges = {meta['CHARGE'] for _, _, meta in records if meta.get('CHARGE')}
        if len(charges) > 1:
            manifest.extend({**row, 'status': 'inconsistent_charge', 'ms_path': ''} for row, _, _ in records)
            continue
        blocks = defaultdict(list)
        for row in records:
            blocks[row[0]['collision_energy']].append(row)
        replicates = max(map(len, blocks.values()))
        for replicate in range(replicates):
            selected = [rows[replicate] for rows in blocks.values() if replicate < len(rows)]
            if not selected:
                continue
            safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', feature)[:100]
            if safe != feature:
                safe += '__' + hashlib.sha256(feature.encode()).hexdigest()[:8]
            path = output_dir / f'{safe}{f"_rep{replicate+1}" if replicates > 1 else ""}.ms'
            first = selected[0][0]
            lines = [f'>compound {feature}', f'>parentmass {first["parentmass"]}',
                f'>ionization {"[M-H]-" if selected[0][2].get("CHARGE", "").endswith("-") else "[M+H]+"}']
            if first['formula']:
                lines.append(f'>formula {first["formula"]}')
            if first['instrument']:
                lines.append(f'>instrumentation {first["instrument"]}')
            for row, peaks, _ in sorted(selected, key=lambda item: item[0]['collision_energy']):
                lines.extend(['', f'>collision {row["collision_energy"]} {unit}'])
                lines.extend(f'{mz} {intensity}' for mz, intensity in peaks)
                manifest.append({**row, 'status': 'ready', 'ms_path': str(path.resolve())})
            path.write_text('\n'.join(lines) + '\n')
    with (output_dir / 'manifest.csv').open('w', newline='') as file:
        columns = ['feature_id', 'mgf_entry', 'status', 'ms_path', 'collision_unit', 'collision_energy',
            'energy_source', 'header_collision_unit', 'unit_conflict', 'source_scan', 'source_scan_role',
            'ms2_scans', 'parentmass', 'formula', 'instrument']
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader(); writer.writerows(manifest)
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest
