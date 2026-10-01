"""Build the clinical unknowns demo from its checked-in public analysis snapshot.

The Pages build uses only Python's standard library. Refreshing the snapshot
from retrieval results requires an environment with ms-pred and RDKit.
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


CLINICAL_URL = 'https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/clinical/'
INSTALL_URL = 'https://github.com/coleygroup/msms-structure-elucidation.skills'
INPUT_NCE = [10, 20, 30, 40, 50]
# Keep plasma 583 first so reviews saved in the original one-unknown demo retain their index.
SOURCES = {
    'plasma_unknown_583': {
        'sha256': '54a2b505a2a9e3918d0dcf2a4ae25c638b396bdc29d9087db0f5b8955454c445',
        'model_ev': [11, 22, 32, 43, 54],
    },
    'csf_unknown': {
        'sha256': '44425a145f4e8feca2050c4437f5e9af1cbacbb5d6da3cec4fb72471f29d2692',
        'model_ev': [5, 10, 16, 21, 26],
    },
    'plasma_unknown_198': {
        'sha256': '9025c8c6874ebb76d0a54608563c2920421ed3f9cf9c7163737628bd018b462b',
        'model_ev': [4, 8, 12, 16, 20],
    },
}
SNAPSHOT = ROOT / 'demo/msms-structure-elucidation/source.json.gz'
DEFAULT_OUTPUT = ROOT / '_site/demo/msms-structure-elucidation/index.html'
TITLE = 'Clinical unknowns | MS/MS structure elucidation demo'


def source_url(name: str) -> str:
    return CLINICAL_URL + name + '.ms'


def source_hash(path: Path) -> str:
    """Ignore only CRLF versus LF, which differs between Git checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def _validate_mapping(result: dict, name: str) -> None:
    mapping = result.get('energy_mapping') or []
    if result.get('collision_unit') != 'NCE' or \
       [int(row['input_value']) for row in mapping] != INPUT_NCE or \
       [int(row['model_energy_ev']) for row in mapping] != SOURCES[name]['model_ev']:
        raise ValueError(f'{name} must cover all five provider-confirmed NCE energies at integer eV')


def _validate_candidates(result: dict, name: str) -> None:
    candidates = result.get('candidates') or []
    if not candidates:
        raise ValueError(f'{name} has no ranked candidates')
    for candidate in candidates:
        if not str(candidate.get('source', '')).startswith('public ICEBERG'):
            raise ValueError(f'{name} contains a nonpublic candidate')
        if len(candidate.get('energy_alignment', [])) != len(INPUT_NCE):
            raise ValueError(f'{name} has a candidate missing a confirmed collision energy')
        for alignment in candidate['energy_alignment']:
            key = alignment.get('prediction_key') or alignment.get('atlas_key')
            if not candidate.get('predicted_fragment_ids', {}).get(key):
                raise ValueError(f'{name} has a candidate missing fragment IDs at {key} eV')


def validate_result(result: dict, spectrum: Path, name: str) -> None:
    expected = SOURCES[name]['sha256']
    if spectrum.name != name + '.ms' or source_hash(spectrum) != expected:
        raise ValueError(f'{name} spectrum does not match the public ms-pred source')
    if source_hash(Path(result['input'])) != expected:
        raise ValueError(f'{name} retrieval result was generated from a different spectrum')
    if result.get('status') != 'ranked':
        raise ValueError(f'{name} requires ranked retrieval results')
    _validate_mapping(result, name)
    _validate_candidates(result, name)


