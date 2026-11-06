"""Exclude carbon anions consistently before candidate prediction and scoring."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation import pubchem, worker


class PubChemCarbonFilterTests(unittest.TestCase):
    def test_carbon_charge_is_independent_of_total_charge_and_other_elements(self):
        rejected = ['C[N+](C)(C)[C-]', 'C[N+](C)(C)[CH2-]', '[13CH3-:7]',
                    '[C-2]', '[C--]', '[c-]1ccccc1']
        retained = ['CCN(C)C', 'C[N+](C)(C)CC(=O)[O-]', '[Cl-]', '[Ca-]', '[CH3-0]']
        for smiles in rejected + retained:
            with self.subTest(smiles=smiles):
                self.assertEqual(pubchem.has_negatively_charged_carbon(smiles), smiles in rejected)

    def test_online_mass_and_formula_searches_exclude_net_neutral_carbon_anions(self):
        rows = [{'CID': i, 'MolecularFormula': 'C4H11N', 'SMILES': smiles, 'Charge': 0,
                 'MonoisotopicMass': '73.089149352', 'InChIKey': f'KEY-{i}'}
                for i, smiles in enumerate(['C[N+](C)(C)[CH2-]', 'CCN(C)C'], 1)]
        for mode in ('mass', 'formula'):
            responses = [{'IdentifierList': {'CID': [1, 2]}}, {'PropertyTable': {'Properties': rows}}]
            with self.subTest(mode=mode), patch.object(pubchem, '_get', side_effect=responses):
                precursor = 73.089149352 + pubchem.PROTON
                if mode == 'mass':
                    result = pubchem.mass_candidates(precursor)
                    self.assertEqual((result['eligible_cids'], result['excluded_cids']), (1, 1))
                    candidates = result['formulas'][0]['candidates']
                else:
                    candidates = pubchem.candidates_for_formula('C4H11N', precursor, '[M+H]+')['candidates']
                self.assertEqual([row['smiles'] for row in candidates], ['CCN(C)C'])


@unittest.skipUnless(importlib.util.find_spec('rdkit') and importlib.util.find_spec('ms_pred'),
                     'requires the scientific RDKit/ms-pred environment')
class ScientificCarbonFilterTests(unittest.TestCase):
    def test_proposals_record_rejection_even_when_formula_matches(self):
        rejected, retained = worker.validate_smiles(['C[N+](C)(C)[CH2-]', 'CCN(C)C'], 'C4H11N')
        self.assertEqual(rejected['formula'], 'C4H11N')
        self.assertTrue(rejected['negatively_charged_carbon'])
        self.assertFalse(rejected['formula_match'])
        self.assertTrue(retained['formula_match'])
        exact = worker.validate_smiles(['C[N+](C)(C)[C-]'], 'C4H9N')[0]
        self.assertTrue(exact['negatively_charged_carbon'])
        self.assertFalse(exact['formula_match'])
        oxygen = worker.validate_smiles(['C[N+](C)(C)CC(=O)[O-]'], 'C5H11NO2')[0]
        self.assertTrue(oxygen['formula_match'])

    def test_atlas_coverage_fallback_and_ranking_exclude_carbon_anions(self):
        from ms_pred import common
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spectrum, mgf = root / 'query.ms', root / 'atlas.mgf'
            precursor = common.formula_mass('C4H11N') + pubchem.PROTON
            spectrum.write_text(f'>compound query\n>parentmass {precursor}\n>ionization [M+H]+\n\n'
                                '>collision 20 eV\n50 10\n60 3\n')
            mgf.write_text(''.join(f'BEGIN IONS\nSMILES={smiles}\nCOLLISION_ENERGY={energy}\n'
                                  '50 10\n60 3\nEND IONS\n'
                                  for smiles, energy in [('C[N+](C)(C)[CH2-]', 40), ('CCN(C)C', 20)]))
            self.assertEqual(worker.atlas_smiles(str(mgf)), ['CCN(C)C'])
            info = worker.atlas_info(str(mgf), [20], str(spectrum), 'C4H11N', 'eV')
            self.assertEqual(info, {'library_structures': 1, 'missing_energies_ev': [], 'smiles': ['CCN(C)C']})
            with patch.object(worker, '_structure_image', return_value=None):
                ranked = worker.rank(str(spectrum), str(mgf), 'C4H11N', 10, 'eV')
            self.assertEqual((ranked['library_structures'], ranked['scored_structures']), (1, 1))
            self.assertEqual(ranked['missing_energies_ev'], [])
            self.assertEqual([row['smiles'] for row in ranked['candidates']], ['CCN(C)C'])

    @unittest.skipUnless(importlib.util.find_spec('h5py'), 'requires HDF5')
    def test_formula_index_accounts_for_rejected_candidates_and_caches_only_eligible_ones(self):
        import h5py
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
        from msms_structure_elucidation.pubchem_index import formula_candidates
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'formulae.hdf5'
            records = [[smiles, Chem.MolToInchiKey(Chem.MolFromSmiles(smiles))]
                       for smiles in ['C[N+](C)(C)[CH2-]', 'CCN(C)C']]
            with h5py.File(path, 'w') as handle:
                handle.create_dataset('C4H11N', data=[json.dumps(records).encode()])
            precursor = rdMolDescriptors.CalcExactMolWt(Chem.MolFromSmiles('CCN(C)C')) + pubchem.PROTON
            for _ in range(2):
                result = formula_candidates('C4H11N', str(path), precursor, '[M+H]+',
                                            prediction_cache=str(root / 'cache'))
                self.assertEqual(result['index_records'], 2)
                self.assertEqual(result['excluded_records'], {'negatively_charged_carbon': 1})
                self.assertEqual([row['smiles'] for row in result['candidates']], ['CCN(C)C'])
                self.assertTrue(all(row['formula_match'] for row in result['validated_structures']))


if __name__ == '__main__':
    unittest.main()
