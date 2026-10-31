"""Persist pipeline settings and verified answers in a local config."""
from __future__ import annotations

import json
import re
from pathlib import Path

from msms_structure_elucidation import config
from msms_structure_elucidation.hostsetup import save_local

DATASET_QUESTIONS = {'collision_unit', 'collision_energies', 'stepped_collision_energies', 'acquisition_mode', 'formula'}
METADATA_QUESTIONS = DATASET_QUESTIONS - {'formula'}


def _name(value: str) -> str:
    """Require a portable YAML mapping key for a question, scope or setting."""
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', value):
        raise ValueError(f'Use letters, digits, underscores or hyphens for config keys/scopes: {value}')
    return value


def _mapping_keys(value: object) -> None:
    """Validate mapping keys before writing with the shared YAML serializer."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('Config mapping keys must be strings')
            _name(key)
            _mapping_keys(item)
    elif isinstance(value, list):
        for item in value:
            _mapping_keys(item)


def _value(text: str):
    """Accept JSON values or an unquoted string for a preference."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = text
    _mapping_keys(value)
    return value


def confirmed_answer(settings: dict, question: str, scope: str = 'project',
                     context: dict | None = None):
    """Reuse an answer only within the exact saved scope and context."""
    pipeline = settings.get('pipeline') or {}
    if not pipeline.get('reuse_answers', True):
        return None
    if question in DATASET_QUESTIONS and scope == 'project':
        return None
    record = ((pipeline.get('answers') or {}).get(scope) or {}).get(question)
    if not record or record.get('source') not in ('user', 'metadata') or record.get('context', {}) != (context or {}):
        return None
    if record['source'] == 'metadata' and question not in METADATA_QUESTIONS:
        return None
    return record.get('value')


def command(args) -> int:
    """Read preferences or merge explicit settings and confirmed answers into a config."""
    path = Path(args.config).expanduser().resolve() if args.config else config.LOCAL_CONFIG
    scope = _name(args.scope)
    context = json.loads(args.context_json)
    if args.source == 'metadata' and not context:
        raise ValueError('Metadata-derived settings need --context-json identifying their acquisition evidence')
    if not isinstance(context, dict):
        raise ValueError('--context-json must be a JSON object')
    _mapping_keys(context)
    update = {}
    for key, raw in args.set or []:
        target = update
        parts = key.split('.')
        for part in parts:
            _name(part)
        if parts[:2] == ['pipeline', 'answers']:
            raise ValueError('Use --answer to record user-confirmed answers')
        for part in parts[:-1]:
            if part in target and not isinstance(target[part], dict):
                raise ValueError(f'Conflicting setting paths: {key}')
            target = target.setdefault(part, {})
        target[parts[-1]] = _value(raw)
    for question, raw in args.answer or []:
        _name(question)
        if args.source == 'metadata' and question not in METADATA_QUESTIONS:
            raise ValueError('Metadata provenance is for explicit acquisition-energy settings, not user workflow choices')
        if question in DATASET_QUESTIONS and (scope == 'project' or not context):
            raise ValueError(f'{question} needs a dataset/spectrum --scope and non-empty --context-json')
        if question == 'model_selection' and not context:
            raise ValueError('model_selection needs --context-json identifying the host and exact weights')
        value = _value(raw)
        if value is None:
            raise ValueError('A null value is not a confirmed answer')
        answers = update.setdefault('pipeline', {}).setdefault('answers', {}).setdefault(scope, {})
        answers[question] = {'value': value, 'source': args.source, 'context': context}
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_local(update, path)
    effective = config.read_yaml(config.DEFAULT_CONFIG)
    if path.is_file():
        config.overlay(effective, config.read_yaml(path))
    result = {'config': str(path), 'feature_detect': effective.get('feature_detect', {}),
              'pipeline': effective.get('pipeline', {})}
    if args.get:
        result['answer'] = confirmed_answer(effective, _name(args.get), scope, context)
    print(json.dumps(result, indent=2))
    return 0
