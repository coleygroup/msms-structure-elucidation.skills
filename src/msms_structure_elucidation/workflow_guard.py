"""Session-scoped MS/MS completion checks shared by Codex and Claude Code hooks.

Only the standard library is used: the client does not need model dependencies.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from stat import S_ISREG
import sys
import tempfile


SCHEMA_VERSION = 1
PREREQUISITES = ('inputs', 'feature_finding', 'collision_energy', 'model_selection', 'candidate_search')
ACTIVE_JOBS = {'pending', 'queued', 'submitted', 'running', 'waiting', 'waiting_resources'}
TERMINAL_JOBS = {'done', 'failed', 'cancelled'}
OUTCOMES = {'ranked', 'no_candidates', 'excluded', 'failed'}
ROUTES = {'agent', 'frigid', 'both', 'skip'}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, value) -> None:
    """Publish state atomically, without sharing a temporary name between writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(value, file, indent=2, allow_nan=False)
            file.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def project_root(cwd: str | Path) -> Path:
    """Find the nearest project boundary, including worktrees and installed skills."""
    path = Path(cwd).expanduser().resolve()
    instructions_root = None
    for candidate in (path, *path.parents):
        if any((candidate / marker).exists() for marker in
               ('.msms-workflow', '.git', '.codex/hooks.json', '.claude/settings.json')):
            return candidate
        if instructions_root is None and (candidate / 'AGENTS.md').exists():
            instructions_root = candidate
    return instructions_root or path


def session_key(session_id: str) -> str:
    if not session_id.strip():
        raise ValueError('A session ID is required; use the ID supplied by the workflow hook.')
    return hashlib.sha256(session_id.encode()).hexdigest()[:24]


def state_path(root: Path, session_id: str, run_id: str) -> Path:
    run_key = hashlib.sha256(run_id.encode()).hexdigest()[:24]
    return root / '.msms-workflow' / session_key(session_id) / run_key / 'state.json'


def instruction_location(root: Path, relative: str) -> str:
    """Locate shared instructions when hooks are installed in a different project."""
    checkout = Path(__file__).resolve().parents[2]
    for base in (root, checkout):
        candidate = base / relative
        if candidate.is_file():
            return str(candidate)
    return 'https://github.com/coleygroup/msms-structure-elucidation.skills/blob/main/' + relative


def start(root: Path, session_id: str, run_id: str, features: list[str], output: Path,
          inputs: list[str] | None = None) -> Path:
    """Register the full requested feature set before launching work."""
    if not run_id.strip() or not (features or inputs) or any(not str(item).strip() for item in features + (inputs or [])):
        raise ValueError('A run ID and nonempty feature or input IDs are required.')
    if len(set(features)) != len(features):
        raise ValueError('Feature IDs must be unique; qualify duplicate IDs by dataset/input.')
    path = state_path(root, session_id, run_id)
    if path.exists():
        raise ValueError(f'Run already registered; update/resume its existing state: {path}')
    data = {
        'schema_version': SCHEMA_VERSION, 'session_id': session_id, 'run_id': run_id,
        'project_root': str(root), 'started_at': now(), 'status': 'active',
        'features_finalized': bool(features), 'input_ids': inputs or [],
        'prerequisites': {name: {'status': 'pending', 'reason': '', 'evidence': []}
                          for name in PREREQUISITES},
        'features': {item: {'outcome': 'pending', 'result': '', 'review': ''} for item in features},
        'jobs': [], 'report': {'required': True, 'path': str(output / 'review_report.html')},
        'final_assessment': str(output / 'final_assessment.md'),
    }
    write_json(path, data)
    write_json(path.with_name('request.json'), {'run_id': run_id, 'session_id': session_id,
                                               'feature_ids': features, 'input_ids': inputs or [],
                                               'started_at': data['started_at']})
    return path


def set_features(path: Path, features: list[str], source: str) -> None:
    """Freeze feature-finding output once, preserving the original input request."""
    data = read_json(path)
    if data.get('features_finalized') or data['features']:
        raise ValueError('The feature set is already frozen; do not replace or shrink it.')
    if not features or any(not isinstance(item, str) or not item.strip() for item in features) or len(set(features)) != len(features):
        raise ValueError('Supply a nonempty list of unique string feature IDs.')
    root = Path(data['project_root'])
    manifest = readable(root, source)
    data['features'] = {item: {'outcome': 'pending', 'result': '', 'review': ''} for item in features}
    data['features_finalized'] = True
    request = read_json(path.with_name('request.json'))
    request.update(feature_ids=features, feature_manifest=str(manifest), feature_manifest_sha256=sha256(manifest))
    write_json(path.with_name('request.json'), request)
    write_json(path, data)


def artifact(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing artifact path')
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def readable(root: Path, value: str, *, json_file: bool = False):
    path = artifact(root, value)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        raise ValueError(f'Missing or empty artifact: {path}') from None
    if not S_ISREG(metadata.st_mode) or not metadata.st_size:
        raise ValueError(f'Missing or empty artifact: {path}')
    if json_file:
        return read_json(path)
    with path.open('rb') as file:
        file.read(1)
    return path


def evidence(root: Path, record: dict, *, checked: set[Path] | None = None) -> None:
    if not isinstance(record.get('reason'), str) or not record['reason'].strip():
        raise ValueError('Record a reason and its supporting evidence.')
    paths = record.get('evidence')
    if not isinstance(paths, list) or not paths:
        raise ValueError('At least one evidence artifact is required.')
    for path in paths:
        target = artifact(root, path)
        if checked is None or target not in checked:
            readable(root, path)
            if checked is not None:
                checked.add(target)


def review_result(root: Path, feature_id: str, feature: dict) -> dict:
    """Require a substantive, final review tied to the current result bytes."""
    review = readable(root, feature.get('review', ''), json_file=True)
    result = readable(root, feature.get('result', ''), json_file=True)
    if not isinstance(result, dict):
        raise ValueError('The result must be a JSON object.')
    if result.get('prediction_complete') is False or result.get('status') not in {'ranked', 'no_candidates'}:
        raise ValueError('Result coverage is incomplete or its status needs resolution.')
    if result['status'] != feature['outcome']:
        raise ValueError('Recorded outcome disagrees with the result status.')
    if result['status'] == 'ranked' and not result.get('candidates'):
        raise ValueError('A ranked result must contain candidates.')
    if any(item.get('status') in {'error', 'failed', 'not_processed', 'blocked_model_assets'}
           for item in result.get('formula_results', [])):
        raise ValueError('The candidate search contains failed or unprocessed formulas.')
    if review.get('feature_id') != feature_id or review.get('coverage') != 'complete':
        raise ValueError('Review must identify this feature and have complete coverage.')
    result_path = artifact(root, feature['result']).resolve()
    if artifact(root, review.get('source_result', '')).resolve() != result_path:
        raise ValueError('Review names a different source result.')
    if review.get('source_sha256') != sha256(result_path):
        raise ValueError('Review is stale: source result hash changed.')
    reviewed = datetime.fromisoformat(review.get('reviewed_at', '').replace('Z', '+00:00'))
    if reviewed.tzinfo is None:
        raise ValueError('Review time must include a timezone.')
    if not isinstance(review.get('assessment'), str) or not review['assessment'].strip():
        raise ValueError('Review assessment is missing.')
    for key in ('candidates_considered', 'peak_evidence', 'supporting_evidence', 'conflicting_evidence', 'ambiguities'):
        if not isinstance(review.get(key), list):
            raise ValueError(f'Review must include a {key} list (empty when appropriate).')
    if result['status'] == 'ranked' and (not review['candidates_considered'] or not review['peak_evidence']):
        raise ValueError('Ranked results need candidate-specific peak evidence, including strong matches.')
    for key in ('weak_evidence', 'important_unexplained_peaks'):
        if not isinstance(review.get(key), bool):
            raise ValueError(f'Review must explicitly assess {key}.')
    if result['status'] == 'no_candidates' and not review['weak_evidence']:
        raise ValueError('A no-candidate outcome needs a weak-evidence assessment and user route choice.')
    energies = review.get('energies_reviewed')
    if not isinstance(energies, list) or set(map(str, energies)) != set(map(str, result.get('spectra', {}))):
        raise ValueError('Review must record energies_reviewed matching every observed spectrum key.')
    return review


def route_issues(root: Path, feature: dict, review: dict, *, checked: set[Path] | None = None) -> list[str]:
    route = feature.get('denovo', {})
    needed = review['weak_evidence'] or review['important_unexplained_peaks']
    if not needed and not route:
        return []
    if route.get('choice') not in ROUTES or route.get('source') != 'user':
        return ['Record the user-selected de novo route: agent, frigid, both or skip.']
    evidence(root, route, checked=checked)
    selected = ('agent', 'frigid') if route['choice'] == 'both' else (route['choice'],)
    issues = []
    for name in selected:
        if name == 'skip':
            continue
        completed = route.get('routes', {}).get(name, {})
        if completed.get('status') != 'done':
            issues.append(f'Finish the selected {name} proposal and scoring route.')
            continue
        readable(root, completed.get('proposals', ''), json_file=True)
        scored = readable(root, completed.get('scored_result', ''), json_file=True)
        if not isinstance(scored, dict) or scored.get('status') not in {'ranked', 'no_candidates'}:
            issues.append(f'{name} proposals still need validated model scoring.')
        if scored.get('prediction_complete') is False:
            issues.append(f'{name} scoring has incomplete prediction coverage.')
    return issues


def feature_signature(root: Path, feature: dict) -> str:
    """Cache validated evidence by file identity, size and nanosecond modification time."""
    paths = [feature.get('result', ''), feature.get('review', '')]
    route = feature.get('denovo', {})
    paths.extend(route.get('evidence', []))
    for completed in route.get('routes', {}).values():
        paths.extend([completed.get('proposals', ''), completed.get('scored_result', '')])
    stamps = []
    for value in paths:
        if not value:
            stamps.append((value, None))
            continue
        path = artifact(root, value)
        try:
            stat = path.stat()
        except FileNotFoundError:
            stamps.append((str(path), None))
            continue
        stamps.append((str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return hashlib.sha256(json.dumps([feature, stamps], sort_keys=True).encode()).hexdigest()


def inspect_state(path: Path, *, force: bool = False, stop_on_issue: bool = False) -> dict:
    """Validate fully for check/finish; Stop may return the first proven blocking issue."""
    data = read_json(path)
    root = Path(data['project_root'])
    issues = []
    checked: set[Path] = set()
    status = data.get('status')
    # Scoped holds require the complete issue set to establish what may stop.
    short = stop_on_issue and not force and status in {'active', 'completed', 'completed_with_failures'}

    def summary(allow_stop: bool = False, *, inspection_complete: bool = False) -> dict:
        return {'run_id': data['run_id'], 'status': status, 'allow_stop': allow_stop,
                'complete': not issues, 'inspection_complete': inspection_complete, 'issues': issues,
                'outcomes': dict(Counter(item.get('outcome', 'pending') for item in data['features'].values()))}

    cache_path = path.with_name('validation_cache.json')
    validator_hash = sha256(Path(__file__))
    try:
        saved_cache = read_json(cache_path) if cache_path.exists() else {}
        if not isinstance(saved_cache, dict):
            saved_cache = {}
        cache = saved_cache.get('features', {}) if saved_cache.get('validator_sha256') == validator_hash else {}
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):  # an interrupted cache is safe to rebuild from source artifacts
        cache = {}
    next_cache = {}

    def add(kind: str, detail: str, feature_id: str | None = None, job_id: str | None = None):
        issues.append({'kind': kind, 'detail': detail, **({'feature_id': feature_id} if feature_id else {}),
                       **({'job_id': job_id} if job_id else {})})

    if data.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported workflow state schema.')
    if not data.get('features_finalized') or not data.get('features'):
        add('prerequisite', 'feature_finding: Freeze the complete feature set after feature finding using set-features.')
    request = read_json(path.with_name('request.json'))
    if (set(request['feature_ids']) != set(data['features']) or
            request['session_id'] != data['session_id'] or request['run_id'] != data['run_id'] or
            request.get('input_ids', []) != data.get('input_ids', [])):
        add('request', 'Feature accounting or analysis identity differs from the registered request.')
    if request.get('feature_manifest') and sha256(artifact(root, request['feature_manifest'])) != request['feature_manifest_sha256']:
        add('request', 'Feature manifest changed after the requested feature set was frozen.')
    if short and issues:
        return summary()
    for name in PREREQUISITES:
        record = data.get('prerequisites', {}).get(name, {})
        try:
            if record.get('status') not in {'done', 'not_applicable'}:
                raise ValueError('Resolve this prerequisite and record its evidence.')
            evidence(root, record, checked=checked)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            add('prerequisite', f'{name}: {exc}')
        if short and issues:
            return summary()

    active_jobs = False
    if not isinstance(data.get('jobs'), list):
        raise ValueError('jobs must be a list of registered job receipts.')
    for job in data['jobs']:
        try:
            job_status = readable(root, job.get('status_path', ''), json_file=True)
            state = job_status.get('state')
            if state in ACTIVE_JOBS:
                active_jobs = True
                raise ValueError(f'{state}; keep polling and review available features.')
            if state not in TERMINAL_JOBS or not job_status.get('finished'):
                raise ValueError('Missing terminal job state and finish time.')
            if type(job_status.get('exit_code')) is not int:
                raise ValueError('Missing verified exit_code in terminal job receipt.')
            if state == 'done' and job_status['exit_code'] != 0:
                raise ValueError('Successful job receipt has a nonzero exit code.')
            if state in {'failed', 'cancelled'}:
                evidence(root, job, checked=checked)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            add('job', f'{job.get("job_id", "unnamed job")}: {exc}', job_id=job.get('job_id'))
        if short and issues:
            return summary()

    ready_reviews = False
    for feature_id, feature in data['features'].items():
        outcome = feature.get('outcome')
        if outcome not in OUTCOMES:
            add('feature', f'Feature outcome is {outcome!r}; finish or account for it.', feature_id)
            if short:
                return summary()
            # A published result must be reviewed even if the state still says pending.
            if feature.get('result') and artifact(root, feature['result']).is_file():
                ready_reviews = True
            continue
        if outcome in {'excluded', 'failed'}:
            try:
                evidence(root, feature, checked=checked)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                add('feature', str(exc), feature_id)
            if short and issues:
                return summary()
            continue
        try:
            signature = feature_signature(root, feature)
            if not force and cache.get(feature_id) == signature:
                next_cache[feature_id] = signature
                continue
            review = review_result(root, feature_id, feature)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            ready_reviews = True
            add('review', str(exc), feature_id)
            if short:
                return summary()
            continue
        try:
            route_problems = route_issues(root, feature, review, checked=checked)
            for detail in route_problems:
                add('route', detail, feature_id)
            if not route_problems:
                next_cache[feature_id] = signature
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            add('route', str(exc), feature_id)
        if short and issues:
            return summary()

    report = data.get('report', {})
    try:
        if report.get('required') is False:
            evidence(root, report, checked=checked)
        elif report.get('required') is True:
            page = readable(root, report.get('path', ''))
            with page.open(encoding='utf-8') as file:
                if '<html' not in file.read(4096).lower():
                    raise ValueError('Final report is not an HTML document.')
        else:
            raise ValueError('Specify whether the final HTML report is required.')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        add('report', str(exc))
    try:
        readable(root, data.get('final_assessment', ''))
    except (OSError, ValueError, TypeError) as exc:
        add('assessment', str(exc))

    allow_stop = not issues
    hold = data.get('hold', {})
    if status == 'paused':
        evidence(root, hold, checked=checked)
        if hold.get('source') != 'user':
            raise ValueError('A pause requires an explicit user instruction.')
        allow_stop = True
    elif status in {'awaiting_user', 'blocked'}:
        evidence(root, hold, checked=checked)
        held_features = set(hold.get('feature_ids', []))
        held_prerequisites = set(hold.get('prerequisites', []))
        held_jobs = set(hold.get('job_ids', []))
        unfinished = {key for key, value in data['features'].items() if value.get('outcome') not in OUTCOMES}
        waiting_issues = [item for item in issues if item['kind'] not in {'report', 'assessment'}]
        covered = all(
            (item.get('feature_id') in held_features and item['kind'] in {'feature', 'route'}) or
            (item['kind'] == 'prerequisite' and item['detail'].split(':', 1)[0] in held_prerequisites) or
            (status == 'blocked' and item['kind'] == 'job' and item.get('job_id') in held_jobs)
            for item in waiting_issues)
        # Waiting for a decision never excuses other running jobs or unread results.
        allow_stop = bool(waiting_issues) and covered and unfinished <= held_features and not active_jobs and not ready_reviews
    elif status not in {'active', 'completed', 'completed_with_failures'}:
        raise ValueError(f'Unknown analysis status: {status!r}')
    if next_cache != cache:
        write_json(cache_path, {'validator_sha256': validator_hash, 'features': next_cache})
    return summary(allow_stop, inspection_complete=True)


def hook(payload: dict, root: Path) -> dict:
    """Use one wire format accepted by both clients, scoped to this session."""
    session_id = payload.get('session_id', '')
    event = payload.get('hook_event_name', 'Stop')
    workflow = instruction_location(root, '.agents/workflows/msms-elucidation.md')
    guide = instruction_location(root, 'docs/workflow-hooks.md')
    if event in {'SessionStart', 'UserPromptSubmit'}:
        resumed = []
        if event == 'UserPromptSubmit' and session_id:
            for path in (root / '.msms-workflow' / session_key(session_id)).glob('*/state.json'):
                data = read_json(path)
                if data.get('status') in {'awaiting_user', 'blocked'}:
                    data.setdefault('hold_history', []).append({'ended_at': now(), **data.pop('hold', {})})
                    data['status'] = 'active'
                    write_json(path, data)
                    resumed.append(data['run_id'])
        context = (f'MS/MS workflow session_id: {session_id}. For an MS/MS analysis, read '
                   f'the project AGENTS.md when present and the workflow at {workflow}. Register the requested analysis '
                   'before launching work with msms-workflow-guard start --session-id '
                   f'{session_id} --run-id <label> --feature <id> --output-dir <results>. '
                   f'Use {guide} for state, review and hold records. '
                   'Do not register ordinary questions, setup-only work or repository edits.')
        if resumed:
            context += (' Reassess these pending holds against the new user message before stopping: ' +
                        ', '.join(resumed) + '. They are active again; retain a scoped hold if input is still missing.')
        return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': context}}
    if event != 'Stop':
        return {}
    if not session_id:
        return {'decision': 'block', 'reason': 'Workflow Stop hook input is missing session_id; repair hook configuration.'}
    paths = sorted((root / '.msms-workflow' / session_key(session_id)).glob('*/state.json'))
    problems = []
    for path in paths:
        try:
            data = read_json(path)
            if data.get('session_id') != session_id:
                raise ValueError('Stored session ID does not match the owning directory.')
            result = inspect_state(path, stop_on_issue=True)
            if result['complete'] and result['status'] not in {'completed', 'completed_with_failures', 'paused'}:
                result['allow_stop'] = False
                result['issues'].append({'kind': 'finalize', 'detail':
                                        f'Run msms-workflow-guard finish --state {path} to verify and write the completion receipt.'})
            if result['status'] in {'completed', 'completed_with_failures'}:
                receipt = read_json(path.with_name('completion_receipt.json'))
                if (receipt.get('state_sha256') != sha256(path) or
                        receipt.get('request_sha256') != sha256(path.with_name('request.json'))):
                    result['allow_stop'] = False
                    result['issues'].append({'kind': 'finalize', 'detail': 'Completion receipt is stale; recheck and finish again.'})
            if not result['allow_stop']:
                details = '; '.join(f'{item.get("feature_id", item["kind"])}: {item["detail"]}'
                                    for item in result['issues'][:8])
                problems.append(f'{result["run_id"]} ({path}): {details}')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f'Unreadable workflow state {path}: {exc}. Repair it; do not discard the analysis.')
    if not problems:
        return {}
    return {'decision': 'block', 'reason': (
        'The requested MS/MS workflow is unfinished. ' + '\n'.join(problems) +
        f'\nRead {workflow} and {guide}; inspect actual jobs and artifacts, '
        'review ready results, and continue authorized work. Poll resource waits with bounded waits '
        'and keep the user informed. If all remaining work genuinely requires user input/external '
        'intervention, record a scoped awaiting_user/blocked hold with evidence. Pause only on an '
        'explicit user instruction. Do not mark steps complete just to satisfy the hook.')}


def install(root: Path, command: str) -> list[str]:
    """Merge only our handlers, preserving existing client settings and other hooks."""
    paths = [root / '.codex/hooks.json', root / '.claude/settings.json']
    for path in paths:
        data = read_json(path) if path.exists() else {}
        events = data.setdefault('hooks', {})
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            groups = events.setdefault(event, [])
            for group in groups:
                group['hooks'] = [item for item in group.get('hooks', [])
                                  if item.get('statusMessage') != 'MS/MS workflow guard']
            groups[:] = [group for group in groups if group.get('hooks')]
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 30,
                                      'statusMessage': 'MS/MS workflow guard'}]})
        write_json(path, data)
    ignore = root / '.gitignore'
    content = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    if '.msms-workflow/' not in content.splitlines():
        with ignore.open('a', encoding='utf-8') as file:
            file.write(('\n' if content and not content.endswith('\n') else '') + '.msms-workflow/\n')
    return [str(path) for path in paths]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog='msms-workflow-guard')
    commands = result.add_subparsers(dest='command', required=True)
    handler = commands.add_parser('hook', help='read a client hook JSON payload from stdin')
    handler.add_argument('--project-dir')
    installer = commands.add_parser('install', help='merge hooks into both clients in a project')
    installer.add_argument('--project-dir', default='.')
    installer.add_argument('--command', dest='hook_command', default='msms-workflow-guard hook',
                           help='hook command; default requires this executable on the client PATH')
    starter = commands.add_parser('start', help='register an analysis before launching it')
    starter.add_argument('--project-dir', default='.')
    starter.add_argument('--session-id', required=True)
    starter.add_argument('--run-id', required=True)
    starter.add_argument('--feature', action='append', default=[])
    starter.add_argument('--input', action='append', default=[], help='input ID before feature finding settles')
    starter.add_argument('--feature-ids', help='JSON array of unique requested feature IDs')
    starter.add_argument('--output-dir', required=True)
    enumerator = commands.add_parser('set-features', help='freeze all feature IDs once after feature finding')
    enumerator.add_argument('--state', required=True)
    enumerator.add_argument('--feature-ids', required=True, help='JSON array of unique feature IDs')
    enumerator.add_argument('--evidence', required=True, help='feature manifest/table to hash and retain')
    reviewer = commands.add_parser('review-check', help='validate a feature review and optionally publish it atomically')
    reviewer.add_argument('--project-dir', default='.')
    reviewer.add_argument('--feature-id', required=True)
    reviewer.add_argument('--result', required=True)
    reviewer.add_argument('--review', required=True, help='draft evidence_review.json')
    reviewer.add_argument('--publish', help='coordinator-owned canonical evidence_review.json path')
    for name in ('check', 'finish'):
        command = commands.add_parser(name)
        command.add_argument('--state', required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == 'hook':
            payload = json.load(sys.stdin)
            root = Path(args.project_dir).resolve() if args.project_dir else project_root(payload.get('cwd', os.getcwd()))
            print(json.dumps(hook(payload, root)))
            return 0
        if args.command == 'install':
            print(json.dumps({'installed': install(Path(args.project_dir).resolve(), args.hook_command)}, indent=2))
            return 0
        if args.command == 'start':
            root = project_root(args.project_dir)
            features = args.feature + (read_json(Path(args.feature_ids)) if args.feature_ids else [])
            if any(not isinstance(item, str) for item in features):
                raise ValueError('Feature IDs must be strings.')
            output = artifact(root, args.output_dir).resolve()
            path = start(root, args.session_id, args.run_id, features, output, args.input)
            print(json.dumps({'state': str(path)}, indent=2))
            return 0
        if args.command == 'set-features':
            path = Path(args.state).resolve()
            set_features(path, read_json(Path(args.feature_ids)), args.evidence)
            print(json.dumps({'state': str(path)}, indent=2))
            return 0
        if args.command == 'review-check':
            root = project_root(args.project_dir)
            result_path = artifact(root, args.result)
            result = readable(root, args.result, json_file=True)
            review = review_result(root, args.feature_id, {
                'outcome': result.get('status'), 'result': str(result_path), 'review': args.review})
            published = None
            if args.publish:
                target = artifact(root, args.publish)
                if target.resolve() == result_path.resolve():
                    raise ValueError('The published review must not overwrite its source result.')
                write_json(target, review)
                published = str(target)
            print(json.dumps({'valid': True, 'feature_id': args.feature_id,
                              'source_sha256': review['source_sha256'], 'published': published}, indent=2))
            return 0
        path = Path(args.state).resolve()
        result = inspect_state(path, force=args.command == 'finish')
        if args.command == 'finish' and result['complete']:
            data = read_json(path)
            data['status'] = 'completed_with_failures' if result['outcomes'].get('failed') else 'completed'
            data['finished_at'] = now()
            write_json(path, data)
            write_json(path.with_name('completion_receipt.json'), {
                'run_id': data['run_id'], 'finished_at': data['finished_at'],
                'status': data['status'], 'outcomes': result['outcomes'],
                'state_sha256': sha256(path), 'request_sha256': sha256(path.with_name('request.json')),
            })
            result['status'] = data['status']
        print(json.dumps(result, indent=2))
        return 0 if result['complete'] else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if args.command == 'hook':
            print(json.dumps({'decision': 'block', 'reason': f'MS/MS workflow checker error: {exc}. Repair the checker input/state.'}))
            return 0
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
