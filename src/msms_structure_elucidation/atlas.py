"""Public ICEBERG atlas formula MGF download with response validation."""
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

BASE_URL = 'https://iceberg-ms.mit.edu'


def download_mgf(formula: str, adduct: str, target: Path, base_url: str = BASE_URL) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size:
        with target.open('rb') as existing:
            if b'BEGIN IONS' in existing.read(4096):
                return target
    url = base_url.rstrip('/') + '/download_mgf?' + urlencode({'formula': formula, 'adduct': adduct})
    with urlopen(url, timeout=90) as response:
        first = response.read(4096)
        if b'BEGIN IONS' not in first:
            raise ValueError(f'Atlas returned no MGF for {formula} {adduct}; check formula or service availability')
        tmp = target.with_suffix(target.suffix + '.part')
        with tmp.open('wb') as output:
            output.write(first)
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        tmp.replace(target)
    return target
