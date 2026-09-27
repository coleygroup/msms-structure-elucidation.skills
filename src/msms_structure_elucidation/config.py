"""Shared, portable workflow settings."""
from __future__ import annotations

import os
import ast
from pathlib import Path

REPO_CONFIG = Path(__file__).resolve().parents[2] / 'configs/default.yaml'
DEFAULT_CONFIG = REPO_CONFIG if REPO_CONFIG.is_file() else Path(__file__).with_name('default.yaml')


def settings(path: str | None = None) -> dict:
    source = Path(path or os.environ.get('MSMS_CONFIG') or DEFAULT_CONFIG).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Workflow config not found: {source}')
    try:
        import yaml
        data = yaml.safe_load(source.read_text()) or {}
    except ImportError:
        data = _simple_yaml(source.read_text())
    if not isinstance(data, dict):
        raise ValueError(f'Workflow config must be a YAML mapping: {source}')
    data['_config_path'] = str(source)
    return data


def _simple_yaml(text: str) -> dict:
    """Read the shipped mapping-only config without a runtime YAML dependency."""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split(' #', 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        key, sep, value = line.strip().partition(':')
        if not sep or not key:
            raise ValueError('Unsupported YAML syntax; install PyYAML for extended YAML')
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        value = value.strip()
        if not value:
            parent[key] = {}
            stack.append((indent, parent[key]))
        elif value.lower() in ('null', 'none'):
            parent[key] = None
        elif value.lower() in ('true', 'false'):
            parent[key] = value.lower() == 'true'
        else:
            try:
                parent[key] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parent[key] = value
    return root


def choice(cli, env: str | None, configured):
    return cli if cli is not None else os.environ.get(env) if env and env in os.environ else configured


def asset(path: str | None, config: dict) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(config['_config_path']).parent / p
    return str(p.resolve())
