"""Shared, portable workflow settings."""
from __future__ import annotations

import os
import ast
import hashlib
import socket
import sys
from pathlib import Path

REPO_CONFIG = Path(__file__).resolve().parents[2] / 'configs/default.yaml'
DEFAULT_CONFIG = REPO_CONFIG if REPO_CONFIG.is_file() else Path(__file__).with_name('default.yaml')
# Host-specific overrides written by `setup`; kept out of Git.
LOCAL_CONFIG = Path(os.environ.get('MSMS_LOCAL_CONFIG') or (REPO_CONFIG.with_name('local.yaml')
    if REPO_CONFIG.is_file() else Path.cwd() / 'configs/local.yaml')).expanduser().resolve()


def read_yaml(source: Path) -> dict:
    try:
        import yaml
        data = yaml.safe_load(source.read_text()) or {}
    except ImportError:
        data = _simple_yaml(source.read_text())
    if not isinstance(data, dict):
        raise ValueError(f'Workflow config must be a YAML mapping: {source}')
    return data


def overlay(base: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            overlay(base[key], value)
        else:
            base[key] = value


def settings(path: str | None = None) -> dict:
    """Load the given config, or the default config overlaid with configs/local.yaml."""
    explicit = path or os.environ.get('MSMS_CONFIG')
    source = Path(explicit or DEFAULT_CONFIG).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Workflow config not found: {source}')
    data = read_yaml(source)
    digest = hashlib.sha256(source.read_bytes())
    if not explicit and LOCAL_CONFIG.is_file():
        local = read_yaml(LOCAL_CONFIG)
        profile = local.get('host_profile') or {}
        if profile.get('hostname') and profile['hostname'] != socket.gethostname():
            print(f"Warning: {LOCAL_CONFIG} was tuned on {profile['hostname']}, not {socket.gethostname()}; "
                  'rerun `msms-structure-elucidation setup`.', file=sys.stderr)
        overlay(data, local)
        digest.update(LOCAL_CONFIG.read_bytes())
        data['_local_config_path'] = str(LOCAL_CONFIG)
    data['_config_path'] = str(source)
    data['_config_digest'] = digest.hexdigest()
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
