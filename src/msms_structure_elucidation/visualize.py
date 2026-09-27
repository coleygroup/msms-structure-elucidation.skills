"""Local, result-driven fragment viewer and review-note server.

Copyright (c) 2026 Coley Group.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import secrets
import tempfile
import threading
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




# Mid-tone heteroatom colors that read on both light and dark surfaces; carbon and bonds
# use currentColor so the page theme controls them.
ATOM_PALETTE = {7: (0.23, 0.49, 0.87), 8: (0.88, 0.33, 0.24), 16: (0.80, 0.62, 0.10),
                15: (0.85, 0.51, 0.17), 9: (0.18, 0.62, 0.27), 17: (0.18, 0.62, 0.27),
                35: (0.65, 0.35, 0.20), 53: (0.55, 0.30, 0.70)}
REVIEW_ORDER = ('keep', 'uncertain', 'reject')


@lru_cache(maxsize=128)
def _fragment_engine(smiles: str):
    from ms_pred.magma.fragmentation import FragmentEngine
    return FragmentEngine(smiles, mol_str_type='smiles', mol_str_canonicalized=True)


def _fragment_formula(engine, fragment_id: int, mz: float, adduct: str) -> tuple[str, int]:
    h_shift = 0
    try:
        h_shift = int(round((mz - float(engine.single_mass(fragment_id))) / 1.00784))
        formula = engine.formula_from_frag(fragment_id, h_shift=h_shift)
    except (ValueError, TypeError, KeyError):
        formula = ''
    sign = '+' if adduct.endswith('+') else '-' if adduct.endswith('-') else ''
    return (formula + sign if formula else ''), h_shift


@lru_cache(maxsize=256)
def _fragment_drawing(smiles: str, fragment_id: int, mz: float, adduct: str) -> dict:
    from rdkit.Chem.Draw import rdMolDraw2D
    engine = _fragment_engine(smiles)
    drawing = engine.get_draw_dict(fragment_id)
    drawer = rdMolDraw2D.MolDraw2DSVG(300, 240)
    drawer.DrawMolecule(drawing['mol'], highlightAtoms=list(drawing.get('hatoms') or []),
                        highlightBonds=list(drawing.get('hbonds') or []))
    drawer.FinishDrawing()
    formula, h_shift = _fragment_formula(engine, fragment_id, mz, adduct)
    return {'svg': drawer.GetDrawingText(), 'formula': formula, 'h_shift': h_shift}


def _clean_svg(svg: str) -> str:
    """Themeable, scalable inline SVG; RDKit's per-atom/bond classes are kept for highlighting."""
    svg = svg[svg.index('<svg'):]
    svg = re.sub(r"width='\d+px' height='\d+px' ", '', svg)
    svg = re.sub(r'<!--.*?-->', '', svg, flags=re.S)
    svg = re.sub(r"<rect style='opacity:1\.0;fill:#FFFFFF[^>]*>\s*</rect>", '', svg)
    svg = svg.replace('#000000', 'currentColor')
    svg = svg.replace(';stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1', '')
    # Bond width and fill come from the page stylesheet; keep only each half-bond's colour.
    svg = re.sub(r"style='fill:none;stroke:(#[0-9A-Fa-f]{6}|currentColor);stroke-width:2\.0px[^']*'", r"style='stroke:\1'", svg)
    return svg.replace('fill-rule:evenodd;', '')


@lru_cache(maxsize=512)
def _structure_svg(smiles: str) -> tuple[str, bool]:
    """One drawing per candidate. True when it is the fragment engine's molecule (indices valid)."""
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D
    try:
        mol, engine_mol = _fragment_engine(smiles).mol, True
    except Exception:
        mol, engine_mol = Chem.MolFromSmiles(smiles), False
    if mol is None:
        return '', False
    drawer = rdMolDraw2D.MolDraw2DSVG(320, 230)
    options = drawer.drawOptions()
    options.clearBackground = False
    options.padding = 0.06
    options.bondLineWidth = 2
    options.updateAtomPalette(ATOM_PALETTE)
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return _clean_svg(drawer.GetDrawingText()), engine_mol


