"""Offline formula lookup preserves structures, mass filtering and snapshot provenance."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.pubchem_index import formula_candidates
from msms_structure_elucidation.ions import PROTON, SODIUM


@unittest.skipUnless(importlib.util.find_spec('h5py') and importlib.util.find_spec('rdkit'),
                     'requires the scientific HDF5/RDKit environment')
class FormulaIndexTests(unittest.TestCase):
    def setUp(self):
        import h5py
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'formulae.hdf5'
        self.mass = rdMolDescriptors.CalcExactMolWt(Chem.MolFromSmiles('CCO'))
        records = [[smi, Chem.MolToInchiKey(Chem.MolFromSmiles(smi))] for smi in ['CCO', 'COC', 'CCO']]
        records.append(['[2H]OCC', records[0][1]])
        with h5py.File(self.path, 'w') as handle:
            handle.create_dataset('C2H6O', data=[json.dumps(records).encode()])

    def lookup(self, **options):
        return formula_candidates('C2H6O', str(self.path), self.mass + PROTON,
                                  '[M+H]+', prediction_cache=str(self.root / 'cache'), **options)

    def test_returns_all_snapshot_structures_with_keys_and_exclusions(self):
        result = self.lookup()
        self.assertEqual({row['smiles'] for row in result['candidates']}, {'CCO', 'COC'})
        self.assertEqual(result['index_records'], 4)
        self.assertEqual(result['excluded_records']['charge_component_or_isotope'], 1)
        self.assertTrue(all(row['pubchem_inchikeys'] for row in result['candidates']))
        self.assertTrue(all(row['pubchem_cids'] == [] for row in result['candidates']))
        self.assertEqual(len(result['formula_index_sha256']), 64)
        self.assertEqual(result['coverage_scope'], 'configured PubChem index snapshot')

    def test_warm_lookup_reuses_validation_and_still_checks_each_precursor(self):
        self.lookup()
        with patch('h5py.h5f.open', side_effect=AssertionError('warm lookup reopened HDF5')):
            self.assertEqual(len(self.lookup()['candidates']), 2)
        with self.assertRaisesRegex(ValueError, 'precursor mismatch'):
            formula_candidates('C2H6O', str(self.path), self.mass + SODIUM, '[M+H]+',
                               prediction_cache=str(self.root / 'cache'))

    def test_missing_formula_is_distinct_from_an_empty_candidate_result(self):
        result = formula_candidates('C99H200', str(self.path), 1400., '[M+H]+',
                                    prediction_cache=str(self.root / 'cache'))
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
