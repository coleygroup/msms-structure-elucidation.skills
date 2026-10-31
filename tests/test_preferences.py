"""Persistence and scope boundaries for user-confirmed pipeline answers."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msms_structure_elucidation import cli, config, hostsetup, preferences


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'configs' / 'local.yaml'

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['preferences', '--config', str(self.path), *args]), 0)
        return json.loads(output.getvalue())

    def test_new_user_has_recommendation_but_no_confirmed_answer(self):
        result = self.call('--get', 'feature_finder')
        self.assertEqual(result['feature_detect']['backend'], 'everything_bagel')
        self.assertIsNone(result['answer'])
        self.assertFalse(self.path.exists())

    def test_choice_round_trips_without_losing_settings_or_setup_updates(self):
        self.path.parent.mkdir()
        self.path.write_text('models:\n  simulator:\n    gen_ckpt: keep.ckpt\n    batch_size: 32\n')
        self.call('--set', 'feature_detect.backend', 'mzmine', '--answer', 'feature_finder', 'mzmine')
        saved = config.read_yaml(self.path)
        self.assertEqual(saved['models']['simulator']['gen_ckpt'], 'keep.ckpt')
        self.assertEqual(saved['models']['simulator']['batch_size'], 32)
        hostsetup.save_local({'models': {'simulator': {'batch_size': 64}}}, self.path)
        self.assertEqual(self.call('--get', 'feature_finder')['answer'], 'mzmine')
        self.assertEqual(config._simple_yaml(self.path.read_text()), config.read_yaml(self.path))
        with patch.object(config, 'LOCAL_CONFIG', self.path):
            merged = config.settings()
        self.assertEqual(merged['feature_detect']['backend'], 'mzmine')
        self.assertEqual(merged['models']['simulator']['batch_size'], 64)
        self.assertEqual(preferences.confirmed_answer(merged, 'feature_finder'), 'mzmine')

    def test_dataset_answer_requires_scope_and_input_context(self):
        for args in [('--answer', 'collision_unit', 'eV'),
                     ('--answer', 'collision_unit', 'eV', '--scope', 'dataset-01')]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.call(*args)
            self.assertFalse(self.path.exists())
        context = json.dumps({'manifest_sha256': 'abc'})
        result = self.call('--answer', 'collision_unit', 'eV', '--scope', 'dataset-01',
                           '--context-json', context, '--get', 'collision_unit')
        self.assertEqual(result['answer'], 'eV')
        self.assertIsNone(self.call('--get', 'collision_unit', '--scope', 'dataset-02',
                                    '--context-json', context)['answer'])
        self.assertIsNone(self.call('--get', 'collision_unit', '--scope', 'dataset-01',
                                    '--context-json', '{"manifest_sha256":"changed"}')['answer'])
        self.assertIsNone(self.call('--get', 'collision_unit')['answer'])

    def test_model_confirmation_does_not_apply_to_changed_weights_or_host(self):
        context = {'host': 'host-a', 'model': 'glacier', 'checkpoint_sha256': 'abc'}
        self.call('--answer', 'model_selection', 'glacier', '--context-json', json.dumps(context))
        self.assertEqual(self.call('--get', 'model_selection',
                                   '--context-json', json.dumps(context))['answer'], 'glacier')
        for changed in ({**context, 'host': 'host-b'}, {**context, 'checkpoint_sha256': 'xyz'}):
            self.assertIsNone(self.call('--get', 'model_selection',
                                        '--context-json', json.dumps(changed))['answer'])

    def test_new_user_answer_replaces_the_previous_answer(self):
        self.call('--set', 'feature_detect.backend', 'everything_bagel',
                  '--answer', 'feature_finder', 'everything_bagel')
        result = self.call('--set', 'feature_detect.backend', 'custom',
                           '--set', 'feature_detect.custom.instructions', 'Import supplied feature MGF',
                           '--answer', 'feature_finder', 'custom', '--get', 'feature_finder')
        self.assertEqual(result['answer'], 'custom')
        self.assertEqual(result['feature_detect']['backend'], 'custom')
        self.assertEqual(result['feature_detect']['custom']['instructions'], 'Import supplied feature MGF')

    def test_answer_reuse_can_be_disabled(self):
        result = self.call('--set', 'pipeline.reuse_answers', 'false',
                           '--answer', 'feature_finder', 'everything_bagel', '--get', 'feature_finder')
        self.assertIsNone(result['answer'])
        self.assertEqual(result['pipeline']['answers']['project']['feature_finder']['value'],
                         'everything_bagel')

    def test_packaged_defaults_match_checkout_defaults(self):
        packaged = Path(config.__file__).with_name('default.yaml')
        self.assertEqual(config.read_yaml(config.REPO_CONFIG), config.read_yaml(packaged))

    def test_verified_acquisition_metadata_is_not_mislabelled_as_a_user_answer(self):
        context = '{"input_sha256":"abc","scan_ids":["scan=42"]}'
        result = self.call('--answer', 'collision_unit', 'NCE', '--source', 'metadata',
                           '--scope', 'dataset-01', '--context-json', context, '--get', 'collision_unit')
        self.assertEqual(result['answer'], 'NCE')
        self.assertEqual(result['pipeline']['answers']['dataset-01']['collision_unit']['source'], 'metadata')
        self.assertIsNone(self.call('--get', 'collision_unit', '--scope', 'dataset-01',
                                    '--context-json', '{"input_sha256":"changed","scan_ids":["scan=42"]}')['answer'])

    def test_metadata_cannot_confirm_a_user_workflow_choice(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('--answer', 'feature_finder', 'everything_bagel', '--source', 'metadata',
                      '--context-json', '{"input_sha256":"abc"}')
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
