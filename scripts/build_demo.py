"""Build the public plasma demo from its checked-in analysis snapshot.

The Pages build needs only Python's standard library. Refreshing the snapshot
from retrieval.json requires an environment with ms-pred and RDKit.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from msms_structure_elucidation.visualize import _inline_page, export_static  # noqa: E402


SOURCE_URL = ('https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/'
              'clinical/plasma_unknown_583.ms')
INSTALL_URL = 'https://github.com/coleygroup/msms-structure-elucidation.skills'
SOURCE_SHA256 = '54a2b505a2a9e3918d0dcf2a4ae25c638b396bdc29d9087db0f5b8955454c445'
INPUT_NCE = [10, 20, 30, 40, 50]
MODEL_EV = [11, 22, 32, 43, 54]
SNAPSHOT = ROOT / 'demo/msms-structure-elucidation/source.json.gz'
DEFAULT_OUTPUT = ROOT / '_site/demo/msms-structure-elucidation/index.html'
TITLE = 'Plasma unknown 583 | MS/MS structure elucidation demo'


def source_hash(path: Path) -> str:
    """Ignore only CRLF versus LF, which differs between Git checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def validate_result(result: dict, spectrum: Path) -> None:
    if source_hash(spectrum) != SOURCE_SHA256:
        raise ValueError('Spectrum does not match the public plasma_unknown_583.ms source')
    if source_hash(Path(result['input'])) != SOURCE_SHA256:
        raise ValueError('Retrieval result was generated from a different spectrum')
    if result.get('status') != 'ranked' or result.get('collision_unit') != 'NCE':
        raise ValueError('Demo requires ranked results using provider-confirmed NCE')
    mapping = result.get('energy_mapping') or []
    if [int(row['input_value']) for row in mapping] != INPUT_NCE or \
       [int(row['model_energy_ev']) for row in mapping] != MODEL_EV:
        raise ValueError('Demo must cover all five confirmed NCE energies at integer eV')
    if not result.get('candidates'):
        raise ValueError('Demo has no ranked candidates')
    for candidate in result['candidates']:
        if not str(candidate.get('source', '')).startswith('public ICEBERG'):
            raise ValueError('Public demo must use public atlas candidates only')
        if len(candidate.get('energy_alignment', [])) != len(INPUT_NCE):
            raise ValueError('A candidate is missing a confirmed collision energy')
        if not candidate.get('predicted_fragment_ids'):
            raise ValueError('A candidate is missing fragment IDs for interactive inspection')


def validate_snapshot(data: dict) -> None:
    if data.get('demo_source_url') != SOURCE_URL or data.get('demo_install_url') != INSTALL_URL:
        raise ValueError('Demo source or install URL differs from the verified source')
    if data.get('source_sha256_lf') != SOURCE_SHA256 or data.get('demo_reviews') is not True:
        raise ValueError('Demo snapshot lacks verified source provenance or review controls')
    if len(data.get('index', [])) != 1 or len(data.get('results', [])) != 1:
        raise ValueError('Demo snapshot must contain exactly one result')
    result = data['results'][0]['result']
    structures = data['results'][0]['structures']
    mapping = result.get('energy_mapping') or []
    if result.get('collision_unit') != 'NCE' or \
       [int(row['input_value']) for row in mapping] != INPUT_NCE or \
       [int(row['model_energy_ev']) for row in mapping] != MODEL_EV:
        raise ValueError('Demo snapshot has incorrect collision-energy provenance')
    candidates = result.get('candidates') or []
    if not candidates or len(candidates) != len(structures):
        raise ValueError('Demo snapshot has missing candidates or structure drawings')
    for candidate, structure in zip(candidates, structures):
        if not str(candidate.get('source', '')).startswith('public ICEBERG'):
            raise ValueError('Demo snapshot contains a nonpublic candidate')
        if len(candidate.get('energy_alignment', [])) != len(INPUT_NCE):
            raise ValueError('Demo snapshot candidate lacks an energy')
        if not candidate.get('predicted_fragment_ids') or not structure.get('fragments'):
            raise ValueError('Demo snapshot lacks interactive fragment assignments')
    blob = json.dumps(data, separators=(',', ':'), allow_nan=False)
    for private_path in ('/home/', '/mnt/', '/tmp/', 'C:\\Users\\'):
        if private_path in blob:
            raise ValueError(f'Private machine path leaked into public demo: {private_path}')


def snapshot_from_result(result_path: Path, result: dict) -> dict:
    with tempfile.TemporaryDirectory() as temp_dir:
        page = export_static([result_path], Path(temp_dir) / 'index.html', top_k=20,
                             demo_reviews=True, title=TITLE,
                             demo_source_url=SOURCE_URL, demo_install_url=INSTALL_URL)
        match = re.search(r'<script>window\.MSMS_STATIC=(.*?);</script>', page.read_text(), re.S)
        if match is None:
            raise ValueError('Viewer export did not contain its static data')
        data = json.loads(match.group(1))
    data['source_sha256_lf'] = SOURCE_SHA256
    data['provenance'] = {
        'formula_source': result.get('formula_source'),
        'formula_hypotheses': [row['formula'] for row in result.get('formula_hypotheses', [])],
        'candidate_source': 'public ICEBERG 2.1 PubChem atlas',
        'scored_structures': result.get('qc', {}).get('scored_structures'),
    }
    validate_snapshot(data)
    return data


def write_snapshot(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    path.write_bytes(gzip.compress(blob, mtime=0))


def read_snapshot(path: Path) -> dict:
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        data = json.load(stream)
    validate_snapshot(data)
    return data


def build_site(data: dict, output: Path) -> None:
    validate_snapshot(data)
    page = _inline_page(data, title=TITLE)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding='utf-8')
    result = data['results'][0]['result']
    provenance = data.get('provenance', {})
    manifest = {
        'source_spectrum': SOURCE_URL,
        'source_sha256_lf': SOURCE_SHA256,
        'user_confirmed_collision_unit': 'NCE',
        'input_energies_nce': INPUT_NCE,
        'model_energies_ev': MODEL_EV,
        'formula_source': provenance.get('formula_source'),
        'formula_hypotheses': provenance.get('formula_hypotheses'),
        'candidate_source': provenance.get('candidate_source'),
        'scored_structures': provenance.get('scored_structures'),
        'ranked_candidates': len(result['candidates']),
        'demo_reviews': 'browser localStorage only',
        'html_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    output.with_name('manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--snapshot', type=Path, help='checked-in public data for a fast site build')
    source.add_argument('--result', type=Path, help='refresh the snapshot from retrieval.json')
    parser.add_argument('--spectrum', type=Path, help='source .ms file; required with --result')
    parser.add_argument('--snapshot-output', type=Path, default=SNAPSHOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.result:
        if args.spectrum is None:
            parser.error('--spectrum is required with --result')
        result = json.loads(args.result.read_text())
        validate_result(result, args.spectrum)
        data = snapshot_from_result(args.result, result)
        write_snapshot(data, args.snapshot_output)
        print(f'Wrote public analysis snapshot {args.snapshot_output}')
    else:
        data = read_snapshot(args.snapshot)
    build_site(data, args.output)


if __name__ == '__main__':
    main()
