"""Worker renderings remain usable on a client without a scientific renderer."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation import visualize


class ViewerCacheTests(unittest.TestCase):
    def test_export_reuses_worker_fragments_without_local_scientific_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'retrieval.json';spectrum=root/'spectrum.ms'
            spectrum.write_text('>compound example\n>parentmass 100\n')
            candidate={'smiles':'CCO','source':'GLACIER','formula':'C2H6O','entropy_similarity':.8,'explained_intensity':.9,
                'energy_alignment':[{'experimental_key':'20','prediction_key':'20','atlas_key':'20'}],
                'predicted_spectra':{'20':[[31.,1.]],'40':[[40.,.5]]},'predicted_fragment_ids':{'20':['3'],'40':['7']},
                'matched_peaks':[]}
            result={'input':str(spectrum),'adduct':'[M+H]+','spectra':{'20':[[31.,1.]]},'candidates':[candidate]}
            source.write_text(json.dumps(result))
            payload={'svg':'<svg>worker structure</svg>','fragments':{'3':{'a':[0,1],'b':[0]}},'peaks':{'20':['CH3O']}}
            with patch.object(visualize,'candidate_payload',return_value=payload):
                visualize.write_structure_cache(result,root/'viewer_structures.json')
            result['viewer_structures_path']='/unavailable/original-host/viewer_structures.json'
            source.write_text(json.dumps(result))
            with patch.object(visualize,'candidate_payload',side_effect=AssertionError('Local science is forbidden')):
                page=visualize.export_static([source],root/'report.html')
            self.assertIn('worker structure',page.read_text())

    def test_modified_predictions_do_not_reuse_a_stale_fragment_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);candidate={'smiles':'CCO','energy_alignment':[{'prediction_key':'20'}],
                'predicted_spectra':{'20':[[31.,1.]]},'predicted_fragment_ids':{'20':['3']}}
            result={'adduct':'[M+H]+','candidates':[candidate]}
            with patch.object(visualize,'candidate_payload',return_value={'svg':'old'}):
                visualize.write_structure_cache(result,root/'viewer_structures.json')
            candidate['predicted_fragment_ids']['20']=['7']
            with patch.object(visualize,'candidate_payload',return_value={'svg':'new'}) as render:
                actual=visualize.saved_candidate_payload(root/'retrieval.json',result,candidate)
            self.assertEqual(actual['svg'],'new')
            render.assert_called_once()


if __name__=='__main__':unittest.main()
