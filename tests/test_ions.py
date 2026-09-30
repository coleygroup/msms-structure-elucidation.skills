import csv
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation.ions import POSITIVE, annotate
from msms_structure_elucidation.mgf import convert_mgf

H, NA, NH4 = POSITIVE['[M+H]+'], POSITIVE['[M+Na]+'], POSITIVE['[M+NH4]+']


def feature(fid, mz, rt=100.0, peaks=None):
    return {'id': fid, 'mz': mz, 'rt': rt, 'negative': False, 'peaks': peaks or [(50.0, 1.0)]}


class IonIdentityTests(unittest.TestCase):
    def test_co_eluting_adducts_share_one_neutral_mass(self):
        # Rhamnolipid Rha-C10-C10 (504.3298) as [M+H]+, [M+Na]+ and [M+NH4]+; a later ion is unrelated.
        m = 504.3298
        ions = annotate([feature('h', m + H), feature('na', m + NA), feature('nh4', m + NH4),
                         feature('late', m + NA, rt=200.0)])
        self.assertEqual({k: ions[k]['adduct'] for k in ('h', 'na', 'nh4', 'late')},
                         {'h': '[M+H]+', 'na': '[M+Na]+', 'nh4': '[M+NH4]+', 'late': '[M+H]+'})
        self.assertEqual({p['id'] for p in ions['na']['partners']}, {'h', 'nh4'})
        self.assertEqual(ions['late']['partners'], [])

    def test_dimer_of_confirmed_molecules_is_a_multimer(self):
        a, b = 271.1936, 269.1780  # NHQ and C9:1-HQ, each with its own sodium adduct
        ions = annotate([feature('a', a + H), feature('a_na', a + NA), feature('b', b + H),
                         feature('b_na', b + NA), feature('ab', a + b + H), feature('aa', 2 * a + H)])
        self.assertEqual((ions['ab']['role'], ions['ab']['related']), ('multimer', ['a', 'b']))
        self.assertEqual((ions['aa']['role'], ions['aa']['related']), ('multimer', ['a']))
        self.assertEqual(ions['a']['role'], 'molecule')

    def test_complementary_fragments_are_not_mistaken_for_a_dimer(self):
        # A precursor's two complementary fragment ions add up to it exactly like dimer monomers do.
        m = 650.3875
        frag1, frag2 = 359.2792, m + 2 * H - 359.2792
        parent_peaks = [(frag1, 30.0), (frag2, 100.0), (m + H, 10.0)]
        ions = annotate([feature('p', m + H, peaks=parent_peaks), feature('p_na', m + NA),
                         feature('f1', frag1), feature('f2', frag2)])
        self.assertEqual(ions['p']['role'], 'molecule')
        self.assertEqual({ions['f1']['role'], ions['f2']['role']}, {'in_source_fragment'})
        self.assertEqual(ions['f1']['related'], ['p'])

    def test_in_source_fragment_needs_a_plausible_co_eluting_parent(self):
        parent = feature('p', 300.2310, peaks=[(298.2154, 5.0), (256.2, 100.0), (180.0, 2.0)])
        ions = annotate([parent, feature('h2', 298.2154),  # 2 Da: inside the isolation window
                         feature('real', 256.2),  # plausible 44 Da loss, 100% peak
                         feature('far', 180.0, rt=110.0)])  # not co-eluting
        self.assertEqual(ions['h2']['role'], 'molecule')
        self.assertEqual((ions['real']['role'], ions['real']['related']), ('in_source_fragment', ['p']))
        self.assertEqual(ions['far']['role'], 'molecule')

    def test_sodium_formate_cluster_groups_with_its_molecule(self):
        m = 807.5752
        ions = annotate([feature('h', m + H), feature('cluster', m + H + 67.987424,
                                                       peaks=[(m + H, 100.0)])])
        self.assertEqual(ions['cluster']['adduct'], '[M+H+HCOONa]+')
        self.assertEqual(ions['h']['role'], 'molecule')


