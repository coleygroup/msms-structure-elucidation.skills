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

from msms_structure_elucidation.ions import MODEL_ADDUCTS, annotate


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


def _raw_scans(path: Path) -> dict[str, dict]:
    """MS2 scan tags from mzXML (energy, precursor m/z, retention time in s), without loading peaks."""
    found = {}
    for _, node in ET.iterparse(path, events=('end',)):
        if node.tag.rsplit('}', 1)[-1] == 'scan':
            if node.attrib.get('msLevel') == '2':
                precursor = next((child for child in node if child.tag.rsplit('}', 1)[-1] == 'precursorMz'), None)
                energy = node.attrib.get('collisionEnergy')
                if energy is None and precursor is not None:
                    energy = precursor.attrib.get('collisionEnergy')
                found[node.attrib['num']] = {
                    'energy': float(energy) if energy is not None else None,
                    'precursor_mz': _float(precursor.text) if precursor is not None else None,
                    'rt': _seconds(node.attrib.get('retentionTime'))}
            node.clear()
    return found


def _float(value: str | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seconds(duration: str | None) -> float | None:
    """mzXML retentionTime is an xs:duration such as PT298.7S or PT4.97M."""
    match = re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?', (duration or '').strip())
    if not match or not any(match.groups()):
        return None
    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)


def _precursor_matches(meta: dict, raw: dict[str, dict], rt_window: float) -> list[str]:
    """MS2 scans of this precursor near the feature retention time, for MGFs that name no MS2 scan."""
    precursor = _float((meta.get('PEPMASS') or '').split(' ')[0])
    rt = _float(meta.get('RTINSECONDS'))
    if precursor is None or rt is None:
        return []
    tolerance = max(0.01, precursor * 20e-6)
    return [num for num, scan in raw.items() if scan['precursor_mz'] is not None and scan['rt'] is not None
            and abs(scan['precursor_mz'] - precursor) <= tolerance and abs(scan['rt'] - rt) <= rt_window]


def _energy(meta: dict, raw: dict[str, dict], override: float | None,
            rt_window: float = 30.0) -> tuple[float | None, str, str | None, list[str]]:
    for field in ('COLLISION_ENERGY', 'COLLISIONENERGY', 'CE', 'NCE'):
        if meta.get(field):
            match = re.search(r'[-+]?\d+(?:\.\d+)?', meta[field])
            if match:
                label = meta[field].upper()
                header_unit = 'NCE' if field == 'NCE' or 'NCE' in label else 'eV' if 'EV' in label else None
                return float(match.group()), f'mgf:{field}', header_unit, []
    scans = sorted(set(re.findall(r'\d+', meta.get('MERGED_SCANS', '') or meta.get('MS2_SCAN', '') or meta.get('MS2_SCANS', ''))), key=int)
    source = 'mzXML:MS2_scan'
    if not scans:  # SOURCE_SCAN/SCANS may be the MS1 apex, so locate this precursor's MS2 scans instead
        scans, source = _precursor_matches(meta, raw, rt_window), 'mzXML:MS2_precursor_match'
    values = {raw[s]['energy'] for s in scans if s in raw and raw[s]['energy'] is not None}
    if len(values) == 1:
        return values.pop(), source, None, scans
    if override is not None:
        return override, 'user_override', None, scans
    return None, 'ambiguous_MS2_scans' if len(values) > 1 else 'missing_MS2_energy', None, scans


def _ion_identity(spectra: list, rt_window: float) -> dict[str, dict]:
    """Ion identity over every MS2 feature with a precursor m/z and retention time."""
    features = {}
    for index, (meta, peaks) in enumerate(spectra, 1):
        feature = meta.get('FEATURE_ID') or meta.get('SPECTRUMID') or meta.get('TITLE') or str(index)
        mz, rt = _float((meta.get('PEPMASS') or '').split(' ')[0]), _float(meta.get('RTINSECONDS'))
        if feature in features or mz is None or rt is None or meta.get('MSLEVEL', '2').strip() not in ('2', '2.0'):
            continue
        features[feature] = {'id': feature, 'mz': mz, 'rt': rt, 'peaks': peaks,
                             'negative': meta.get('CHARGE', '').strip().endswith('-')}
    return annotate(list(features.values()), rt_window=rt_window) if features else {}


def _raw_files(raw_mzxml) -> list[Path]:
    if raw_mzxml is None:
        return []
    files = []
    for item in ([raw_mzxml] if isinstance(raw_mzxml, (str, Path)) else raw_mzxml):
        item = Path(item)
        files.extend(sorted(p for p in item.iterdir() if p.suffix.lower() == '.mzxml') if item.is_dir() else [item])
    return files


