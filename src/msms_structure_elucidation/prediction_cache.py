"""Share raw candidate predictions across features; never cache experimental scores."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Callable


def cache_root(configured: str | None = None) -> Path:
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.environ.get('LOCALAPPDATA') if os.name == 'nt' else os.environ.get('XDG_CACHE_HOME')
    return (Path(base) if base else Path.home() / '.cache') / 'msms-structure-elucidation' / 'predictions'


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def file_lock(path: Path, on_wait: Callable | None = None, on_acquired: Callable | None = None,
              *, blocking: bool = True):
    """Release an interprocess lock automatically even when its owner exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        waited = False
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            # Nonblocking acquisition with bounded polls; Windows LK_LOCK stops after ten retries.
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not blocking:
                        yield False
                        return
                    if on_wait is not None and not waited:
                        on_wait()
                        waited = True
                    time.sleep(.2)
            try:
                if waited and on_acquired is not None:
                    on_acquired()
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            if not blocking:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                    return
            elif on_wait is None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if not waited:
                            on_wait()
                            waited = True
                        time.sleep(.2)
            try:
                if waited and on_acquired is not None:
                    on_acquired()
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lease_busy(path: Path) -> bool:
    """An open advisory lock proves liveness even across hosts; no PID or expiry guesses."""
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


@contextmanager
def candidate_guard(folder: Path, molecules: list[str], on_wait=None, on_acquired=None):
    """Protect overlapping sets with one live lease per request, independent of candidate count."""
    leases = folder / 'leases'
    leases.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    lock = leases / (token + '.lock')
    entry = leases / (token + '.json')
    keys = {digest(smi) for smi in molecules}
    waited = False
    try:
        with file_lock(lock):
            while True:
                conflict = False
                with file_lock(leases / 'registry.lock'):
                    for other in leases.glob('*.json'):
                        other_lock = other.with_suffix('.lock')
                        if not _lease_busy(other_lock):
                            # A dead writer can leave a partial JSON record. Check
                            # the kernel lease before attempting to parse it.
                            other.unlink(missing_ok=True)
                            other_lock.unlink(missing_ok=True)
                            continue
                        if keys.intersection(json.loads(other.read_text())['candidates']):
                            conflict = True
                            break
                    if not conflict:
                        entry.write_text(json.dumps({'candidates': sorted(keys)}))
                        break
                if on_wait and not waited:
                    on_wait()
                waited = True
                time.sleep(.2)
            try:
                if waited and on_acquired:
                    on_acquired()
                yield
            finally:
                with file_lock(leases / 'registry.lock'):
                    entry.unlink(missing_ok=True)
    finally:
        lock.unlink(missing_ok=True)