class ConvertIonTests(unittest.TestCase):
    def test_converter_writes_adducts_and_skips_fragments_and_multimers(self):
        m, a = 504.3298, 271.1936
        entry = 'BEGIN IONS\nFEATURE_ID={}\nPEPMASS={:.4f}\nRTINSECONDS=100\nCHARGE=1+\nMSLEVEL=2\nCOLLISION_ENERGY=30\n{}END IONS\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgf = root / 'features.mgf'
            mgf.write_text(entry.format(1, m + H, '359.2792 20\n200 100\n') + entry.format(2, m + NA, '50 1\n')
                           + entry.format(3, 359.2792, '50 1\n') + entry.format(4, a + H, '50 1\n')
                           + entry.format(5, a + NA, '50 1\n') + entry.format(6, 2 * a + H, '50 1\n'))
            ids = root / 'ids.csv'; ids.write_text('feature_id\n2\n3\n6\n')
            from msms_structure_elucidation.cli import _feature_ids
            rows = {r['feature_id']: r for r in convert_mgf(mgf, root / 'out', 'eV', feature_ids=_feature_ids(ids))}
            self.assertEqual(set(rows), {'2', '3', '6'})
            self.assertEqual((rows['2']['status'], rows['2']['adduct'], rows['2']['adduct_partners']),
                             ('ready', '[M+Na]+', '1:[M+H]+'))
            self.assertIn('>ionization [M+Na]+', Path(rows['2']['ms_path']).read_text())
            self.assertEqual((rows['3']['status'], rows['3']['related_features']), ('in_source_fragment', '1'))
            self.assertEqual((rows['6']['status'], rows['6']['related_features']), ('multimer', '4'))
            with (root / 'out' / 'manifest.csv').open() as file:
                self.assertIn('ion_evidence', next(csv.reader(file)))
            everything = convert_mgf(mgf, root / 'all', 'eV', feature_ids={'3'}, elucidate_all_ions=True)
            self.assertEqual(everything[0]['status'], 'ready')

    def test_stated_non_default_adduct_wins_default_does_not(self):
        m = 504.3298
        entry = 'BEGIN IONS\nFEATURE_ID={}\nPEPMASS={:.4f}\nRTINSECONDS=100\nMSLEVEL=2\nCOLLISION_ENERGY=30\nION={}\n50 1\nEND IONS\n'
        with tempfile.TemporaryDirectory() as tmp:
            mgf = Path(tmp) / 'f.mgf'
            mgf.write_text(entry.format(1, m + H, '[M+H]1+') + entry.format(2, m + NA, '[M+H]+')
                           + entry.format(3, 300.0, '[M+K]+'))
            rows = {r['feature_id']: r for r in convert_mgf(mgf, Path(tmp) / 'out', 'eV')}
            self.assertEqual((rows['1']['adduct'], rows['1']['adduct_source']), ('[M+H]+', 'ion_identity'))
            self.assertEqual((rows['2']['adduct'], rows['2']['adduct_source']), ('[M+Na]+', 'ion_identity'))
            self.assertEqual((rows['3']['adduct'], rows['3']['adduct_source']), ('[M+K]+', 'mgf'))


@unittest.skipUnless(importlib.util.find_spec('ms_pred'), 'requires ms_pred')
class MsbuddyAdductTests(unittest.TestCase):
    def test_msbuddy_receives_the_spectrum_adduct(self):
        from ms_pred import common  # noqa: F401  (import before patching sys.modules, which drops new imports)
        from msms_structure_elucidation import spectrum, worker  # noqa: F401
        seen = []
        class Engine:
            def __init__(self, config): self.data = []
            def load_mgf(self, path): seen.append(Path(path).read_text())
            def annotate_formula(self): pass
        fake = types.SimpleNamespace(Msbuddy=Engine, MsbuddyConfig=lambda **kw: kw)
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / 'q.ms'
            spec.write_text('>parentmass 527.3156\n>ionization [M+Na]+\n>collision 30 eV\n100 1\n')
            with patch.dict(sys.modules, {'msbuddy': fake}):
                worker.formula_candidates(str(spec), 'eV')
        self.assertIn('ADDUCT=[M+Na]+', seen[0])


if __name__ == '__main__':
    unittest.main()