def validate_snapshot(data: dict) -> None:
    if data.get('demo_install_url') != INSTALL_URL or data.get('demo_reviews') is not True:
        raise ValueError('Demo snapshot lacks install provenance or review controls')
    if data.get('source_sha256_lf') != {name: s['sha256'] for name, s in SOURCES.items()}:
        raise ValueError('Demo snapshot has incorrect source hashes')
    if data.get('demo_source_urls') != [source_url(name) for name in SOURCES]:
        raise ValueError('Demo snapshot has incorrect source URLs')
    index, results = data.get('index', []), data.get('results', [])
    if len(index) != len(SOURCES) or len(results) != len(SOURCES):
        raise ValueError('Demo snapshot must contain every public clinical unknown')
    for i, name in enumerate(SOURCES):
        if index[i].get('label') != name or results[i].get('label') != name:
            raise ValueError(f'Demo result {i} must be {name}')
        if index[i].get('path') != name or results[i]['result'].get('input') != name + '.ms':
            raise ValueError(f'{name} leaks a local input path')
        result, structures = results[i]['result'], results[i]['structures']
        if result.get('status') != 'ranked':
            raise ValueError(f'{name} has no ranked result')
        _validate_mapping(result, name)
        _validate_candidates(result, name)
        if len(result['candidates']) != len(structures):
            raise ValueError(f'{name} has missing structure drawings')
        if any(not structure.get('fragments') for structure in structures):
            raise ValueError(f'{name} lacks interactive fragment assignments')
    blob = json.dumps(data, separators=(',', ':'), allow_nan=False)
    for private_path in ('/home/', '/mnt/', '/tmp/', 'C:\\Users\\'):
        if private_path in blob:
            raise ValueError(f'Private machine path leaked into public demo: {private_path}')


def snapshot_from_results(result_paths: list[Path], spectra: list[Path]) -> dict:
    if len(result_paths) != len(SOURCES) or len(spectra) != len(SOURCES):
        raise ValueError('Supply one result and one spectrum for each clinical unknown')
    for name, result_path, spectrum in zip(SOURCES, result_paths, spectra):
        validate_result(json.loads(result_path.read_text()), spectrum, name)
    with tempfile.TemporaryDirectory() as temp_dir:
        page = export_static(result_paths, Path(temp_dir) / 'index.html', top_k=20,
                             demo_reviews=True, title=TITLE,
                             demo_source_url=source_url(next(iter(SOURCES))),
                             demo_install_url=INSTALL_URL)
        match = re.search(r'<script>window\.MSMS_STATIC=(.*?);</script>', page.read_text(), re.S)
        if match is None:
            raise ValueError('Viewer export did not contain its static data')
        data = json.loads(match.group(1))
    data['source_sha256_lf'] = {name: s['sha256'] for name, s in SOURCES.items()}
    data['demo_source_urls'] = [source_url(name) for name in SOURCES]
    # The former demo stored browser reviews under its export timestamp. Preserve those notes.
    if SNAPSHOT.exists():
        with gzip.open(SNAPSHOT, 'rt', encoding='utf-8') as stream:
            previous = json.load(stream)
        data['demo_review_storage_key'] = previous.get(
            'demo_review_storage_key', 'msms-demo-reviews:' + previous['exported_at'])
    data['provenance'] = {}
    for name, result_path in zip(SOURCES, result_paths):
        result = json.loads(result_path.read_text())
        data['provenance'][name] = {
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
    unknowns = []
    for i, name in enumerate(SOURCES):
        result = data['results'][i]['result']
        unknowns.append({
            'name': name,
            'source_spectrum': source_url(name),
            'source_sha256_lf': SOURCES[name]['sha256'],
            'user_confirmed_collision_unit': 'NCE',
            'input_energies_nce': INPUT_NCE,
            'model_energies_ev': SOURCES[name]['model_ev'],
            **data['provenance'][name],
            'ranked_candidates': len(result['candidates']),
        })
    manifest = {
        'unknowns': unknowns,
        'demo_reviews': 'browser localStorage only',
        'html_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    output.with_name('manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--snapshot', type=Path, help='checked-in public data for a fast site build')
    source.add_argument('--result', type=Path, action='append', help='retrieval.json in SOURCES order; repeat for each unknown')
    parser.add_argument('--spectrum', type=Path, action='append', help='source .ms file in the same order; required with --result')
    parser.add_argument('--snapshot-output', type=Path, default=SNAPSHOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.result:
        if not args.spectrum or len(args.spectrum) != len(args.result):
            parser.error('supply one --spectrum for every --result')
        data = snapshot_from_results(args.result, args.spectrum)
        write_snapshot(data, args.snapshot_output)
        print(f'Wrote public analysis snapshot {args.snapshot_output}')
    else:
        data = read_snapshot(args.snapshot)
    build_site(data, args.output)


if __name__ == '__main__':
    main()
