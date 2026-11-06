"""Exact native merge parity and bounded fragment decoding on temporary components."""
import unittest
from unittest.mock import patch

from msms_structure_elucidation import worker


class SteppedMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from ms_pred import common
        except ImportError:
            raise unittest.SkipTest('native ms-pred environment required')
        cls.common = common

    def prediction(self, dtype, fragments=True):
        import numpy as np
        masks = np.zeros((5, 100), dtype=bool)
        masks[0, [0, 99]] = True
        masks[1, 0] = True
        masks[2] = masks[0]
        masks[3, 1] = True
        masks[4] = masks[0]
        return self.common.CompositeMassSpec([
            self.common.MassSpec(energy, root_canonical_smiles='CCO', adduct='[M+H]+',
                masses=np.array([50.12341, 50.12342, 50.12341, 60., 70.], dtype=dtype),
                intens=np.array(intensities, dtype=dtype), frags=masks.copy() if fragments else None)
            for energy, intensities in [(10, [.1, .2, .3, 0, .4]), (20, [.7, .1, .2, .8, 0])]])

    def native_baseline(self, prediction, energies):
        """Use native copies and merge to reproduce the independent pre-optimization calculation."""
        components = []
        for key in dict.fromkeys(str(energy) for energy in energies):
            spec = prediction[key]
            keep = spec.intens > 0
            if keep.any():
                components.append(self.common.MassSpec(float(key), root_canonical_smiles=spec.root_canonical_smiles,
                    adduct=spec.adduct, masses=spec.masses[keep].copy(), intens=spec.intens[keep].copy(),
                    frags=spec.frags[keep].copy() if spec.frags is not None else None))
        return self.common.CompositeMassSpec(components).merge_spectra(
            energies=[str(energy) for energy in energies], merge_method='sum')

    def test_exact_arrays_fragments_and_scores_with_wide_masks_and_mass_collisions(self):
        import numpy as np
        for dtype in (np.float32, np.float64):
            for fragments in (False, True):
                with self.subTest(dtype=dtype, fragments=fragments):
                    predicted = self.prediction(dtype, fragments)
                    before = worker._serial_spec(predicted), worker._serial_fragment_ids(predicted)
                    source = {'stepped': {'model_energies_ev': [10, 10, 20]}}
                    expected = self.native_baseline(predicted, [10, 10, 20])
                    result = worker._scoring_prediction(predicted, source)
                    merged = result['stepped']
                    self.assertIs(type(merged), self.common.MassSpec)
                    self.assertEqual(merged.masses.dtype, expected.masses.dtype)
                    self.assertEqual(merged.intens.dtype, expected.intens.dtype)
                    np.testing.assert_array_equal(merged.masses, expected.masses)
                    np.testing.assert_array_equal(merged.intens, expected.intens)
                    np.testing.assert_array_equal(merged.frags, expected.frags)
                    self.assertEqual(worker._serial_fragment_ids({'stepped': merged}),
                                     worker._serial_fragment_ids({'stepped': expected}))
                    experimental = {'stepped': self.common.MassSpec(10,
                        masses=np.array([50.12341, 60., 75.]), intens=np.array([1., .5, .3]))}
                    alignment = [{'atlas_key': 'stepped', 'experimental_key': 'stepped'}]
                    baseline = {'stepped': expected}
                    self.assertEqual(worker._score(experimental, result, alignment, 100),
                                     worker._score(experimental, baseline, alignment, 100))
                    self.assertEqual(worker._explained(experimental, result, alignment),
                                     worker._explained(experimental, baseline, alignment))
                    self.assertEqual(before, (worker._serial_spec(predicted), worker._serial_fragment_ids(predicted)))
                    self.assertIs(result['10'], predicted['10'])

    def test_decode_once_per_component_without_global_patch_or_stale_cache(self):
        import numpy as np
        predicted = self.prediction(np.float32)
        native_property = self.common.MassSpec.int_frags
        calls = []

        def decode(spec):
            calls.append(spec)
            return native_property.fget(spec)

        source = {'stepped': {'model_energies_ev': [10, 10, 20]}}
        with patch.object(self.common.MassSpec, 'int_frags', property(decode)):
            first = worker._scoring_prediction(predicted, source)
            self.assertEqual(len(calls), 2)
            predicted['10'].frags[0, 50] = True
            second = worker._scoring_prediction(predicted, source)
            self.assertEqual(len(calls), 4)
        self.assertIs(self.common.MassSpec.int_frags, native_property)
        self.assertNotEqual(worker._serial_fragment_ids({'stepped': first['stepped']}),
                            worker._serial_fragment_ids({'stepped': second['stepped']}))

    def test_explicit_arithmetic_mean_preserves_native_summation_order(self):
        import numpy as np
        predicted = self.prediction(np.float32)
        union = {}
        for key in ('10', '10', '20'):
            spec = predicted[key]
            keep = spec.intens > 0
            component = self.common.MassSpec(float(key), root_canonical_smiles=spec.root_canonical_smiles,
                adduct=spec.adduct, masses=spec.masses[keep].copy(), intens=spec.intens[keep].copy(),
                frags=spec.frags[keep].copy())
            union = component._merge_spec_to_tup(union, merge_method='sum')
        values = list(union.values())
        result = worker._scoring_prediction(predicted, {'stepped': {
            'model_energies_ev': [10,10,20], 'merge_method': 'arithmetic_mean'}})['stepped']
        np.testing.assert_array_equal(result.masses, [v[0] for v in values])
        np.testing.assert_array_equal(result.intens, [v[1] / 3 for v in values])
        np.testing.assert_array_equal(result.frags, np.stack([v[2] for v in values]))

    def test_empty_invalid_and_missing_components_keep_existing_validation(self):
        import numpy as np
        source = {'stepped': {'model_energies_ev': [10, 20]}}
        predicted = self.prediction(np.float64)
        predicted['10'].intens[:] = 0
        merged = worker._scoring_prediction(predicted, source)['stepped']
        np.testing.assert_array_equal(merged.intens,
            self.native_baseline(predicted, [20]).intens)
        for invalid in (-1, float('nan'), float('inf')):
            predicted['20'].intens[0] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'Invalid stepped'):
                worker._scoring_prediction(predicted, source)
        predicted['20'].intens[:] = 0
        with self.assertRaisesRegex(ValueError, 'no positive intensity'):
            worker._scoring_prediction(predicted, source)
        with self.assertRaisesRegex(ValueError, 'Missing stepped'):
            worker._scoring_prediction(predicted, {'stepped': {'model_energies_ev': [30]}})
        self.assertIs(worker._scoring_prediction(predicted, {}), predicted)


if __name__ == '__main__':
    unittest.main()