def candidate_payload(candidate: dict, adduct: str) -> dict:
    """Structure drawing plus, per paired energy, each predicted peak's fragment formula (aligned with
    predicted_fragment_ids) and the atom/bond indices of every fragment, so the page can highlight
    fragments without more requests."""
    smiles = candidate['smiles']
    svg, engine_mol = _structure_svg(smiles)
    payload = {'svg': svg, 'fragments': {}, 'peaks': {}}
    if not engine_mol:
        return payload
    engine = _fragment_engine(smiles)
    for alignment in candidate.get('energy_alignment', []):
        ce = alignment.get('prediction_key') or alignment['atlas_key']
        spectrum = candidate.get('predicted_spectra', {}).get(ce) or []
        ids = candidate.get('predicted_fragment_ids', {}).get(ce) or []
        if len(ids) != len(spectrum):
            continue
        rows = []
        for (mz, _), fragment in zip(spectrum, ids):
            fragment = str(fragment)
            if fragment not in payload['fragments']:
                try:
                    drawing = engine.get_draw_dict(int(fragment))
                    payload['fragments'][fragment] = {'a': [int(i) for i in drawing['hatoms']],
                                                      'b': [int(i) for i in drawing['hbonds']]}
                except Exception:
                    payload['fragments'][fragment] = None
            formula, _ = _fragment_formula(engine, int(fragment), float(mz), adduct)
            rows.append(formula)
        payload['peaks'][ce] = rows
    payload['fragments'] = {k: v for k, v in payload['fragments'].items() if v}
    return payload


def client_result(result: dict, top_k: int | None = None, max_exp_peaks: int | None = None) -> dict:
    """What the page needs: paired-energy predictions only, optionally fewer candidates/peaks."""
    keep = ('smiles', 'canonical_smiles', 'inchikey', 'formula', 'source', 'entropy_similarity',
            'explained_intensity', 'energy_alignment', 'matched_peaks', 'review_key', 'rank', 'ambiguity',
            'model_collision_energies_ev', 'model_instrument', 'model_name', 'model_version',
            'ms_pred_version', 'model_checkpoint')
    candidates = []
    for candidate in result['candidates'][:top_k]:
        ces = [a.get('prediction_key') or a['atlas_key'] for a in candidate.get('energy_alignment', [])]
        slim = {k: candidate[k] for k in keep if k in candidate}
        slim['predicted_spectra'] = {ce: candidate['predicted_spectra'][ce]
                                     for ce in ces if ce in candidate.get('predicted_spectra', {})}
        slim['predicted_fragment_ids'] = {ce: candidate['predicted_fragment_ids'][ce]
                                          for ce in ces if ce in candidate.get('predicted_fragment_ids', {})}
        candidates.append(slim)
    spectra = result['spectra']
    if max_exp_peaks:
        matched = {(str(p['ce']), round(float(p['mz']), 5)) for c in candidates for p in c.get('matched_peaks', [])}
        spectra = {}
        for ce, peaks in result['spectra'].items():
            top = set(map(tuple, sorted(peaks, key=lambda p: -p[1])[:max_exp_peaks]))
            spectra[ce] = [p for p in peaks if tuple(p) in top or (ce, round(float(p[0]), 5)) in matched]
    fields = ('input', 'parentmass', 'adduct', 'peaks', 'formula', 'formula_source', 'formula_hypotheses',
              'collision_unit', 'energy_mapping', 'warnings', 'library_structures', 'review_evidence', 'review',
              'next_step', 'status', 'formula_results', 'instrument', 'mass_tolerance')
    return {**{k: result[k] for k in fields if k in result}, 'spectra': spectra, 'candidates': candidates}


def _result_label(result_path: Path) -> str:
    return result_path.parent.name if result_path.name == 'retrieval.json' else result_path.stem


def model_label(candidate: dict) -> str | None:
    """'ICEBERG 2.1' for model-predicted candidates; None for atlas entries."""
    source = str(candidate.get('source', ''))
    if not candidate or source.startswith('public'):
        return None
    name = candidate.get('model_name') or source
    version = candidate.get('model_version')
    return f'{name} {version}' if version else name


def _outcome(result: dict) -> str:
    """Viewer outcome from the retrieval status; results without a status use the older fields."""
    candidates = result.get('candidates') or []
    if candidates:
        return 'atlas' if str(candidates[0].get('source', '')).startswith('public') else 'model'
    step, status = result.get('next_step'), result.get('status')
    if status:
        return {'needs_formula': 'no-pubchem-formula' if step == 'review-frigid' else 'no-formula',
                'no_atlas_coverage': 'not-in-atlas', 'no_candidates': 'no-candidates',
                'blocked_model_assets': 'blocked-model-assets'}.get(status, 'atlas-failed')
    if not result.get('formula'):
        return 'no-pubchem-formula' if step == 'review-frigid' else 'no-formula'
    if step == 'retry-retrieval' or any(str(w).startswith('Atlas retrieval failed') for w in result.get('warnings', [])):
        return 'atlas-failed'
    if not result.get('library_structures'):
        return 'not-in-atlas'
    return 'no-energy-pair'


