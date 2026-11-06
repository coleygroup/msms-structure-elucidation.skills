"""Prepared entries and cache-only scoring share the native model's molecule identity."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation import preparation
from msms_structure_elucidation.mcp.features import Controller
from msms_structure_elucidation.mcp.jobs import read_json,write_json


@unittest.skipUnless(importlib.util.find_spec('rdkit') and importlib.util.find_spec('ms_pred'),
                     'requires the scientific RDKit/ms-pred environment')
class PreparedIdentityTests(unittest.TestCase):
    def test_aromatic_normalization_matches_native_entries_and_provenance(self):
        from ms_pred import common
        original='O=[N+]([O-])c1cc2ccc(Cl)c3c1N=c23'
        canonical=common.rm_stereo(original)
        self.assertNotEqual(original,canonical)
        self.assertEqual(common.inchikey_from_smiles(original),common.inchikey_from_smiles(canonical))
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);out=root/'out';out.mkdir()
            source=root/'native/src/ms_pred';source.mkdir(parents=True);(source/'code.py').write_text('pass\n')
            checkpoint=root/'weights.ckpt';checkpoint.write_bytes(b'fixture')
            spectrum=root/'spectrum.ms';spectrum.write_text('>parentmass 230\n>ionization [M+Na]+\n>collision 9 eV\n50 1\n')
            options=dict(model='glacier',checkpoint=str(checkpoint),ms_pred_dir=str(root/'native'),
                prediction_cache=str(root/'cache'),batch_size=128,num_cpu_workers=4,num_gpu_workers=1)
            def native_prepare(**kwargs):
                folder=root/'native-entries';folder.mkdir();path=folder/'prepared.json'
                write_json(path,{'entries':[[common.rm_stereo(s),'pred_candidate_'+str(i),e]
                    for i,s in enumerate(kwargs['smiles']) for e in kwargs['energies']],
                    'options':{'num_cpu_workers':kwargs['num_cpu_workers']}})
                return path
            with patch('msms_structure_elucidation.worker._check_glacier_features'), \
                    patch('msms_structure_elucidation.glacier.prepare',side_effect=native_prepare):
                preparation.record_request(out,spectrum,'C9H3ClN2O2','eV','Orbitrap',options,[original])
            request_path=next((out/'preparation').glob('*.json'));request=read_json(request_path)
            self.assertEqual(request['smiles'],[canonical])
            cropped=root/'group.json'
            Controller._prepared_group(None,[(canonical,request['native_prepared'])],'glacier',(9,),cropped)
            self.assertEqual(read_json(cropped)['entries'][0][0],canonical)
            manifest=out/'candidates.json';write_json(manifest,[{'smiles':original,'pubchem_cids':[7]}])
            saved=out/'prepared.json';write_json(saved,{'requests':[str(request_path)],'top_k':10,'shard_size':128,
                'no_report':True,'result':{'input':str(spectrum),'collision_unit':'eV','status':'prepared',
                'search_mode':'pubchem-formulas','instrument':'Orbitrap','model_settings':{'prediction_cache':str(root/'cache')},
                'formula_results':[{'formula':'C9H3ClN2O2','candidate_manifest':str(manifest)}]}})
            with patch('msms_structure_elucidation.cli._simulate_shards',return_value=([{'smiles':canonical}],1)), \
                    patch('msms_structure_elucidation.cli._finalize_result') as finalize, \
                    patch('msms_structure_elucidation.cli._save'):
                preparation.score(saved,'unused-cache-only-interpreter')
            self.assertEqual(finalize.call_args.args[2][0]['pubchem_cids'],[7])
