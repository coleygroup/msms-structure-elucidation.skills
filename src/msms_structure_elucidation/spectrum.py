"""Lightweight .ms preflight; ms-pred does scientific parsing and scoring."""
from __future__ import annotations

import math
from pathlib import Path


# Default precursor (MS1) and fragment (MS2) tolerances by mass analyzer, in ppm.
MASS_TOLERANCE_PPM = {'Q-TOF': (10.0, 20.0), 'Orbitrap': (5.0, 10.0)}


def mass_tolerance(instrumentation: str | None) -> dict:
    """Tolerances for the instrument named in the .ms header; unknown analyzers get the wider Q-TOF values."""
    text = (instrumentation or '').lower()
    if 'tof' in text:
        kind = 'Q-TOF'
    elif any(key in text for key in ('orbitrap', 'fticr', 'ft-icr', 'it-ft', 'q exactive', 'exploris')):
        kind = 'Orbitrap'
    else:
        kind = None
    ms1, ms2 = MASS_TOLERANCE_PPM[kind or 'Q-TOF']
    return {'instrument': kind or 'unknown', 'ms1_ppm': ms1, 'ms2_ppm': ms2}


def inspect_ms(path: Path, collision_unit: str) -> dict:
    """Read a spectrum using the user-confirmed collision-energy unit."""
    if collision_unit not in ('NCE', 'eV'):
        raise ValueError('Collision energy unit must be explicitly supplied as NCE or eV')
    metadata: dict[str, str] = {}
    raw_spectra: dict[str, list[list[float]]] = {}
    header_units: set[str] = set()
    current: str | None = None
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('>'):
            key, _, value = line[1:].partition(' ')
            if key.lower() == 'collision':
                parts = value.split()
                current = str(float(parts[0]))
                if len(parts) > 1:
                    header_units.add(parts[1])
                raw_spectra[current] = []
            else:
                metadata[key.lower()] = value
            continue
        if current is None:
            raise ValueError(f'{path}:{line_number}: peak before collision block')
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f'{path}:{line_number}: expected m/z and intensity')
        mz, intensity = map(float, parts[:2])
        if not all(map(math.isfinite, (mz, intensity))) or mz <= 0 or intensity < 0:
            raise ValueError(f'{path}:{line_number}: invalid peak')
        raw_spectra[current].append([mz, intensity])
    if not raw_spectra or not any(raw_spectra.values()):
        raise ValueError(f'{path}: no MS/MS peaks')
    parentmass = float(metadata['parentmass'])
    if not math.isfinite(parentmass) or parentmass <= 0:
        raise ValueError(f'{path}: invalid parentmass')
    spectra = {}
    energy_mapping = []
    for source, peaks in raw_spectra.items():
        ev = round(float(source) if collision_unit == 'eV' else float(source) * parentmass / 500, 8)
        if not math.isfinite(ev) or int(round(ev)) <= 0:
            raise ValueError(f'{path}: collision energy must convert to a positive finite integer eV')
        spectra[str(ev)] = peaks
        energy_mapping.append({'input_value': float(source), 'input_unit': collision_unit,
                               'collision_energy_ev': ev, 'model_energy_ev': int(round(ev))})
    return {'metadata': metadata, 'spectra': spectra, 'parentmass': parentmass,
            'adduct': metadata.get('ionization', '[M+H]+'),
            'peaks': sum(map(len, spectra.values())), 'collision_unit': collision_unit,
            'header_units': sorted(header_units), 'energy_mapping': energy_mapping}
