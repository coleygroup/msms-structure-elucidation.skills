"""Gate production routes with a canary and hold repeated environment failures."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import ACTIVE, read_json, write_json
from msms_structure_elucidation.prediction_cache import file_lock


def failure_signature(message: str) -> str | None:
    """Classify environment faults; malformed individual spectra never stop unrelated work."""
    text = message.lower()
    for signature, terms in {
        'tls_certificate': ('certificate_verify_failed', 'certificate verify failed'),
        'missing_dependency': ('modulenotfounderror:', 'no module named '),
        'model_configuration': ('checkpoint required', 'checkpoint does not match', 'native source differs',
                                'package source differs', 'checkpoints are required'),
        'pubchem_rate_limit': ('http error 429', 'too many requests'),
    }.items():
        if any(term in text for term in terms):
            return signature
    return None


class Admission:
    """Persist route canaries and environment holds across dispatcher restarts."""

    def __init__(self, store, config: dict):
        self.store, self.config = store, config
        self.path = store.root / 'admission.json'
        self.threshold = int(config.get('mcp', {}).get('environment_failure_limit', 3))
        if self.threshold < 1:
            raise ValueError('environment_failure_limit must be positive')

    def scope(self, request: dict) -> str:
        family = 'pubchem' if request['task'].startswith('search_pubchem_') else request['task']
        params = request.get('params', {})
        identity = params.get('_model_identity', {})
        version = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() if identity else (
            self.config.get('_config_digest') or 'configured')
        return ':'.join((family, params.get('model', 'iceberg'), str(version)))

    def status(self) -> dict:
        return read_json(self.path, {})

    def summary(self) -> dict:
        """Expose operational state without sending the attempt ledger to MCP clients."""
        return {scope: {key: value for key, value in row.items() if key != 'attempts'}
                for scope, row in self.status().items()}

    def _hold(self, job_id: str, hold: dict) -> None:
        if self.store.status(job_id).get('admission_hold') != hold:
            self.store.set_status(job_id, 'queued', admission_hold=hold)

    def admit(self, job_id: str) -> bool:
        """Admit one PubChem canary before expanding a previously unverified route."""
        request = self.store.request(job_id)
        if request.get('kind') != 'pipeline':
            return True
        scope = self.scope(request)
        with file_lock(self.path.with_suffix('.lock')):
            data = self.status()
            row = data.setdefault(scope, {})
            if row.get('hold'):
                self._hold(job_id, {'scope': scope, **row['hold']})
                return False
            canary = self.config.get('mcp', {}).get('route_canary', True) and scope.startswith('pubchem:')
            if canary and not row.get('verified'):
                owner = row.get('canary')
                if owner and owner != job_id:
                    status = self.store.status(owner)
                    if status['state'] in ACTIVE or self.store.scheduler_pending(owner, status):
                        self._hold(job_id, {
                            'scope': scope, 'reason': 'Waiting for production-route canary', 'job_id': owner})
                        return False
                    if status['state'] == 'done':
                        row['verified'] = {'job_id': owner, 'observed': time.time()}
                if not row.get('verified'):
                    row['canary'] = job_id
            write_json(self.path, data)
        return True

    def record(self, job_id: str, state: str, message: str = '') -> None:
        """Account for a completed attempt once, and stop admissions on a bad canary."""
        request = self.store.request(job_id)
        if request.get('kind') != 'pipeline':
            return
        if state == 'done' and self.store.scheduler_pending(job_id):
            return
        signature = failure_signature(message)
        scope = self.scope(request)
        with file_lock(self.path.with_suffix('.lock')):
            data = self.status()
            row = data.setdefault(scope, {})
            token = request.get('attempt') or hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            attempts = row.setdefault('attempts', [])
            if token in attempts:
                return
            attempts.append(token)
            # Keep environment accounting bounded; failures are counts, not a history scan.
            del attempts[:-1024]
            if state == 'done':
                row['verified'] = {'job_id': job_id, 'observed': time.time()}
                if not row.get('hold'):
                    row['failures'] = {}
            elif signature:
                failures = row.setdefault('failures', {})
                failures[signature] = failures.get(signature, 0) + 1
                if failures[signature] >= self.threshold or row.get('canary') == job_id:
                    row['hold'] = {'signature': signature, 'job_id': job_id, 'observed': time.time(),
                        'reason': 'Environment failure; verify the production route and explicitly resume admission'}
            write_json(self.path, data)

    def resume(self, scope: str, reason: str) -> dict:
        """Clear a verified environment hold; the next queued request is again a canary."""
        if not reason.strip():
            raise ValueError('A repair/validation reason is required')
        with file_lock(self.path.with_suffix('.lock')):
            data = self.status()
            if scope not in data:
                raise ValueError('Unknown admission scope')
            data[scope] = {'resumed': {'reason': reason, 'observed': time.time()}}
            write_json(self.path, data)
        return data[scope]
