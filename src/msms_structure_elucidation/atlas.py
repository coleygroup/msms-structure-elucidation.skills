"""Public ICEBERG atlas formula MGF download with response validation."""
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError
import uuid
import threading

BASE_URL = 'https://iceberg-ms.mit.edu'
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class AtlasNoEntry(ValueError):
    """The formula/adduct has no public atlas entry."""


def download_mgf(formula: str, adduct: str, target: Path, base_url: str = BASE_URL) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(target.resolve()), threading.Lock())
    with lock:
        return _download_mgf_locked(formula, adduct, target, base_url)


def _download_mgf_locked(formula: str, adduct: str, target: Path, base_url: str) -> Path:
    if target.exists() and target.stat().st_size:
        with target.open('rb') as existing:
            if b'BEGIN IONS' in existing.read(4096):
                return target
    url = base_url.rstrip('/') + '/download_mgf?' + urlencode({'formula': formula, 'adduct': adduct})
    try:
        with urlopen(url, timeout=90) as response:
            first = response.read(4096)
            if b'BEGIN IONS' not in first:
                final_url = response.geturl() if hasattr(response, 'geturl') else url
                if b'MGF file not found for formula' in first or final_url.rstrip('/') == base_url.rstrip('/'):
                    raise AtlasNoEntry(f'No public atlas entry for {formula} {adduct}')
                raise RuntimeError(f'Atlas returned a non-MGF response for {formula} {adduct}: {first[:120]!r}')
            tmp = target.with_name(target.name + f'.{uuid.uuid4().hex}.part')
            try:
                with tmp.open('wb') as output:
                    output.write(first)
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
    except HTTPError as exc:
        if exc.code == 404:
            raise AtlasNoEntry(f'No public atlas entry for {formula} {adduct}') from exc
        raise
    return target