def _review_summary(notes: dict, keys: list[str]) -> dict:
    """Overall verdict for one unknown: a kept candidate wins, then uncertain, then reject."""
    entries = notes.get('candidates', {})
    touched = [k for k, n in entries.items()
               if n.get('decision', 'unreviewed') != 'unreviewed' or n.get('comment') or n.get('fragments')]
    verdict, rank = None, None
    for decision in REVIEW_ORDER:
        ranks = [keys.index(k) + 1 for k, n in entries.items() if n.get('decision') == decision and k in keys]
        if ranks:
            verdict, rank = decision, min(ranks)
            break
    return {'reviewed': len(touched), 'verdict': verdict, 'rank': rank}


def _ms_header(path: str | None) -> dict:
    header = {}
    if not path or not Path(path).is_file():
        return header
    with open(path) as handle:
        for line in handle:
            if line.startswith('>'):
                key, _, value = line[1:].strip().partition(' ')
                header[key.lower()] = value
            elif line.strip() and header.get('collision'):
                break
    return header


def _summarize(index: int, result_path: Path) -> tuple[dict, list[str]]:
    """Light per-result entry for the unknowns list, read once at startup."""
    result = json.loads(result_path.read_text())
    candidates = result.get('candidates') or []
    top = candidates[0] if candidates else {}
    keys = [candidate_key(c) for c in candidates]
    notes_path = result_path.with_name('review_notes.json')
    try:
        notes = _read_notes(notes_path)
    except (ValueError, json.JSONDecodeError):
        notes = {'candidates': {}}
    header = _ms_header(result.get('input'))
    rt = header.get('rt', '').rstrip('s')
    energies = [m.get('collision_energy_ev') for m in result.get('energy_mapping', [])]
    summary = {
        'index': index, 'label': _result_label(result_path), 'path': str(result_path),
        'parentmass': result.get('parentmass'), 'formula': result.get('formula'),
        'formula_source': result.get('formula_source'),
        'hypotheses': [h.get('formula') for h in result.get('formula_hypotheses', [])],
        'rt_min': round(float(rt) / 60, 2) if rt.replace('.', '', 1).isdigit() else None,
        'energies_ev': energies, 'outcome': _outcome(result), 'candidates': len(candidates),
        'top_similarity': top.get('entropy_similarity'), 'top_explained': top.get('explained_intensity'),
        'top_source': top.get('source'), 'top_smiles': top.get('smiles'), 'top_model': model_label(top),
        'review': _review_summary(notes, keys)}
    summary['reviewed'] = summary['review']['reviewed']
    return summary, keys


def _inline_page(data: dict | None, data_src: str | None = None, title: str | None = None) -> str:
    page = (ASSETS / 'index.html').read_text()
    css = (ASSETS / 'viewer.css').read_text()
    script = (ASSETS / 'viewer.js').read_text()
    if data_src:  # data published as a separate (gzip) file next to the page
        boot = f'<script>window.MSMS_STATIC_SRC={json.dumps(data_src)};</script>'
    else:
        payload = json.dumps(data, separators=(',', ':'), allow_nan=False).replace('</', '<\\/')
        boot = f'<script>window.MSMS_STATIC={payload};</script>'
    if title:
        page = re.sub(r'<title>.*?</title>', f'<title>{html.escape(title)}</title>', page, count=1)
    page = page.replace('<link rel="stylesheet" href="/assets/viewer.css">', f'<style>\n{css}\n</style>')
    return page.replace('<script src="/assets/viewer.js" defer></script>',
                        f'{boot}\n<script>\n{script}\n</script>')


