"""Build the public, browser-only plasma unknown demo from a verified retrieval result.

Run with a Python that has ms-pred and RDKit available. The source spectrum is
the public file in coleygroup/ms-pred; collision energies were confirmed as NCE
by the spectrum provider for this demo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from msms_structure_elucidation.visualize import export_static  # noqa: E402


SOURCE_URL = ('https://github.com/coleygroup/ms-pred/blob/main/data/exp_specs/'
              'clinical/plasma_unknown_583.ms')
INSTALL_URL = 'https://github.com/coleygroup/msms-structure-elucidation.skills'
SOURCE_SHA256 = '54a2b505a2a9e3918d0dcf2a4ae25c638b396bdc29d9087db0f5b8955454c445'
INPUT_NCE = [10, 20, 30, 40, 50]
MODEL_EV = [11, 22, 32, 43, 54]


def source_hash(path: Path) -> str:
    """Ignore only CRLF versus LF, which differs between Git checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def validate(result: dict, spectrum: Path) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True, help='retrieval.json from the confirmed-NCE run')
    parser.add_argument('--spectrum', type=Path, required=True, help='public plasma_unknown_583.ms')
    parser.add_argument('--output', type=Path, default=ROOT / 'demo/msms-structure-elucidation/index.html')
    args = parser.parse_args()
    result = json.loads(args.result.read_text())
    validate(result, args.spectrum)
    output = export_static([args.result], args.output, top_k=20, demo_reviews=True,
                           title='Plasma unknown 583 | MS/MS structure elucidation demo',
                           demo_source_url=SOURCE_URL, demo_install_url=INSTALL_URL)
    page = output.read_text()
    for private_path in ('/home/', '/mnt/', '/tmp/', 'C:\\Users\\'):
        if private_path in page:
            raise ValueError(f'Private machine path leaked into public demo: {private_path}')
    manifest = {
        'source_spectrum': SOURCE_URL,
        'source_sha256_lf': SOURCE_SHA256,
        'user_confirmed_collision_unit': 'NCE',
        'input_energies_nce': INPUT_NCE,
        'model_energies_ev': MODEL_EV,
        'formula_source': result.get('formula_source'),
        'formula_hypotheses': [row['formula'] for row in result.get('formula_hypotheses', [])],
        'candidate_source': 'public ICEBERG 2.1 PubChem atlas',
        'scored_structures': result.get('qc', {}).get('scored_structures'),
        'ranked_candidates': len(result['candidates']),
        'demo_reviews': 'browser localStorage only',
        'html_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    output.with_name('manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
