import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from msms_structure_elucidation import cli, worker


class PredictionLauncherTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[1] / '.agents/skills/msms-predict/scripts/predict_msms.py'
        spec = importlib.util.spec_from_file_location('prediction_launcher', script)
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'predictions.json'
        self.config = self.root / 'config.yaml'
        checkout = self.root / 'ms-pred'
        (checkout / 'src/ms_pred').mkdir(parents=True)
        for name in ('glacier', 'gen', 'inten'):
            (self.root / f'{name}.ckpt').touch()
        self.config.write_text(
            f'models:\n  simulator:\n    ms_pred_src: {checkout}\n    python: {sys.executable}\n'
            '    glacier_ckpt: glacier.ckpt\n    gen_ckpt: gen.ckpt\n    inten_ckpt: inten.ckpt\n'
            '    instrument: Orbitrap\n    batch_size: 8\n    num_cpu_workers: 4\n    num_gpu_workers: 2\n')

    def arguments(self, model: str = 'glacier', unit: str = 'eV') -> list[str]:
        return ['--smiles', 'CCO', '--model', model, '--collision-energies', '20', '40',
                '--collision-unit', unit, '--config', str(self.config), '--output', str(self.output)]

    def test_both_models_use_the_shared_backend_and_preserve_the_confirmed_unit(self):
        for model, unit in (('glacier', 'NCE'), ('iceberg', 'eV')):
            with self.subTest(model=model, unit=unit):
                prediction = {'smiles': 'CCO', 'model_name': model.upper(), 'predicted_spectra': {'20': [[31, 1]]}}

                def predict(python, action, **kwargs):
                    self.assertEqual(action, 'predict')
                    self.assertEqual(json.loads(Path(kwargs['smiles_json']).read_text()), ['CCO'])
                    self.assertEqual(kwargs['collision_unit'], unit)
                    self.assertEqual(kwargs['collision_energies'], '20.0,40.0')
                    self.assertEqual(kwargs['model'], model)
                    self.assertEqual(kwargs['instrument'], 'Orbitrap')
                    self.assertEqual(kwargs['num_gpu_workers'], 1 if model == 'glacier' else 2)
                    self.assertTrue(kwargs['force_recompute'])
                    self.assertTrue(kwargs['fragments'])
                    required = ('checkpoint',) if model == 'glacier' else ('gen_checkpoint', 'inten_checkpoint')
                    self.assertTrue(all(Path(kwargs[key]).is_file() for key in required))
                    return [prediction]

                with patch.object(cli, 'worker', side_effect=predict), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.launcher.main(self.arguments(model, unit)), 0)
                result = json.loads(self.output.read_text())
                self.assertEqual(result['model'], model.upper())
                self.assertEqual(result['collision_unit'], unit)
                self.assertEqual(result['predictions'], [prediction])

    def test_glacier_does_not_require_iceberg_weights_and_cpu_zero_is_preserved(self):
        (self.root / 'gen.ckpt').unlink()
        (self.root / 'inten.ckpt').unlink()
        with patch.object(cli, 'worker', return_value=[{'smiles': 'CCO'}]) as predict, \
                contextlib.redirect_stdout(io.StringIO()):
            self.launcher.main(self.arguments() + ['--model-cpu-workers', '0', '--no-fragments'])
        self.assertEqual(predict.call_args.kwargs['num_cpu_workers'], 0)
        self.assertFalse(predict.call_args.kwargs['fragments'])

    def test_input_and_weight_errors_happen_before_prediction(self):
        arguments = self.arguments()
        index = arguments.index('--collision-unit')
        invalid_inputs = [arguments[:index] + arguments[index + 2:],
                          arguments + ['--collision-energies', 'nan'],
                          arguments + ['--collision-energies', '-5']]
        with patch.object(cli, 'worker') as predict, contextlib.redirect_stderr(io.StringIO()):
            for invalid in invalid_inputs:
                with self.assertRaises(SystemExit):
                    self.launcher.main(invalid)
            with self.assertRaises(ValueError):
                self.launcher.main(arguments + ['--model-batch-size', '0'])
            (self.root / 'glacier.ckpt').unlink()
            with self.assertRaisesRegex(FileNotFoundError, 'GLACIER needs'):
                self.launcher.main(arguments)
            predict.assert_not_called()

    def test_failures_do_not_replace_a_previous_output(self):
        self.output.write_text('preserved output')
        with patch.object(cli, 'worker', side_effect=RuntimeError('prediction failed')):
            with self.assertRaisesRegex(RuntimeError, 'prediction failed'):
                self.launcher.main(self.arguments())
        self.assertEqual(self.output.read_text(), 'preserved output')
        with patch.object(cli, 'worker', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, '0 predictions'):
                self.launcher.main(self.arguments())
        self.assertEqual(self.output.read_text(), 'preserved output')


class PredictionDispatchTests(unittest.TestCase):
    def test_force_recompute_reaches_the_model_adapter(self):
        common = types.ModuleType('ms_pred.common')
        database = Mock()
        database.get_all_specs.return_value = []
        common.PredSpecDB = Mock(return_value=database)
        package = types.ModuleType('ms_pred')
        package.common = common
        with patch.dict(sys.modules, {'ms_pred': package, 'ms_pred.common': common}), \
                patch.object(worker, '_model_versions', return_value=('test', 'test')), \
                patch.object(worker, '_run_model', return_value=Path('/unused')) as run:
            worker.predict(['CCO'], [20, 40], 'eV', '[M+H]+', 'glacier', '/weights.ckpt', None, None,
                           force_recompute=True)
        self.assertIs(run.call_args.args[-1], True)
        database.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
