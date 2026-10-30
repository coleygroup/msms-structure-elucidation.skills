import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class NativePreprocessTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[1] / '.agents/skills/msms-preprocess/scripts/run.py'
        spec = importlib.util.spec_from_file_location('preprocess_script', script)
        self.preprocess = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.preprocess)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / 'sample file.raw'
        self.input.write_text('raw input fixture')
        self.output = self.root / 'converted output' / 'sample file.mzML'
        self.environment = patch.dict(os.environ, {'MSCONVERT_BIN': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_explicit_path_takes_precedence_over_environment_and_path(self):
        with patch.dict(os.environ, {'MSCONVERT_BIN': '/user/env/msconvert'}), \
                patch.object(self.preprocess.shutil, 'which', side_effect=lambda value: value) as which:
            self.assertEqual(self.preprocess._find_msconvert('/user/cli/msconvert'), '/user/cli/msconvert')
            self.assertEqual(self.preprocess._find_msconvert(), '/user/env/msconvert')
        self.assertEqual([call.args[0] for call in which.call_args_list],
                         ['/user/cli/msconvert', '/user/env/msconvert'])

    def test_path_discovery_invokes_the_resolved_native_executable(self):
        with patch.object(self.preprocess.shutil, 'which', return_value='/user/bin/msconvert') as which, \
                patch.object(self.preprocess.subprocess, 'run') as run:
            self.preprocess.convert_raw(self.input, self.output, 'mgf', 'negative')
        which.assert_called_once_with('msconvert')
        command = run.call_args.args[0]
        self.assertEqual(command[0], '/user/bin/msconvert')
        self.assertIn(str(self.input), command)
        self.assertIn('--mgf', command)
        self.assertIn('peakPicking true 1-', command)
        self.assertIn('polarity negative', command)
        self.assertTrue(run.call_args.kwargs['check'])

    def test_missing_native_converter_does_not_try_container_commands(self):
        def available(command):
            return '/unused/docker' if command == 'docker' else None

        with patch.object(self.preprocess.shutil, 'which', side_effect=available) as which, \
                patch.object(self.preprocess.subprocess, 'run') as run:
            with self.assertRaisesRegex(FileNotFoundError, 'instrument workstation'):
                self.preprocess.convert_raw(self.input, self.output, 'mzml', 'any')
        which.assert_called_once_with('msconvert')
        run.assert_not_called()
        self.assertFalse(self.output.parent.exists())

    def test_invalid_explicit_path_does_not_fall_back_to_another_tool(self):
        with patch.object(self.preprocess.shutil, 'which', return_value=None) as which:
            with self.assertRaisesRegex(FileNotFoundError, '/missing/msconvert'):
                self.preprocess._find_msconvert('/missing/msconvert')
        which.assert_called_once_with('/missing/msconvert')

    def test_open_format_passthrough_needs_no_converter(self):
        for suffix in ('.mzML', '.mgf'):
            source = self.input.with_suffix(suffix)
            source.write_text('open input fixture')
            output = io.StringIO()
            with patch.object(self.preprocess, '_find_msconvert') as find, contextlib.redirect_stdout(output):
                self.preprocess.main(['--input', str(source), '--output', str(self.output)])
            find.assert_not_called()
            self.assertIn(str(source), output.getvalue())
            self.assertFalse(self.output.exists())

    def test_ms_export_stays_in_python(self):
        source = self.input.with_suffix('.mzML')
        source.write_text('open input fixture')
        with patch.object(self.preprocess, 'convert_to_ms') as convert, \
                patch.object(self.preprocess, '_find_msconvert') as find, contextlib.redirect_stdout(io.StringIO()):
            self.preprocess.main(['--input', str(source), '--output', str(self.root / 'spectra'), '--format', 'MS'])
        convert.assert_called_once_with(source, self.root / 'spectra')
        find.assert_not_called()

    def test_native_converter_failures_are_propagated(self):
        with patch.object(self.preprocess.shutil, 'which', return_value='/user/bin/msconvert'), \
                patch.object(self.preprocess.subprocess, 'run',
                             side_effect=subprocess.CalledProcessError(7, ['msconvert'])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.preprocess.convert_raw(self.input, self.output, 'mzml', 'any')
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == 'posix', 'Executable fixture uses a POSIX shebang')
    def test_custom_executable_handles_spaces_and_preserves_the_raw_input(self):
        executable = self.root / 'converter with spaces'
        receipt = self.root / 'converter-arguments.json'
        executable.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'Path(os.environ["MSMS_TEST_CONVERTER_RECEIPT"]).write_text(json.dumps(args))\n'
            'output = Path(args[args.index("-o") + 1]) / args[args.index("--outfile") + 1]\n'
            'output.write_text("converted fixture")\n')
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        with patch.dict(os.environ, {'MSMS_TEST_CONVERTER_RECEIPT': str(receipt)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.preprocess.main(['--input', str(self.input), '--output', str(self.output),
                                  '--msconvert-bin', str(executable), '--polarity', 'positive'])
        self.assertEqual(self.input.read_text(), 'raw input fixture')
        self.assertEqual(self.output.read_text(), 'converted fixture')
        command = json.loads(receipt.read_text())
        self.assertEqual(command[0], str(self.input))
        self.assertIn('polarity positive', command)


if __name__ == '__main__':
    unittest.main()
