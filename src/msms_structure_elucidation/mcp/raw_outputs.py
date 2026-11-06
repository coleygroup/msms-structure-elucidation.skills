"""Serialize native HDF5 predictions once, then publish validated raw cache records."""
from __future__ import annotations

import argparse
import json
import inspect
import hashlib
import time
from collections import defaultdict
from pathlib import Path

from msms_structure_elucidation import prediction_cache as cache


def missing_groups(root: Path, identity: dict, smiles: list[str], energies: list[int],
                   timeout: float = 30) -> dict[tuple, list[str]]:
    """Plan only absent structure/energy pairs without importing a scientific runtime."""
    database = root / cache.digest(identity) / 'spectra.sqlite3'
    requested = [str(int(value)) for value in energies]
    found = set()
    if database.is_file():
        with cache.read_cache(database, timeout=timeout) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spectra'").fetchone():
                for start in range(0, len(smiles), 128):
                    values = smiles[start:start+128]
                    query = 'SELECT smiles,energy FROM spectra WHERE smiles IN (' + ','.join('?' for _ in values) + ')'
                    found.update(db.execute(query, values).fetchall())
    groups = defaultdict(list)
    for smi in smiles:
        groups[tuple(int(e) for e in requested if (smi,e) not in found)].append(smi)
    return dict(groups)


def publish(folder: Path, root: Path, identity: dict, smiles: list[str], energies: list[int]) -> dict:
    """Use the existing validator and atomic cache writer under the dispatcher's held candidate lease."""
    path = folder / 'raw-predictions.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    by_smiles = {row['smiles']:row for row in records}
    if len(by_smiles) != len(records) or set(by_smiles) != set(smiles):
        raise RuntimeError('Collected raw prediction identities differ from the GPU request')
    _, receipt = cache.predictions(root, identity, smiles, energies,
        lambda molecules, needed: [by_smiles[smi] for smi in molecules], lease_held=True)
    return receipt


def publish_native(params: dict, config: dict) -> dict:
    """Decode closed native HDF5 files and publish the raw cache on a CPU worker."""
    from ms_pred import common
    from msms_structure_elucidation.worker import _serial_spec, _serial_fragment_ids
    from msms_structure_elucidation.config import asset
    started = time.monotonic()
    native = asset(config['models']['simulator']['ms_pred_src'], config)
    if cache.source_fingerprint(Path(native) / 'src/ms_pred') != params['native_source']:
        raise RuntimeError('CPU output decoder differs from the confirmed native implementation')
    if params.get('batch_plan'):
        from msms_structure_elucidation.mcp.batches import read_plan
        expected = {smi:{str(int(e)) for e in values}
                    for smi,values in read_plan(params['batch_plan'])['expected_energies'].items()}
    else:
        expected = {smi:{str(int(e)) for e in params['energies']} for smi in params['smiles']}
    folder = Path(params['folder'])
    records = {}
    reader_options = ({'h5_persistent':True}
                      if 'h5_persistent' in inspect.signature(common.PredSpecDB).parameters else {})
    db = common.PredSpecDB(folder / 'preds.hdf5', **reader_options)
    try:
        for item in db.get_all_specs():
            pred = item[-1]
            smi = pred.root_canonical_smiles
            record = {'smiles':smi, 'predicted_spectra':_serial_spec(pred),
                      'predicted_fragment_ids':_serial_fragment_ids(pred)}
            if smi in records or smi not in expected or set(record['predicted_spectra'])!=expected[smi]:
                raise RuntimeError('Native output has duplicate structures or incorrect energy coverage')
            # Validate the entire native bundle before publishing any subset.
            for energy,peaks in record['predicted_spectra'].items():
                cache._validate({'smiles':smi,'energy':energy,'peaks':peaks,
                                 'fragment_ids':record['predicted_fragment_ids'].get(energy)},smi,energy)
            records[smi] = record
    finally:
        db.close()
    if set(records)!=set(expected):
        raise RuntimeError('Native output does not account for all requested structures')
    count = sum(map(len,expected.values()))
    if params.get('expected_spectra',count)!=count:
        raise RuntimeError('Native output spectrum count differs from the CPU batch plan')
    subsets = defaultdict(list)
    for smi,energies in expected.items():
        subsets[tuple(sorted(map(int,energies)))].append(smi)
    receipt = {'namespace':cache.digest(params['raw_cache_identity']),'identity':params['raw_cache_identity'],
               'predicted_spectra':0,'reused_spectra':0,'expected_spectra':count}
    for energies,smiles in subsets.items():
        _,part = cache.predictions(Path(params['raw_cache_root']),params['raw_cache_identity'],smiles,list(energies),
            lambda molecules,needed:[records[smi] for smi in molecules],lease_held=True)
        for key in ('predicted_spectra','reused_spectra'):
            receipt[key] += part[key]
    native_files = {}
    for path in sorted(folder.glob('*.hdf5')):
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(4*1024*1024),b''):
                hasher.update(block)
        native_files[path.name] = {'size':path.stat().st_size,'sha256':hasher.hexdigest()}
    return {**receipt, 'input_format':'native-hdf5', 'structures':len(expected),'native_files':native_files,
            'elapsed_seconds':time.monotonic()-started}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder')
    parser.add_argument('--request')
    parser.add_argument('--publish-job', help='CPU publication job directory with request.json and config.json')
    args = parser.parse_args()
    if args.publish_job:
        if args.folder or args.request:
            parser.error('--publish-job cannot be combined with --folder/--request')
        from msms_structure_elucidation.mcp.jobs import read_json
        folder = Path(args.publish_job)
        receipt = publish_native(read_json(folder/'request.json')['params'],read_json(folder/'config.json'))
        print('RESULT_JSON='+json.dumps(receipt))
        return 0
    if not args.folder or not args.request:
        parser.error('Pass --folder and --request, or --publish-job')
    from ms_pred import common
    from msms_structure_elucidation.worker import _serial_spec, _serial_fragment_ids
    request = json.loads(Path(args.request).read_text())['arguments']
    folder = Path(args.folder)
    db = common.PredSpecDB(folder / 'preds.hdf5')
    target = folder / 'raw-predictions.jsonl'
    temporary = target.with_suffix('.tmp')
    seen = set()
    expected = {str(int(e)) for e in request['energies']}
    try:
        with temporary.open('w') as stream:
            for item in db.get_all_specs():
                pred = item[-1]
                record = {'smiles':pred.root_canonical_smiles, 'predicted_spectra':_serial_spec(pred),
                          'predicted_fragment_ids':_serial_fragment_ids(pred)}
                if record['smiles'] in seen or set(record['predicted_spectra']) != expected:
                    raise RuntimeError('Native output has duplicate structures or incomplete energy coverage')
                seen.add(record['smiles'])
                stream.write(json.dumps(record, allow_nan=False) + '\n')
        if seen != set(request['smiles']):
            raise RuntimeError('Native output does not account for all requested structures')
        temporary.replace(target)
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
