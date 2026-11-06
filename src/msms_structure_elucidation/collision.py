"""Explicit collision-energy settings shared by preflight, conversion and scoring."""
from __future__ import annotations

import math
import re
import json


def energy_values(value: str | list[float]) -> list[float]:
    """Read explicit energy values without interpreting a range or center/spread."""
    if isinstance(value, str):
        if value.lstrip().startswith('['):
            decoded = json.loads(value)
            if not isinstance(decoded, list) or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in decoded):
                raise ValueError('Stepped JSON energies must be an explicit numeric list')
            return energy_values(decoded)
        if not re.fullmatch(r'\s*\d+(?:\.\d+)?(?:\s*[,;/ ]\s*\d+(?:\.\d+)?)*\s*', value):
            raise ValueError('Energy values must be an explicit list, not a range or center/spread')
        values = [float(x) for x in re.split(r'[,;/\s]+', value.strip())]
    else:
        values = [float(x) for x in value]
    if not values or any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError('Collision energies must be positive finite values')
    return values


def stepped_components(value: str | list[float], precursor: float, unit: str, nce_to_ev=None) -> dict:
    """Preserve every component, including repeated rounded energies and their weight."""
    values = energy_values(value)
    if len(values) < 2:
        raise ValueError('Stepped acquisition needs at least two component energies')
    if unit not in ('NCE', 'eV'):
        raise ValueError('Collision unit must be NCE or eV')
    convert = nce_to_ev or (lambda nce, mz: nce * mz / 500)
    ev = [round(float(convert(x, precursor) if unit == 'NCE' else x), 8) for x in values]
    model = [int(round(x)) for x in ev]
    if any(not math.isfinite(x) or x <= 0 for x in ev) or any(x <= 0 for x in model):
        raise ValueError('Each stepped component must convert to a positive model energy')
    from msms_structure_elucidation.config import settings
    aggregation = settings().get('pipeline', {}).get('stepped_aggregation', 'sum_then_base_peak_normalize')
    if aggregation not in ('sum_then_base_peak_normalize', 'arithmetic_mean'):
        raise ValueError('Unsupported stepped aggregation: ' + str(aggregation))
    weight = 1.0 / len(values) if aggregation == 'arithmetic_mean' else 1.0
    return {'values': values, 'unit': unit, 'energies_ev': ev, 'model_energies_ev': model,
            'merge_method': aggregation, 'weights': [weight] * len(values)}
