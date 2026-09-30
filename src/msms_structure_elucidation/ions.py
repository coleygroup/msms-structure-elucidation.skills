"""Ion identity of LC-MS features from MS1 co-elution: adducts, multimers and in-source fragments.

Feature finders such as GNPS export every feature as [M+H]+ by default, and their in-source
fragment flags can point the wrong way (a monomer flagged as a fragment of the dimer it forms).
This module re-derives each feature's ion from the features eluting with it:

- adducts: co-eluting features whose neutral masses agree under different adducts, such as
  m/z 505.333 [M+H]+ with 527.316 [M+Na]+ and 522.360 [M+NH4]+;
- multimers: a neutral mass equal to the sum of two co-eluting neutral masses ([2M+H]+ or a
  heterodimer [M1+M2+H]+), where a monomer is shown to be a molecule by its own adduct partner;
- in-source fragments: a lighter feature without adduct partners whose m/z is a peak in the MS2
  spectrum of a heavier co-eluting molecule, at a plausible neutral loss.

Two complementary in-source fragments add up to their precursor exactly like the monomers of a
heterodimer, so mass alone cannot tell them apart; the adduct-partner requirement does.
"""
from __future__ import annotations

PROTON = 1.007276
WATER = 18.010565
SODIUM = 22.989218
POSITIVE = {'[M+H]+': PROTON, '[M+Na]+': SODIUM, '[M+NH4]+': 18.033823, '[M+K]+': 38.963158,
            '[M+2Na-H]+': 2 * SODIUM - PROTON, '[M+H+HCOONa]+': PROTON + 67.987424,  # sodium formate cluster
            '[M+H-H2O]+': PROTON - WATER}
NEGATIVE = {'[M-H]-': -PROTON, '[M+Cl]-': 34.969402, '[M+CHO2]-': 44.998201, '[M-H-H2O]-': -PROTON - WATER}
PRIORITY = ['[M+H]+', '[M-H]-', '[M+Na]+', '[M+NH4]+', '[M+Cl]-', '[M+CHO2]-', '[M+K]+',
            '[M+2Na-H]+', '[M+H+HCOONa]+', '[M+H-H2O]+', '[M-H-H2O]-']
# Adducts ms-pred models (ICEBERG, GLACIER) accept; the public atlas holds [M+H]+ only.
MODEL_ADDUCTS = {'[M+H]+', '[M+Na]+', '[M+K]+', '[M+NH4]+', '[M+H-H2O]+', '[M-H]-', '[M+Cl]-', '[M+CHO2]-', '[M-H-H2O]-'}
LOSSES = {'[M+H-H2O]+', '[M-H-H2O]-'}  # in-source losses, not evidence that a feature is a molecule


def _plausible_loss(mass: float) -> bool:
    """An organic neutral loss (CH4, NH3 and H2O are the smallest common ones) with a mass defect
    between about -0.1 and +0.3 Da. Smaller differences lie inside the isolation window, where a
    peak is more likely a co-isolated ion than a fragment."""
    defect = mass - round(mass)
    return mass >= 14 and -0.1 <= defect <= 0.3


