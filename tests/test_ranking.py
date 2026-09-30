import copy
import random
import unittest
from types import SimpleNamespace

from msms_structure_elucidation.worker import _explained, _explained_python, sort_candidates


def reference_sort(candidates, tie_band=0.03):
    """Previous quadratic implementation, kept as the specification."""
    ordered = sorted(candidates, key=lambda c: c['entropy_similarity'], reverse=True)
    result = []
    while ordered:
        anchor = ordered[0]['entropy_similarity']
        band = [c for c in ordered if anchor - c['entropy_similarity'] <= tie_band]
        ordered = ordered[len(band):]
        result.extend(sorted(band, key=lambda c: (c['explained_intensity'], c['entropy_similarity']), reverse=True))
    for index, a in enumerate(result):
        a['rank'] = index + 1
        a['ambiguity'] = []
        a_peaks = {(p['ce'], round(p['mz'], 4)) for p in a.get('matched_peaks', [])}
        for b in result[:index]:
            b_peaks = {(p['ce'], round(p['mz'], 4)) for p in b.get('matched_peaks', [])}
            if a.get('formula') == b.get('formula') and a_peaks == b_peaks and \
                    abs(a['entropy_similarity'] - b['entropy_similarity']) <= tie_band:
                a['ambiguity'].append(f"No diagnostic matched peaks versus rank {b['rank']}; "
                                      'positional isomers may be indistinguishable')
                break
    return result


def reference_explained(experimental, predicted, alignment, ppm=10):
    matched, total, covered = [], 0.0, 0.0
    for pair in alignment:
        ce = pair['experimental_key']
        a, b = experimental[ce], predicted[pair['atlas_key']]
        masses = list(map(float, b.masses))
        for mz, intensity in zip(a.masses, a.intens):
            mz, intensity = float(mz), float(intensity)
            total += intensity
            hits = [p for p in masses if abs(p - mz) <= max(0.002, mz * ppm * 1e-6)]
            if hits:
                covered += intensity
                matched.append({'ce': ce, 'mz': mz, 'predicted_mz': min(hits, key=lambda p: abs(p - mz))})
    return (covered / total if total else 0.0), matched


class RankingEquivalenceTest(unittest.TestCase):
    def test_sort_matches_reference_with_ties_and_shared_peaks(self):
        rng = random.Random(7)
        peak_sets = [[], [{'ce': '20', 'mz': 91.0542}], [{'ce': '20', 'mz': 91.05421}, {'ce': '40', 'mz': 65.039}]]
        for trial in range(30):
            candidates = [{'smiles': f'C{i}', 'formula': rng.choice(['C7H8', 'C7H8', 'C6H6']),
                           'entropy_similarity': round(rng.choice([rng.random(), 0.5, 0.52, 0.55]), 3),
                           'explained_intensity': rng.choice([0.2, 0.4, rng.random()]),
                           'matched_peaks': rng.choice(peak_sets)} for i in range(rng.randint(1, 120))]
            expected = reference_sort(copy.deepcopy(candidates))
            actual = sort_candidates(copy.deepcopy(candidates))
            self.assertEqual([(c['smiles'], c['rank'], c['ambiguity']) for c in actual],
                             [(c['smiles'], c['rank'], c['ambiguity']) for c in expected])

    def test_explained_matches_reference(self):
        rng = random.Random(3)
        for trial in range(50):
            exp_masses = sorted(rng.uniform(50, 300) for _ in range(rng.randint(0, 30)))
            pred_masses = [m + rng.choice([0, 0.0005, 0.001, 0.01, 1]) for m in exp_masses if rng.random() < 0.7]
            pred_masses += [rng.uniform(50, 300) for _ in range(rng.randint(0, 20))]
            rng.shuffle(pred_masses)
            experimental = {'e': SimpleNamespace(masses=exp_masses, intens=[rng.random() for _ in exp_masses])}
            predicted = {'p': SimpleNamespace(masses=pred_masses)}
            alignment = [{'experimental_key': 'e', 'atlas_key': 'p'}]
            expected_fraction, expected_peaks = reference_explained(experimental, predicted, alignment)
            # _explained uses numpy when installed; the pure-Python fallback runs without it (as in CI).
            for explained in (_explained, _explained_python):
                fraction, peaks = explained(experimental, predicted, alignment)
                self.assertAlmostEqual(fraction, expected_fraction, places=12)
                self.assertEqual(len(peaks), len(expected_peaks))
                for got, want in zip(peaks, expected_peaks):
                    self.assertEqual(got['ce'], want['ce'])
                    self.assertAlmostEqual(got['mz'], want['mz'], places=12)
                    self.assertAlmostEqual(abs(got['predicted_mz'] - got['mz']), abs(want['predicted_mz'] - want['mz']), places=12)


if __name__ == '__main__':
    unittest.main()
