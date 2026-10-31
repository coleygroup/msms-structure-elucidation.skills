"""Feature-table ID and energy handling for GNPS2 Everything Bagel-compatible MGF exports."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from msms_structure_elucidation import cli
from msms_structure_elucidation.mgf import convert_mgf


class Gnps2FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mgf = self.root / 'aligned_features_filled.mgf'
        self.output = self.root / 'converted'

    def entry(self, feature: str, mass: float, energy: float | None = None) -> str:
        """Build an export with deliberately different table IDs and display titles."""
        collision = f'COLLISION_ENERGY={energy}\n' if energy is not None else ''
        return (f'BEGIN IONS\nSCANS={feature}\nTITLE=Feature {feature}\n'
                f'FEATURE_ID=legacy-{feature}\nPEPMASS={mass}\n'
                f'RTINSECONDS=100\nCHARGE=1+\nMSLEVEL=2\n{collision}50 10\nEND IONS\n')

    def test_cli_uses_row_ids_for_selection_and_all_features_for_ion_identity(self):
        self.mgf.write_text(self.entry('42', 101.007276) + self.entry('43', 122.989218))
        selected = self.root / 'selected.csv'
        selected.write_text('row ID\n42\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(['convert-mgf', '--input', str(self.mgf),
                               '--output-dir', str(self.output), '--collision-unit', 'eV',
                               '--energy', '30', '--feature-id-field', 'scans',
                               '--feature-ids', str(selected)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['ready'], 1)
        rows = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['feature_id'], '42')
        self.assertEqual(rows[0]['feature_id_field'], 'SCANS')
        self.assertEqual(rows[0]['adduct_partners'], '43:[M+Na]+')
        self.assertIn('>compound 42\n', Path(rows[0]['ms_path']).read_text())

    def test_default_identifier_precedence_is_preserved(self):
        self.mgf.write_text(self.entry('42', 101.007276, 30))
        rows = convert_mgf(self.mgf, self.output, 'eV', feature_ids={'legacy-42'})
        self.assertEqual(rows[0]['feature_id'], 'legacy-42')
        self.assertEqual(rows[0]['status'], 'ready')

    def test_selected_field_is_required_instead_of_falling_back_to_a_title(self):
        self.mgf.write_text(self.entry('42', 101.007276, 30).replace('SCANS=42\n', ''))
        for ion_identity in (True, False):
            with self.subTest(ion_identity=ion_identity):
                with self.assertRaisesRegex(ValueError, 'entry 1.*SCANS'):
                    convert_mgf(self.mgf, self.output, 'eV', feature_id_field='SCANS',
                                ion_identity=ion_identity)
        self.assertFalse(list(self.output.glob('*.ms')))

    def test_aligned_row_id_never_supplies_raw_ms2_energy(self):
        self.mgf.write_text(self.entry('42', 101.007276))
        raw = self.root / 'raw.mzXML'
        raw.write_text('<mzXML><msRun><scan num="42" msLevel="2" '
                       'retentionTime="PT100S" collisionEnergy="35">'
                       '<precursorMz>901.0</precursorMz></scan></msRun></mzXML>')
        rows = convert_mgf(self.mgf, self.output, 'eV', raw_mzxml=raw,
                           feature_id_field='SCANS')
        self.assertEqual(rows[0]['feature_id'], '42')
        self.assertEqual(rows[0]['status'], 'needs_energy')
        self.assertIsNone(rows[0]['collision_energy'])
        self.assertEqual(rows[0]['ms_path'], '')

    def test_energy_separated_entries_share_the_aligned_feature_id(self):
        self.mgf.write_text(self.entry('42', 101.007276, 20)
                            + self.entry('42', 101.007276, 40))
        rows = convert_mgf(self.mgf, self.output, 'eV', feature_id_field='SCANS')
        self.assertEqual({row['feature_id'] for row in rows}, {'42'})
        self.assertEqual({row['collision_energy'] for row in rows}, {20, 40})
        self.assertEqual(len({row['ms_path'] for row in rows}), 1)
        spectrum = Path(rows[0]['ms_path']).read_text()
        self.assertIn('>collision 20.0 eV', spectrum)
        self.assertIn('>collision 40.0 eV', spectrum)


if __name__ == '__main__':
    unittest.main()