def annotate(features: list[dict], rt_window: float = 3.0, ppm: float = 10.0,
             min_da: float = 0.003, fragment_min_relative: float = 0.01) -> dict[str, dict]:
    """Assign an ion to every feature.

    features: dicts with 'id', 'mz', 'rt' (seconds), 'negative' (bool) and optional 'peaks'
    [(mz, intensity), ...] from the feature's MS2 spectrum.
    Returns {id: {'adduct', 'role', 'partners', 'related', 'evidence'}}. role is 'molecule',
    'multimer' or 'in_source_fragment'; 'partners' lists co-eluting ions of the same molecule as
    {'id', 'adduct'}; 'related' names a multimer's monomers or a fragment's parent.
    """
    tol = lambda mass: max(min_da, abs(mass) * ppm * 1e-6)
    by_id = {f['id']: f for f in features}
    table = {f['id']: NEGATIVE if f.get('negative') else POSITIVE for f in features}
    ordered = sorted(features, key=lambda f: f['rt'])
    near = {f['id']: [] for f in features}
    for i, f in enumerate(ordered):
        for g in ordered[i + 1:]:
            if g['rt'] - f['rt'] > rt_window:
                break
            if bool(f.get('negative')) == bool(g.get('negative')):
                near[f['id']].append(g['id']); near[g['id']].append(f['id'])

    # Adduct groups: each (feature, adduct) hypothesis gathers co-eluting features reaching the same
    # neutral mass under another adduct; larger groups, then more common adducts, are accepted first.
    hypotheses = []
    for f in features:
        for adduct, shift in table[f['id']].items():
            neutral = f['mz'] - shift
            members = {f['id']: adduct}
            for gid in near[f['id']]:
                for other, other_shift in table[gid].items():
                    if other != adduct and abs(by_id[gid]['mz'] - other_shift - neutral) <= tol(neutral):
                        members[gid] = other
                        break
            if len(members) > 1:
                hypotheses.append((-len(members), sorted(PRIORITY.index(a) for a in members.values()),
                                   f['id'], neutral, members))
    hypotheses.sort(key=lambda h: (h[0], h[1], h[2]))
    result, neutral = {}, {}
    for _, _, _, mass, members in hypotheses:
        if any(fid in result for fid in members):
            continue
        for fid, adduct in members.items():
            result[fid] = {'adduct': adduct, 'role': 'molecule', 'related': [],
                           'partners': [{'id': g, 'adduct': a} for g, a in members.items() if g != fid],
                           'evidence': 'co-eluting adducts of neutral mass %.4f' % mass}
            neutral[fid] = mass
    for f in features:
        if f['id'] not in result:
            adduct = '[M-H]-' if f.get('negative') else '[M+H]+'
            result[f['id']] = {'adduct': adduct, 'role': 'molecule', 'partners': [], 'related': [],
                               'evidence': 'no co-eluting adduct partner; default adduct'}
            neutral[f['id']] = f['mz'] - table[f['id']][adduct]
    # Only a true adduct partner shows that a feature is an intact molecule.
    confirmed = {fid for fid, r in result.items()
                 if any(p['adduct'] not in LOSSES for p in r['partners']) and r['adduct'] not in LOSSES}
    confirmed |= {fid for fid, r in result.items() if any(p['id'] in confirmed for p in r['partners'])}

    def sum_of(fid, allowed):
        """Two co-eluting neutral masses (possibly the same) adding up to this feature's, one confirmed."""
        mass = neutral[fid]
        others = [(g, neutral[g]) for g in near[fid] if g in allowed and neutral[g] < mass - 1]
        for i, (a, ma) in enumerate(others):
            for b, mb in others[i:]:
                if abs(ma + mb - mass) <= tol(mass) and (a in confirmed or b in confirmed):
                    return sorted({a, b})
        return None

    # In-source fragments, heaviest first so that a fragment is never chosen as a parent.
    for f in sorted(features, key=lambda f: -f['mz']):
        fid = f['id']
        if fid in confirmed or result[fid]['adduct'] in LOSSES and result[fid]['partners']:
            continue
        best = None
        for gid in near[fid]:
            parent = by_id[gid]
            if result[gid]['role'] != 'molecule' or not parent.get('peaks') \
                    or not _plausible_loss(parent['mz'] - f['mz']) or sum_of(gid, set(by_id)):
                continue
            top = max(i for _, i in parent['peaks'])
            share = max((i / top for mz, i in parent['peaks'] if abs(mz - f['mz']) <= tol(f['mz'])), default=0)
            if share >= fragment_min_relative and (best is None or share > best[1]):
                best = (gid, share)
        if best:
            for member in [fid] + [p['id'] for p in result[fid]['partners']]:
                result[member].update(role='in_source_fragment', related=[best[0]],
                                      evidence='m/z %.4f is a %.0f%% peak in the MS2 of co-eluting %s'
                                               % (f['mz'], 100 * best[1], best[0]))

    # Multimers of intact molecules.
    molecules = {fid for fid, r in result.items() if r['role'] == 'molecule'}
    for f in features:
        monomers = sum_of(f['id'], molecules)
        if monomers and result[f['id']]['role'] == 'molecule':
            result[f['id']].update(role='multimer', related=monomers,
                                   evidence='neutral mass equals ' + (' + '.join(monomers) if len(monomers) > 1
                                                                      else '2 x ' + monomers[0]))
    return result
