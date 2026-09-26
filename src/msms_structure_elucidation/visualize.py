"""Local, result-driven fragment viewer and review-note server.

Copyright (c) 2026 Coley Group.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


ASSETS = Path(__file__).with_name('visualize_assets')
DECISIONS = {'unreviewed', 'keep', 'uncertain', 'reject'}


def candidate_key(candidate: dict) -> str:
    inchikey = candidate.get('inchikey')
    return 'inchikey:' + inchikey if inchikey else 'smiles:' + candidate.get('canonical_smiles', candidate['smiles'])


def _atlas_path(result_path: Path, stored: str | None, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Atlas MGF not found: {path}')
        return path
    if not stored:
        return None
    path = Path(stored)
    if path.is_absolute():
        return path if path.is_file() else None
    for base in (Path.cwd(), *result_path.parents):
        candidate = base / path
        if candidate.is_file():
            return candidate.resolve()
    return None


def _recover_atlas_fragments(result: dict, mgf: Path | None) -> None:
    """Backfill annotations for older retrieval JSONs without changing that file."""
    if mgf is None:
        return
    missing = [(candidate, ce, peaks)
        for candidate in result.get('candidates', [])
        for ce, peaks in candidate.get('predicted_spectra', {}).items()
        if ce not in candidate.get('predicted_fragment_ids', {})]
    if not missing:
        return
    from ms_pred import common
    by_key = {}
    for meta, peaks in common.parse_spectra_mgf(str(mgf)):
        tokens = str(meta.get('FRAGS', '')).replace(',', ' ').split()
        if not tokens or len(tokens) != len(peaks):
            continue
        try:
            ids = [str(int(token)) for token in tokens]
            ce = f"{float(str(meta['COLLISION_ENERGY']).split()[0]):.0f}"
        except (KeyError, ValueError):
            continue
        for identity in (meta.get('INCHIKEY'), meta.get('SMILES')):
            if identity:
                by_key[(identity, ce)] = (peaks, ids)
    for candidate, ce, stored_peaks in missing:
        item = (by_key.get((candidate.get('inchikey'), ce)) or
                by_key.get((candidate.get('smiles'), ce)))
        if item is None:
            continue
        peaks, ids = item
        if len(stored_peaks) != len(peaks):
            continue
        if any(abs(float(pair[0])-float(row[0])) > 1e-5 or
               abs(float(pair[1])-float(row[1])) > 1e-6
               for pair, row in zip(stored_peaks, peaks)):
            continue
        candidate.setdefault('predicted_fragment_ids', {})[ce] = ids


def load_result(result_path: Path, atlas_mgf: Path | None = None) -> dict:
    result_path = result_path.expanduser().resolve()
    result = json.loads(result_path.read_text())
    if not isinstance(result, dict) or not isinstance(result.get('candidates'), list) or not isinstance(result.get('spectra'), dict):
        raise ValueError('Expected a retrieval.json result with candidates and experimental spectra')
    _recover_atlas_fragments(result, _atlas_path(result_path, result.get('atlas_mgf'), atlas_mgf))
    for candidate in result['candidates']:
        candidate['review_key'] = candidate_key(candidate)
        for ce, ids in list(candidate.get('predicted_fragment_ids', {}).items()):
            if ce not in candidate.get('predicted_spectra', {}) or len(ids) != len(candidate['predicted_spectra'][ce]):
                del candidate['predicted_fragment_ids'][ce]
    return result


def _read_notes(path: Path) -> dict:
    if not path.exists():
        return {'version': 1, 'candidates': {}}
    notes = json.loads(path.read_text())
    if notes.get('version') != 1 or not isinstance(notes.get('candidates'), dict):
        raise ValueError(f'Unsupported review notes format: {path}')
    return notes


def _write_notes(path: Path, notes: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.review_notes_', suffix='.tmp',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(notes, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@lru_cache(maxsize=16)
def _fragment_engine(smiles: str):
    from ms_pred.magma.fragmentation import FragmentEngine
    return FragmentEngine(smiles, mol_str_type='smiles', mol_str_canonicalized=True)


@lru_cache(maxsize=256)
def _fragment_drawing(smiles: str, fragment_id: int, mz: float, adduct: str) -> dict:
    from rdkit.Chem.Draw import rdMolDraw2D
    engine = _fragment_engine(smiles)
    drawing = engine.get_draw_dict(fragment_id)
    drawer = rdMolDraw2D.MolDraw2DSVG(300, 240)
    drawer.DrawMolecule(drawing['mol'], highlightAtoms=list(drawing.get('hatoms') or []),
                        highlightBonds=list(drawing.get('hbonds') or []))
    drawer.FinishDrawing()
    h_shift = 0
    try:
        h_shift = int(round((mz - float(engine.single_mass(fragment_id))) / 1.00784))
        formula = engine.formula_from_frag(fragment_id, h_shift=h_shift)
    except (ValueError, TypeError, KeyError):
        formula = ''
    sign = '+' if adduct.endswith('+') else '-' if adduct.endswith('-') else ''
    return {'svg': drawer.GetDrawingText(), 'formula': formula + sign if formula else '',
            'h_shift': h_shift}


def create_app(result_path: Path, atlas_mgf: Path | None = None):
    try:
        from flask import Flask, jsonify, request, send_from_directory
    except ImportError as exc:
        raise RuntimeError('Flask is required for visualization; install this package with the [visualize] extra in your ms-pred Python') from exc
    result_path = Path(result_path).expanduser().resolve()
    result = load_result(result_path, atlas_mgf)
    notes_path = result_path.with_name('review_notes.json')
    token = secrets.token_urlsafe(24)
    allowed = {candidate['review_key']: candidate for candidate in result['candidates']}
    app = Flask(__name__, static_folder=str(ASSETS), static_url_path='/assets')

    @app.get('/')
    def index():
        return send_from_directory(ASSETS, 'index.html')

    @app.get('/api/state')
    def state():
        return jsonify({'result': result, 'notes': _read_notes(notes_path), 'review_token': token})

    @app.get('/api/fragment/<int:candidate_index>/<ce>/<int:peak_index>')
    def fragment(candidate_index: int, ce: str, peak_index: int):
        if candidate_index < 0 or candidate_index >= len(result['candidates']):
            return jsonify({'error': 'Unknown candidate'}), 404
        candidate = result['candidates'][candidate_index]
        peaks = candidate.get('predicted_spectra', {}).get(ce, [])
        ids = candidate.get('predicted_fragment_ids', {}).get(ce, [])
        if peak_index < 0 or peak_index >= len(peaks) or peak_index >= len(ids):
            return jsonify({'error': 'No fragment annotation for this peak'}), 404
        try:
            fragment_id = int(ids[peak_index])
            mz = float(peaks[peak_index][0])
            drawing = _fragment_drawing(candidate['smiles'], fragment_id, mz, result.get('adduct', ''))
        except Exception as exc:
            return jsonify({'error': f'Cannot draw fragment: {exc}'}), 422
        return jsonify({**drawing, 'fragment_id': str(fragment_id), 'mz': mz,
                        'intensity': float(peaks[peak_index][1])})

    @app.post('/api/review')
    def save_review():
        if request.headers.get('X-Review-Token') != token:
            return jsonify({'error': 'Invalid review token'}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get('candidate_key') not in allowed:
            return jsonify({'error': 'Unknown candidate'}), 400
        decision = body.get('decision')
        comment = body.get('comment')
        if decision not in DECISIONS or not isinstance(comment, str) or len(comment) > 2000:
            return jsonify({'error': 'Invalid decision or candidate comment'}), 400
        candidate = allowed[body['candidate_key']]
        fragment_note = body.get('fragment')
        if fragment_note is not None:
            if not isinstance(fragment_note, dict):
                return jsonify({'error': 'Invalid fragment comment'}), 400
            ce = str(fragment_note.get('ce', ''))
            peak_index = fragment_note.get('peak_index')
            fragment_comment = fragment_note.get('comment')
            ids = candidate.get('predicted_fragment_ids', {}).get(ce, [])
            if (type(peak_index) is not int or peak_index < 0 or peak_index >= len(ids) or
                    not isinstance(fragment_comment, str) or len(fragment_comment) > 2000):
                return jsonify({'error': 'Unknown fragment or invalid comment'}), 400
        notes = _read_notes(notes_path)
        entry = notes['candidates'].setdefault(body['candidate_key'], {'fragments': {}})
        entry['decision'] = decision
        entry['comment'] = comment
        entry['updated_at'] = datetime.now(timezone.utc).isoformat()
        if fragment_note is not None:
            fragment_key = f'{ce}:{peak_index}'
            entry.setdefault('fragments', {})[fragment_key] = {
                'ce': ce, 'peak_index': peak_index, 'fragment_id': str(ids[peak_index]),
                'mz': float(candidate['predicted_spectra'][ce][peak_index][0]),
                'comment': fragment_comment}
        _write_notes(notes_path, notes)
        return jsonify({'saved': True, 'notes': notes, 'path': str(notes_path)})

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve a local interactive fragment viewer')
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--atlas-mgf', type=Path)
    parser.add_argument('--port', type=int, default=0, help='localhost port; 0 selects a free port')
    args = parser.parse_args(argv)
    from werkzeug.serving import make_server
    app = create_app(args.result, args.atlas_mgf)
    server = make_server('127.0.0.1', args.port, app)
    print(f'Fragment viewer: http://127.0.0.1:{server.server_port}/', flush=True)
    print('Press Ctrl+C to stop the viewer.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