def export_static(result_paths, output: Path, top_k: int = 5, max_exp_peaks: int = 250,
                  progress=None, split_data: bool = False, demo_reviews: bool = False,
                  title: str | None = None, demo_source_url: str | None = None,
                  demo_install_url: str | None = None) -> Path:
    """Write one review page for every result: self-contained, or with its data in a gzip file beside it
    (split_data, for hosts with a page-size limit). Reviews are read-only unless demo_reviews, which lets
    viewers try the review controls with notes kept only in their own browser."""
    paths = [Path(p).expanduser().resolve() for p in result_paths]
    index, results = [], []
    for i, path in enumerate(paths):
        summary, _ = _summarize(i, path)
        result = load_result(path)
        slim = client_result(result, top_k=top_k, max_exp_peaks=max_exp_peaks)
        if demo_source_url:
            # Public snapshots should not disclose the paths of the machine that ran the analysis.
            summary['path'] = summary['label']
            slim['input'] = Path(slim['input']).name
            for formula_result in slim.get('formula_results', []):
                formula_result.pop('atlas_mgf', None)
            for candidate in slim['candidates']:
                if candidate.get('model_checkpoint'):
                    candidate['model_checkpoint'] = Path(candidate['model_checkpoint']).name
        structures = [candidate_payload(c, result.get('adduct', '')) for c in slim['candidates']]
        index.append(summary)
        results.append({'label': summary['label'], 'result': slim, 'structures': structures,
                        'notes': _read_notes(path.with_name('review_notes.json'))})
        if progress:
            progress(i + 1, len(paths))
    data = {'index': index, 'results': results, 'top_k': top_k, 'demo_reviews': demo_reviews,
            'exported_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    if demo_source_url:
        data['demo_source_url'] = demo_source_url
    if demo_install_url:
        data['demo_install_url'] = demo_install_url
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if split_data:
        data_path = output.with_name(f'{output.stem}.data.json.gz')
        blob = gzip.compress(json.dumps(data, separators=(',', ':'), allow_nan=False).encode(), mtime=0)
        data_temporary = data_path.with_name(f'.{data_path.name}.tmp')
        data_temporary.write_bytes(blob)
        os.replace(data_temporary, data_path)
        page = _inline_page(None, data_src=data_path.name, title=title)
    else:
        page = _inline_page(data, title=title)
    temporary = output.with_name(f'.{output.name}.tmp')
    temporary.write_text(page)
    os.replace(temporary, output)
    return output


def create_app(result_paths, atlas_mgf: Path | None = None, cache_size: int = 8,
               export_path: Path | None = None):
    """Serve one or more retrieval.json results; each keeps its own review_notes.json."""
    try:
        from flask import Flask, jsonify, request, send_file, send_from_directory
    except ImportError as exc:
        raise RuntimeError('Flask is required for visualization; install this package with the [visualize] extra in your ms-pred Python') from exc
    if isinstance(result_paths, (str, Path)):
        result_paths = [result_paths]
    paths = [Path(p).expanduser().resolve() for p in result_paths]
    if not paths:
        raise ValueError('At least one retrieval.json is required')
    if atlas_mgf is not None and len(paths) > 1:
        raise ValueError('--atlas-mgf applies to a single result; results with saved fragment IDs do not need it')
    summaries, keys = zip(*(_summarize(i, p) for i, p in enumerate(paths)))
    summaries = list(summaries)
    token = secrets.token_urlsafe(24)
    common = Path(os.path.commonpath([str(p.parent) for p in paths]))
    export_path = Path(export_path) if export_path else common / 'review_report.html'
    export_state = {'state': 'idle', 'done': 0, 'total': len(paths), 'path': str(export_path), 'error': None}
    export_lock = threading.Lock()
    app = Flask(__name__, static_folder=str(ASSETS), static_url_path='/assets')

    # Results can be large (full predicted spectra); keep only recently viewed ones in memory.
    @lru_cache(maxsize=cache_size)
    def loaded(index: int):
        result = load_result(paths[index], atlas_mgf)
        return result, {candidate['review_key']: candidate for candidate in result['candidates']}

    def result_index(value) -> int | None:
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        return index if 0 <= index < len(paths) else None

    @app.get('/')
    def index():
        return send_from_directory(ASSETS, 'index.html')

    @app.get('/api/index')
    def result_list():
        return jsonify({'results': summaries, 'export': export_state, 'review_token': token})

    @app.get('/api/state')
    def state():
        index = result_index(request.args.get('result', 0))
        if index is None:
            return jsonify({'error': 'Unknown result'}), 404
        result, _ = loaded(index)
        return jsonify({'result': client_result(result), 'result_index': index,
                        'label': summaries[index]['label'],
                        'notes': _read_notes(paths[index].with_name('review_notes.json')),
                        'review_token': token})

    @app.get('/api/candidate/<int:index>/<int:candidate_index>')
    def candidate(index: int, candidate_index: int):
        if result_index(index) is None:
            return jsonify({'error': 'Unknown result'}), 404
        result, _ = loaded(index)
        if not 0 <= candidate_index < len(result['candidates']):
            return jsonify({'error': 'Unknown candidate'}), 404
        return jsonify(candidate_payload(result['candidates'][candidate_index], result.get('adduct', '')))

    @app.get('/api/thumb/<int:index>')
    def thumb(index: int):
        if result_index(index) is None or not summaries[index]['top_smiles']:
            return '', 404
        svg, _ = _structure_svg(summaries[index]['top_smiles'])
        return svg, 200, {'Content-Type': 'image/svg+xml', 'Cache-Control': 'max-age=3600'}

    def fragment_response(index: int, candidate_index: int, ce: str, peak_index: int):
        result, _ = loaded(index)
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

    @app.get('/api/fragment/<int:candidate_index>/<ce>/<int:peak_index>')
    def fragment(candidate_index: int, ce: str, peak_index: int):
        return fragment_response(0, candidate_index, ce, peak_index)

    @app.get('/api/fragment/<int:index>/<int:candidate_index>/<ce>/<int:peak_index>')
    def result_fragment(index: int, candidate_index: int, ce: str, peak_index: int):
        if result_index(index) is None:
            return jsonify({'error': 'Unknown result'}), 404
        return fragment_response(index, candidate_index, ce, peak_index)

    @app.post('/api/review')
    def save_review():
        if request.headers.get('X-Review-Token') != token:
            return jsonify({'error': 'Invalid review token'}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({'error': 'Invalid review'}), 400
        index = result_index(body.get('result', 0))
        if index is None:
            return jsonify({'error': 'Unknown result'}), 400
        _, allowed = loaded(index)
        if body.get('candidate_key') not in allowed:
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
        notes_path = paths[index].with_name('review_notes.json')
        notes = _read_notes(notes_path)
        entry = notes['candidates'].setdefault(body['candidate_key'], {'fragments': {}})
        entry['decision'] = decision
        entry['comment'] = comment
        entry['updated_at'] = datetime.now(timezone.utc).isoformat()
        if fragment_note is not None:
            fragment_key = f'{ce}:{peak_index}'
            if fragment_comment:
                entry.setdefault('fragments', {})[fragment_key] = {
                    'ce': ce, 'peak_index': peak_index, 'fragment_id': str(ids[peak_index]),
                    'mz': float(candidate['predicted_spectra'][ce][peak_index][0]),
                    'comment': fragment_comment}
            else:
                entry.setdefault('fragments', {}).pop(fragment_key, None)
        _write_notes(notes_path, notes)
        summaries[index]['review'] = _review_summary(notes, keys[index])
        summaries[index]['reviewed'] = summaries[index]['review']['reviewed']
        return jsonify({'saved': True, 'notes': notes, 'path': str(notes_path),
                        'reviewed': summaries[index]['reviewed'], 'review': summaries[index]['review']})

    def run_export():
        try:
            def progress(done, total):
                export_state['done'] = done
            export_static(paths, export_path, progress=progress)
            export_state['state'] = 'done'
        except Exception as exc:  # reported to the page
            export_state.update(state='error', error=str(exc))

    @app.post('/api/export')
    def start_export():
        if request.headers.get('X-Review-Token') != token:
            return jsonify({'error': 'Invalid review token'}), 403
        with export_lock:
            if export_state['state'] != 'running':
                export_state.update(state='running', done=0, error=None)
                threading.Thread(target=run_export, daemon=True).start()
        return jsonify(export_state)

    @app.get('/api/export')
    def export_status():
        return jsonify(export_state)

    @app.get('/export/review_report.html')
    def export_download():
        if export_state['state'] != 'done' or not export_path.exists():
            return jsonify({'error': 'No finished export'}), 404
        return send_file(export_path, mimetype='text/html', as_attachment=True,
                         download_name=export_path.name)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve a local interactive fragment viewer')
    parser.add_argument('--result', type=Path, nargs='+', required=True,
                        help='one or more retrieval.json files; several are served as one review page')
    parser.add_argument('--atlas-mgf', type=Path)
    parser.add_argument('--port', type=int, default=0, help='localhost port; 0 selects a free port')
    parser.add_argument('--export', type=Path,
                        help='write a self-contained read-only review page to this path and exit')
    parser.add_argument('--split-data', action='store_true',
                        help='with --export: write the data to <name>.data.json.gz beside the page')
    parser.add_argument('--demo-reviews', action='store_true',
                        help='with --export: let viewers try reviews, kept only in their browser')
    parser.add_argument('--title', help='with --export: page title')
    args = parser.parse_args(argv)
    if args.export:
        def progress(done, total):
            print(f'\rExporting {done}/{total}', end='', flush=True)
        path = export_static(args.result, args.export, progress=progress, split_data=args.split_data,
                             demo_reviews=args.demo_reviews, title=args.title)
        print(f'\nStatic review page: {path}', flush=True)
        return
    from werkzeug.serving import make_server
    app = create_app(args.result, args.atlas_mgf)
    server = make_server('127.0.0.1', args.port, app, threaded=True)
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