def file_fingerprint(path: Path, root: Path) -> str:
    """Memoize weight hashes only after the file is older than coarse timestamp ticks."""
    path = path.resolve()
    stat = path.stat()
    identity = [str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    saved = root / 'fingerprints' / (digest(identity) + '.json')
    with file_lock(saved.with_suffix('.lock')):
        stable = time.time_ns() - max(stat.st_mtime_ns, stat.st_ctime_ns) > 5_000_000_000
        if saved.is_file() and stable:
            return json.loads(saved.read_text())['sha256']
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                hasher.update(block)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
            raise RuntimeError(f'Checkpoint changed while hashing: {path}')
        saved.write_text(json.dumps({'identity': identity, 'sha256': hasher.hexdigest()}))
        return hasher.hexdigest()


def source_fingerprint(source: Path) -> str:
    source_hash = hashlib.sha256()
    for path in sorted(source.rglob('*.py')):
        source_hash.update(str(path.relative_to(source)).encode())
        source_hash.update(path.read_bytes())
    return source_hash.hexdigest()


def model_identity(model: str, checkpoints: list[str], source: Path, root: Path,
                   adduct: str, instrument: str | None, sparse_k: int) -> dict:
    return {'schema': 1, 'model': model, 'weights_sha256': [file_fingerprint(Path(p), root) for p in checkpoints],
            'source_sha256': source_fingerprint(source), 'adduct': adduct, 'instrument': instrument,
            'sparse_k': sparse_k, 'energy_unit': 'eV', 'seed': 42}


def _validate(row: dict, smiles: str, energy: str) -> None:
    if row.get('smiles') != smiles or str(row.get('energy')) != energy:
        raise RuntimeError('Prediction cache identity mismatch')
    peaks, ids = row.get('peaks'), row.get('fragment_ids')
    if not isinstance(peaks, list) or not peaks or any(
            len(p) != 2 or not all(math.isfinite(v) for v in p) or p[0] < 0 or p[1] < 0 for p in peaks):
        raise RuntimeError('Invalid raw spectrum in prediction cache')
    if ids is not None and (not isinstance(ids, list) or len(ids) != len(peaks) or
                            any(not str(i).isdigit() for i in ids)):
        raise RuntimeError('Invalid peak-aligned fragments in prediction cache')


def predictions(root: Path, identity: dict, smiles: list[str], energies: list[int],
                produce: Callable[[list[str], list[int]], list[dict]], refresh: bool = False) -> tuple[list[dict], dict]:
    """Predict only missing structure/energy pairs, with single-flight candidate locks."""
    namespace = digest(identity)
    folder = root / namespace
    folder.mkdir(parents=True, exist_ok=True)
    requested = sorted({str(int(e)) for e in energies}, key=int)
    unique = list(dict.fromkeys(smiles))
    rows = {}
    hits = 0
    database = folder / 'spectra.sqlite3'
    if database.is_file() and not refresh:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=120)) as db:
            db.execute('BEGIN')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spectra'").fetchone():
                for smi in unique:
                    for energy in requested:
                        saved = db.execute('SELECT payload FROM spectra WHERE smiles=? AND energy=?', (smi, energy)).fetchone()
                        if saved:
                            row = json.loads(saved[0])
                            _validate(row, smi, energy)
                            rows[smi, energy] = row
                            hits += 1
    if hits == len(unique) * len(requested):
        return _records(rows, unique, requested), {'namespace': namespace, 'reused_spectra': hits,
            'predicted_spectra': 0, 'identity': identity}
    missing_molecules = [smi for smi in unique if any((smi, e) not in rows for e in requested)]
    from msms_structure_elucidation.mcp.pipeline import cache_wait_callbacks
    on_wait, on_acquired = cache_wait_callbacks()
    with candidate_guard(folder, missing_molecules, on_wait, on_acquired):
        with sqlite3.connect(folder / 'spectra.sqlite3', timeout=120) as db:
            db.execute('CREATE TABLE IF NOT EXISTS spectra (smiles TEXT, energy TEXT, payload TEXT, '
                       'PRIMARY KEY(smiles, energy))')
            missing = defaultdict(list)
            for smi in unique:
                needed = []
                for energy in requested:
                    if (smi, energy) in rows:
                        continue
                    saved = None if refresh else db.execute(
                        'SELECT payload FROM spectra WHERE smiles=? AND energy=?', (smi, energy)).fetchone()
                    if saved:
                        row = json.loads(saved[0])
                        _validate(row, smi, energy)
                        rows[smi, energy] = row
                        hits += 1
                    else:
                        needed.append(energy)
                if needed:
                    missing[tuple(needed)].append(smi)
            for needed, molecules in missing.items():
                produced = produce(molecules, list(map(int, needed)))
                by_smiles = {r['smiles']: r for r in produced}
                if len(by_smiles) != len(produced) or set(by_smiles) != set(molecules):
                    raise RuntimeError('Model prediction identities differ from the requested cache candidates')
                pending = []
                for smi in molecules:
                    record = by_smiles[smi]
                    for energy in needed:
                        row = {'smiles': smi, 'energy': energy,
                               'peaks': record['predicted_spectra'].get(energy),
                               'fragment_ids': record.get('predicted_fragment_ids', {}).get(energy)}
                        _validate(row, smi, energy)
                        rows[smi, energy] = row
                        pending.append((smi, energy, json.dumps(row, allow_nan=False)))
                # Commit only after the complete requested group passed validation.
                db.executemany('INSERT OR REPLACE INTO spectra VALUES (?, ?, ?)', pending)
                db.commit()
    return _records(rows, unique, requested), {'namespace': namespace, 'reused_spectra': hits,
                     'predicted_spectra': len(unique) * len(requested) - hits, 'identity': identity}


def _records(rows: dict, unique: list[str], requested: list[str]) -> list[dict]:
    """Return independent request records from validated immutable cache rows."""
    return [{'smiles': smi, 'predicted_spectra': {e: rows[smi, e]['peaks'] for e in requested},
                'predicted_fragment_ids': {e: rows[smi, e]['fragment_ids'] for e in requested
                                           if rows[smi, e]['fragment_ids'] is not None}} for smi in unique]