def convert_mgf(input_path: Path, output_dir: Path, unit: str,
                energy: float | None = None, raw_mzxml: Path | list[Path] | None = None,
                instrument: str | None = None, rt_window: float = 30.0, feature_ids: set[str] | None = None,
                ion_identity: bool = True, ion_rt_window: float = 3.0,
                elucidate_all_ions: bool = False) -> list[dict]:
    """Convert MS2 entries to .ms files and a batch manifest.

    raw_mzxml: mzXML files or folders; an entry reads the file named by its SOURCE_FILE, or the only file given.
    feature_ids: convert only these features; ion identity still uses every feature in the MGF.
    ion_identity: derive each feature's adduct, and whether it is a multimer or in-source fragment, from
    co-eluting features (ions.annotate). In-source fragments, multimers and adducts the models do not
    support get their own manifest status unless elucidate_all_ions.
    """
    if unit not in ('NCE', 'eV'):
        raise ValueError('Confirm --collision-unit NCE or eV with the spectrum provider')
    output_dir.mkdir(parents=True, exist_ok=True)
    files = _raw_files(raw_mzxml)
    by_name, parsed = {f.name: f for f in files}, {}
    def raw_for(meta: dict) -> dict:
        path = by_name.get(Path(meta.get('SOURCE_FILE', '')).name) or (files[0] if len(files) == 1 else None)
        if path is not None and path not in parsed:
            parsed[path] = _raw_scans(path)
        return parsed.get(path, {})
    grouped = defaultdict(list)
    manifest = []
    spectra = list(entries(input_path))
    ions = _ion_identity(spectra, ion_rt_window) if ion_identity else {}
    for index, (meta, peaks) in enumerate(spectra, 1):
        try:
            level = int(float(meta.get('MSLEVEL', '2')))
        except ValueError:
            level = 0
        if level != 2 or not peaks:
            continue
        feature = meta.get('FEATURE_ID') or meta.get('SPECTRUMID') or meta.get('TITLE') or str(index)
        if feature_ids is not None and feature not in feature_ids:
            continue
        ce, provenance, header_unit, ms2_scans = _energy(meta, raw_for(meta), energy, rt_window)
        ion = ions.get(feature, {})
        default = '[M-H]-' if meta.get('CHARGE', '').strip().endswith('-') else '[M+H]+'
        stated = (meta.get('ADDUCT') or meta.get('ION') or meta.get('IONTYPE') or '').replace(' ', '')
        stated = stated.replace(']1+', ']+').replace(']1-', ']-')
        # A feature finder's stated adduct counts unless it is the default every feature gets.
        if stated and (stated != default or not ion.get('partners')):
            adduct, source = stated, 'mgf'
        else:
            adduct, source = ion.get('adduct') or default, 'ion_identity' if ion else 'default'
        base = {'feature_id': feature, 'mgf_entry': index, 'collision_unit': unit,
            'collision_energy': ce, 'energy_source': provenance,
            'header_collision_unit': header_unit, 'unit_conflict': bool(header_unit and header_unit != unit),
            'source_scan': meta.get('SOURCE_SCAN') or meta.get('SCANS'),
            'source_scan_role': 'feature_or_MS1_apex_not_MS2',
            'ms2_scans': meta.get('MERGED_SCANS') or meta.get('MS2_SCAN') or meta.get('MS2_SCANS') or ','.join(ms2_scans) or None,
            'parentmass': (meta.get('PEPMASS') or '').split()[0],
            'formula': meta.get('FORMULA'), 'instrument': instrument or meta.get('INSTRUMENT') or meta.get('INSTRUMENTATION'),
            'adduct': adduct, 'adduct_source': source,
            'ion_role': ion.get('role', 'molecule'), 'related_features': ';'.join(ion.get('related', [])),
            'adduct_partners': ';'.join(f"{p['id']}:{p['adduct']}" for p in ion.get('partners', [])),
            'ion_evidence': ion.get('evidence', '')}
        try:
            parentmass = float(base['parentmass'])
        except ValueError:
            parentmass = 0.0
        if base['ion_role'] != 'molecule' and not elucidate_all_ions:
            manifest.append({**base, 'status': base['ion_role'], 'ms_path': ''})
            continue
        if adduct not in MODEL_ADDUCTS and not elucidate_all_ions:
            manifest.append({**base, 'status': 'unsupported_adduct', 'ms_path': ''})
            continue
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
                f'>ionization {first["adduct"] if first["adduct"] in MODEL_ADDUCTS else "[M-H]-" if first["adduct"].endswith("-") else "[M+H]+"}']
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
            'ms2_scans', 'parentmass', 'formula', 'instrument', 'adduct', 'adduct_source', 'ion_role',
            'related_features', 'adduct_partners', 'ion_evidence']
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader(); writer.writerows(manifest)
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest
