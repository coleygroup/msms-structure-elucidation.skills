"""Behavioral checks for the shared client completion gate, without model inference."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from msms_structure_elucidation import workflow_guard as guard


class WorkflowGuardTests(unittest.TestCase):
    def test_install_cli_preserves_subcommand_and_uses_default_or_custom_hook(self):
        for command in ('msms-workflow-guard hook', 'configured-python -m msms_structure_elucidation.workflow_guard hook'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                args = ['install', '--project-dir', tmp]
                if command != 'msms-workflow-guard hook':
                    args += ['--command', command]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(guard.main(args), 0)
                for name in ('.codex/hooks.json', '.claude/settings.json'):
                    events = guard.read_json(Path(tmp) / name)['hooks']
                    for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
                        self.assertEqual(events[event][0]['hooks'][0]['command'], command)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='msms guard ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'results'
        self.output.mkdir()
        self.path = guard.start(self.root, 'session-1', 'analysis-1', ['001', '002'], self.output)
        self.data = guard.read_json(self.path)
        self.proof = self.root / 'confirmed.json'
        guard.write_json(self.proof, {'model': 'glacier', 'collision_unit': 'NCE', 'user_message': 'both'})
        for name in guard.PREREQUISITES:
            self.data['prerequisites'][name] = self.evidence()
        for feature_id in self.data['features']:
            self.make_result(feature_id)
        self.report = self.output / 'review_report.html'
        self.report.write_text('<!doctype html><html><body>Review</body></html>')
        (self.output / 'final_assessment.md').write_text('Two hypotheses with remaining positional ambiguity.')
        self.save()

    def evidence(self, **kwargs):
        return {'status': 'done', 'reason': 'Verified against the supplied input and user confirmation.',
                'evidence': [str(self.proof)], **kwargs}

    def save(self):
        guard.write_json(self.path, self.data)

    def make_result(self, feature_id, *, weak=False):
        folder = self.output / feature_id
        folder.mkdir(exist_ok=True)
        result = folder / 'retrieval.json'
        review = folder / 'evidence_review.json'
        guard.write_json(result, {'status': 'ranked', 'prediction_complete': True,
                                  'spectra': {'20': [[31.0184, 1.0]]},
                                  'candidates': [{'smiles': 'CCO'}]})
        guard.write_json(review, {
            'feature_id': feature_id, 'source_result': str(result), 'source_sha256': guard.sha256(result),
            'reviewed_at': datetime.now(timezone.utc).isoformat(), 'coverage': 'complete',
            'energies_reviewed': ['20'],
            'assessment': 'The main fragments support the scaffold; a positional isomer remains possible.',
            'candidates_considered': ['CCO'], 'peak_evidence': [{'mz': 31.0184, 'energy': 20, 'matched': True}],
            'supporting_evidence': ['Shared oxygen-containing fragment.'], 'conflicting_evidence': [],
            'ambiguities': ['Positional isomer not resolved.'], 'weak_evidence': weak,
            'important_unexplained_peaks': False,
        })
        self.data['features'][feature_id] = {'outcome': 'ranked', 'result': str(result), 'review': str(review)}

    def stop(self, **extra):
        return guard.hook({'session_id': 'session-1', 'hook_event_name': 'Stop', **extra}, self.root)

    def test_complete_analysis_requires_finalization_and_receipt(self):
        self.assertTrue(guard.inspect_state(self.path)['complete'])
        self.assertEqual(self.stop()['decision'], 'block')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['finish', '--state', str(self.path)]), 0)
        self.assertEqual(self.stop(), {})
        receipt = guard.read_json(self.path.with_name('completion_receipt.json'))
        self.assertEqual(receipt['outcomes'], {'ranked': 2})

    def test_unrelated_session_is_not_blocked(self):
        self.report.unlink()
        self.assertEqual(guard.hook({'session_id': 'other', 'hook_event_name': 'Stop'}, self.root), {})
        self.assertEqual(guard.hook({'session_id': 'new', 'hook_event_name': 'Stop'}, self.root), {})

    def test_review_missing_provisional_empty_or_stale_blocks_even_high_scoring_results(self):
        review_path = Path(self.data['features']['001']['review'])
        original = guard.read_json(review_path)
        for changes in ({'coverage': 'provisional'}, {'source_sha256': 'old'},
                        {'assessment': ''}, {'peak_evidence': []}, {'feature_id': 'wrong'}):
            with self.subTest(changes=changes):
                guard.write_json(review_path, {**original, **changes})
                result = self.stop()
                self.assertEqual(result['decision'], 'block')
                self.assertIn('001', result['reason'])
        review_path.unlink()
        self.assertEqual(self.stop()['decision'], 'block')

    def test_modifying_result_invalidates_review_after_completion(self):
        with contextlib.redirect_stdout(io.StringIO()):
            guard.main(['finish', '--state', str(self.path)])
        result_path = Path(self.data['features']['001']['result'])
        result = guard.read_json(result_path)
        result['candidates'].append({'smiles': 'COC'})
        guard.write_json(result_path, result)
        self.assertIn('stale', self.stop()['reason'])

    def test_running_job_blocks_and_no_job_is_silently_ignored(self):
        receipt = self.root / 'status.json'
        self.data['jobs'] = [{'job_id': 'gpu-task', 'status_path': str(receipt)}]
        self.save()
        for state in ('queued', 'running', 'waiting_resources'):
            guard.write_json(receipt, {'state': state})
            self.assertIn('keep polling', self.stop()['reason'])
        guard.write_json(receipt, {'state': 'done', 'finished': guard.now()})
        self.assertIn('exit_code', self.stop()['reason'])
        guard.write_json(receipt, {'state': 'done', 'finished': guard.now(), 'exit_code': 0})
        self.assertTrue(guard.inspect_state(self.path)['complete'])

    def test_stop_returns_proven_running_job_without_scanning_remaining_work(self):
        receipt = self.root / 'running.json'
        guard.write_json(receipt, {'state': 'running'})
        self.data['jobs'] = [{'job_id': 'active', 'status_path': str(receipt)},
                             {'job_id': 'later', 'status_path': 'later.json'}]
        self.save()
        native = guard.readable

        def bounded(root, value, **kwargs):
            if value == 'later.json':
                self.fail('Stop scanned later work after proving an active job')
            return native(root, value, **kwargs)

        with patch.object(guard, 'readable', side_effect=bounded), \
                patch.object(guard, 'review_result', side_effect=AssertionError('unneeded review scan')):
            result = self.stop()
        self.assertEqual(result['decision'], 'block')
        self.assertIn('active: running', result['reason'])
        self.assertNotIn('Unreadable workflow state', result['reason'])
        # Explicit checks and finish still inspect every remaining requirement.
        checked = guard.inspect_state(self.path, force=True, stop_on_issue=True)
        self.assertTrue(checked['inspection_complete'])
        self.assertEqual({issue.get('job_id') for issue in checked['issues']}, {'active', 'later'})

    def test_early_stop_leaves_review_cache_intact_and_full_check_lists_all_issues(self):
        guard.inspect_state(self.path)
        cache_path = self.path.with_name('validation_cache.json')
        cache_before = cache_path.read_bytes()
        for feature in self.data['features'].values():
            Path(feature['review']).unlink()
        result = guard.inspect_state(self.path, stop_on_issue=True)
        self.assertFalse(result['inspection_complete'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['allow_stop'])
        self.assertEqual(cache_before, cache_path.read_bytes())
        full = guard.inspect_state(self.path)
        self.assertTrue(full['inspection_complete'])
        self.assertEqual({issue.get('feature_id') for issue in full['issues']}, {'001', '002'})

    def test_thousands_of_exclusions_share_evidence_only_within_one_check(self):
        features = [str(index) for index in range(5000)]
        path = guard.start(self.root, 'large-session', 'large-run', features, self.output)
        data = guard.read_json(path)
        data['features'] = {key: self.evidence(outcome='excluded') for key in features}
        data['prerequisites'] = self.data['prerequisites']
        data['report'] = self.data['report']
        data['final_assessment'] = self.data['final_assessment']
        guard.write_json(path, data)
        for _ in range(2):
            with patch.object(guard, 'readable', wraps=guard.readable) as readable:
                result = guard.inspect_state(path, force=True)
            self.assertTrue(result['complete'])
            self.assertEqual(result['outcomes'], {'excluded': 5000})
            proof_reads = [call for call in readable.call_args_list if call.args[1] == str(self.proof)]
            self.assertEqual(len(proof_reads), 1)
        # A shared file never excuses a malformed record or disappears behind a persistent cache.
        data['features'][features[-1]]['reason'] = ''
        guard.write_json(path, data)
        result = guard.inspect_state(path, force=True)
        self.assertEqual([issue['feature_id'] for issue in result['issues']], [features[-1]])
        self.proof.unlink()
        result = guard.inspect_state(path, force=True)
        self.assertFalse(result['complete'])
        self.assertEqual(len([issue for issue in result['issues'] if issue['kind'] == 'feature']), 5000)

    def test_weak_evidence_requires_user_route_and_both_routes_need_scoring(self):
        self.make_result('001', weak=True)
        self.save()
        self.assertIn('user-selected', self.stop()['reason'])
        feature = self.data['features']['001']
        feature['denovo'] = self.evidence(choice='both', source='agent')
        self.save()
        self.assertIn('user-selected', self.stop()['reason'])
        feature['denovo']['source'] = 'user'
        self.save()
        self.assertIn('selected agent', self.stop()['reason'])
        proposals = self.root / 'proposals.json'
        guard.write_json(proposals, ['CCO'])
        route = {'status': 'done', 'proposals': str(proposals), 'scored_result': feature['result']}
        feature['denovo']['routes'] = {'agent': route}
        self.save()
        self.assertIn('selected frigid', self.stop()['reason'])
        feature['denovo']['routes']['frigid'] = route
        self.save()
        self.assertTrue(guard.inspect_state(self.path)['complete'])

    def test_skip_requires_confirmation_but_no_generated_candidates(self):
        self.make_result('001', weak=True)
        self.data['features']['001']['denovo'] = self.evidence(choice='skip', source='user')
        self.save()
        self.assertTrue(guard.inspect_state(self.path)['complete'])

    def test_missing_prerequisite_evidence_or_report_blocks(self):
        self.data['prerequisites']['collision_energy']['evidence'] = []
        self.save()
        self.assertIn('collision_energy', self.stop()['reason'])
        self.data['prerequisites']['collision_energy'] = self.evidence()
        self.save()
        self.report.unlink()
        self.assertIn('report', self.stop()['reason'])

    def test_request_coverage_cannot_shrink_to_completed_subset(self):
        del self.data['features']['002']
        self.save()
        self.assertIn('Feature accounting', self.stop()['reason'])

    def test_no_candidate_outcome_needs_assessment_but_not_peak_table(self):
        feature = self.data['features']['001']
        result_path, review_path = Path(feature['result']), Path(feature['review'])
        guard.write_json(result_path, {'status': 'no_candidates', 'spectra': {'20': [[31.0184, 1.0]]}})
        review = guard.read_json(review_path)
        review.update(source_sha256=guard.sha256(result_path), peak_evidence=[], candidates_considered=[], weak_evidence=True)
        guard.write_json(review_path, review)
        feature['outcome'] = 'no_candidates'
        feature['denovo'] = self.evidence(choice='skip', source='user')
        self.save()
        self.assertTrue(guard.inspect_state(self.path)['complete'])

    def test_failed_and_excluded_features_are_accounted_separately(self):
        self.data['features']['001'] = self.evidence(outcome='excluded')
        self.data['features']['002'] = self.evidence(outcome='failed')
        self.save()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['finish', '--state', str(self.path)]), 0)
        receipt = guard.read_json(self.path.with_name('completion_receipt.json'))
        self.assertEqual(receipt['status'], 'completed_with_failures')
        self.assertEqual(receipt['outcomes'], {'excluded': 1, 'failed': 1})

    def test_explicit_pause_allows_running_work_but_requires_user_source(self):
        self.data['status'] = 'paused'
        self.data['hold'] = self.evidence(source='user')
        self.report.unlink()
        self.save()
        self.assertEqual(self.stop(), {})
        self.data['hold']['source'] = 'agent'
        self.save()
        self.assertIn('explicit user', self.stop()['reason'])

    def test_waiting_for_user_only_allows_stop_when_unaffected_work_is_done(self):
        self.make_result('001', weak=True)
        self.data['status'] = 'awaiting_user'
        self.data['hold'] = self.evidence(feature_ids=['001'])
        self.report.unlink()
        self.save()
        self.assertEqual(self.stop(), {})
        self.data['features']['002']['outcome'] = 'pending'
        self.save()
        self.assertEqual(self.stop()['decision'], 'block')

    def test_waiting_for_input_does_not_excuse_running_jobs_or_unreviewed_results(self):
        self.make_result('001', weak=True)
        self.data['status'] = 'awaiting_user'
        self.data['hold'] = self.evidence(feature_ids=['001'])
        receipt = self.root / 'status.json'
        guard.write_json(receipt, {'state': 'running'})
        self.data['jobs'] = [{'job_id': 'gpu-task', 'status_path': str(receipt)}]
        self.save()
        self.assertEqual(self.stop()['decision'], 'block')
        self.data['jobs'] = []
        Path(self.data['features']['001']['review']).unlink()
        self.save()
        self.assertEqual(self.stop()['decision'], 'block')

    def test_corrupt_state_blocks_own_session_and_repeat_hooks_still_check(self):
        self.path.write_text('{partial')
        self.assertIn('Unreadable workflow state', self.stop(stop_hook_active=True)['reason'])
        self.assertEqual(guard.hook({'session_id': 'other', 'hook_event_name': 'Stop'}, self.root), {})

    def test_configuration_merge_is_idempotent_and_preserves_other_settings(self):
        settings = self.root / '.claude/settings.json'
        guard.write_json(settings, {'permissions': {'allow': ['Read']},
                                   'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'other-check'}]}]}})
        guard.install(self.root, 'msms-workflow-guard hook')
        first = settings.read_bytes()
        guard.install(self.root, 'msms-workflow-guard hook')
        self.assertEqual(first, settings.read_bytes())
        data = guard.read_json(settings)
        self.assertEqual(data['permissions']['allow'], ['Read'])
        self.assertEqual(len(data['hooks']['Stop']), 2)
        self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'], 'other-check')

    def test_real_hook_process_handles_subdirectory_spaces_and_bad_input(self):
        child = self.root / 'work with spaces'
        child.mkdir()
        (child / 'AGENTS.md').write_text('Nested instructions do not hide the owning analysis.')
        payload = {'session_id': 'session-1', 'hook_event_name': 'Stop', 'cwd': str(child)}
        process = subprocess.run([sys.executable, str(Path(guard.__file__)), 'hook'],
                                 input=json.dumps(payload), capture_output=True, text=True, cwd=child)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['decision'], 'block')
        process = subprocess.run([sys.executable, str(Path(guard.__file__)), 'hook'],
                                 input='not json', capture_output=True, text=True, cwd=child)
        self.assertEqual(process.returncode, 0)
        self.assertIn('checker error', json.loads(process.stdout)['reason'])

    def test_start_context_supplies_session_id_without_registering_an_analysis(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            result = guard.hook({'session_id': 'new-session', 'hook_event_name': event}, self.root)
            self.assertIn('new-session', result['hookSpecificOutput']['additionalContext'])
        self.assertFalse((self.root / '.msms-workflow' / guard.session_key('new-session')).exists())

    def test_raw_input_scope_cannot_complete_until_feature_list_is_frozen(self):
        path = guard.start(self.root, 'session-raw', 'raw-input', [], self.output, inputs=['run-01.mzML'])
        data = guard.read_json(path)
        for name in guard.PREREQUISITES:
            data['prerequisites'][name] = self.evidence()
        guard.write_json(path, data)
        self.assertFalse(guard.inspect_state(path)['complete'])
        manifest = self.root / 'manifest.csv'
        manifest.write_text('feature_id,status\n001,ready\n002,ready\n')
        guard.set_features(path, ['001', '002'], str(manifest))
        request = guard.read_json(path.with_name('request.json'))
        self.assertEqual(request['input_ids'], ['run-01.mzML'])
        self.assertEqual(request['feature_ids'], ['001', '002'])
        self.assertEqual(request['feature_manifest_sha256'], guard.sha256(manifest))
        with self.assertRaises(ValueError):
            guard.set_features(path, ['001'], str(manifest))

    def test_cached_checks_skip_unchanged_spectra_but_finish_revalidates_hashes(self):
        from unittest.mock import patch
        self.assertTrue(guard.inspect_state(self.path)['complete'])
        with patch.object(guard, 'review_result', side_effect=AssertionError('should use cache')):
            self.assertTrue(guard.inspect_state(self.path)['complete'])
        with patch.object(guard, 'review_result', wraps=guard.review_result) as review:
            self.assertTrue(guard.inspect_state(self.path, force=True)['complete'])
            self.assertEqual(review.call_count, 2)

    def test_main_cli_dispatches_workflow_subcommands(self):
        from msms_structure_elucidation import cli
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['workflow', 'check', '--state', str(self.path)]), 0)

    def test_review_check_publishes_only_valid_current_feature_reviews(self):
        feature = self.data['features']['001']
        draft = self.root / 'review-tasks' / 'draft.json'
        guard.write_json(draft, guard.read_json(Path(feature['review'])))
        target = self.output / 'accepted-review.json'
        args = ['review-check', '--project-dir', str(self.root), '--feature-id', '001',
                '--result', feature['result'], '--review', str(draft), '--publish', str(target)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(args), 0)
        accepted = target.read_bytes()
        self.assertEqual(guard.read_json(target), guard.read_json(draft))
        guard.write_json(Path(feature['result']), {'status': 'ranked', 'candidates': [{'smiles': 'COC'}]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(args), 2)
        self.assertEqual(target.read_bytes(), accepted)

    def test_review_check_rejects_wrong_feature_provisional_and_source_overwrite(self):
        feature = self.data['features']['001']
        args = ['review-check', '--project-dir', str(self.root), '--feature-id', '001',
                '--result', feature['result'], '--review', feature['review']]
        source = Path(feature['result']).read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main([*args, '--publish', feature['result']]), 2)
        self.assertEqual(Path(feature['result']).read_bytes(), source)
        review = guard.read_json(Path(feature['review']))
        review['coverage'] = 'provisional'
        guard.write_json(Path(feature['review']), review)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(args), 2)

    def test_incomplete_candidate_coverage_does_not_pass_as_ranked(self):
        feature = self.data['features']['001']
        result_path = Path(feature['result'])
        result = guard.read_json(result_path)
        result['formula_results'] = [{'formula': 'C2H6O', 'status': 'not_processed'}]
        guard.write_json(result_path, result)
        self.assertIn('unprocessed', self.stop()['reason'])

    def test_review_must_cover_all_observed_energies_and_stepped_key(self):
        feature = self.data['features']['001']
        result_path = Path(feature['result'])
        result = guard.read_json(result_path)
        result['spectra']['40'] = [[31.0184, 1.0]]
        guard.write_json(result_path, result)
        review_path = Path(feature['review'])
        review = guard.read_json(review_path)
        review['source_sha256'] = guard.sha256(result_path)
        guard.write_json(review_path, review)
        self.assertIn('every observed spectrum', self.stop()['reason'])
        result['spectra'] = {'stepped': [[31.0184, 1.0]]}
        guard.write_json(result_path, result)
        review.update(source_sha256=guard.sha256(result_path), energies_reviewed=['stepped'])
        guard.write_json(review_path, review)
        self.assertTrue(guard.inspect_state(self.path)['complete'])

    def test_new_user_message_reopens_waiting_hold_but_retains_explicit_pause(self):
        self.make_result('001', weak=True)
        self.data['status'] = 'awaiting_user'
        self.data['hold'] = self.evidence(feature_ids=['001'])
        self.save()
        self.assertEqual(self.stop(), {})
        context = guard.hook({'session_id': 'session-1', 'hook_event_name': 'UserPromptSubmit'}, self.root)
        self.assertIn('Reassess', context['hookSpecificOutput']['additionalContext'])
        self.assertEqual(guard.read_json(self.path)['status'], 'active')
        self.assertEqual(self.stop()['decision'], 'block')
        self.data['status'] = 'paused'
        self.data['hold'] = self.evidence(source='user')
        self.save()
        guard.hook({'session_id': 'session-1', 'hook_event_name': 'UserPromptSubmit'}, self.root)
        self.assertEqual(guard.read_json(self.path)['status'], 'paused')

    def test_mcp_direct_scoring_retains_observed_spectra_and_final_status(self):
        from unittest.mock import patch
        from msms_structure_elucidation.mcp import tasks
        spectrum = self.root / 'input.ms'
        spectrum.write_text('>compound sample\n>parentmass 47.0491\n>ionization [M+H]+\n'
                            '>instrumentation Orbitrap\n>collision 20 NCE\n31.0184 1.0\n')
        params = {'smiles': ['CCO'], 'collision_unit': 'NCE', 'model': 'glacier', 'top_k': 2}
        with patch.object(tasks, '_spectrum', return_value=spectrum), \
                patch.object(tasks, '_ms_pred_python', return_value=sys.executable), \
                patch.object(tasks, '_simulator_options', return_value={}), \
                patch.object(tasks.cli, 'worker', return_value=[{'smiles': 'CCO'}]):
            result = tasks.score_candidates(params, self.root, {}, None, io.StringIO())
        self.assertEqual(result['status'], 'ranked')
        self.assertEqual(list(result['spectra'].values()), [[[31.0184, 1.0]]])
        self.assertEqual(result['collision_unit'], 'NCE')
        self.assertTrue(result['energy_mapping'])

    def test_external_access_hold_accounts_for_unobservable_job_without_completing_it(self):
        self.data['features']['001'] = {'outcome': 'pending'}
        receipt = self.root / 'status.json'
        guard.write_json(receipt, {'state': 'unavailable', 'error': 'SSH connection failed', 'observed_at': guard.now()})
        self.data['jobs'] = [{'job_id': 'remote-task', 'status_path': str(receipt)}]
        self.data['status'] = 'blocked'
        self.data['hold'] = self.evidence(feature_ids=['001'], job_ids=['remote-task'])
        self.save()
        result = guard.inspect_state(self.path)
        self.assertTrue(result['allow_stop'])
        self.assertFalse(result['complete'])
        guard.write_json(receipt, {'state': 'waiting_resources'})
        self.assertFalse(guard.inspect_state(self.path)['allow_stop'])

    def test_installed_non_git_project_is_found_from_a_subdirectory(self):
        project = self.root / 'independent project'
        guard.install(project, 'msms-workflow-guard hook')
        child = project / 'inputs'
        child.mkdir()
        self.assertEqual(guard.project_root(child), project)
        self.assertIn('.msms-workflow/', (project / '.gitignore').read_text())
        context = guard.hook({'session_id': 'installed', 'hook_event_name': 'SessionStart'}, project)
        guide = guard.instruction_location(project, 'docs/workflow-hooks.md')
        self.assertTrue(Path(guide).is_file())
        self.assertIn(guide, context['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
