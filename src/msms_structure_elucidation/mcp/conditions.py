"""Keep shared prediction configuration independent of disposable feature directories."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import JobStore, read_json, write_json
from msms_structure_elucidation.prediction_cache import digest


def snapshot_config(root: Path, config: dict) -> Path:
    """Retain an immutable runtime snapshot for the lifetime of the shared queue."""
    if not isinstance(config,dict) or not config:
        raise ValueError('Shared prediction configuration is missing')
    folder = root/'condition-configs'
    folder.mkdir(exist_ok=True)
    target = folder/(digest(config)+'.json')
    if target.exists():
        if read_json(target)!=config:
            raise ValueError('Shared prediction configuration snapshot changed')
    else:
        write_json(target,config)
    return target


def condition_config(store: JobStore, root: Path, db: sqlite3.Connection,
                     key: str, recorded_path: str) -> dict:
    """Resolve legacy references from the same condition's consumers or native jobs."""
    path = Path(recorded_path)
    config = read_json(path)
    if isinstance(config,dict) and config and path.parent==root/'condition-configs':
        return config
    if not config:
        peers = [row[0] for row in db.execute('SELECT feature FROM consumers WHERE condition=?',(key,))]
        groups = [row[0] for row in db.execute('SELECT DISTINCT job FROM items WHERE condition=? AND job IS NOT NULL',(key,))]
        for job in peers+groups:
            candidate = store.root/job/'config.json'
            config = read_json(candidate)
            if isinstance(config,dict) and config:
                path = candidate
                break
    if not isinstance(config,dict) or not config:
        raise FileNotFoundError(f'Shared condition {key} has no retained runtime configuration: {recorded_path}')
    retained = snapshot_config(root,config)
    db.execute('UPDATE conditions SET config=? WHERE id=?',(str(retained),key))
    return config
