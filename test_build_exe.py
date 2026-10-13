"""Offline packaging structure checks; never builds an EXE or changes the GUI."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_exe


class BuildExeTests(unittest.TestCase):
    def test_onefile_windowed_name_and_embedded_entry(self):
        gui, exe = build_exe.build_commands(executable='python-test')
        self.assertEqual(gui, ['python-test', str(build_exe.ROOT / 'build_gui.py')])
        self.assertEqual(exe[:3], ['python-test', '-m', 'PyInstaller'])
        self.assertIn('--onefile', exe)
        self.assertIn('--windowed', exe)
        self.assertEqual(exe[exe.index('--name') + 1], 'CardDownloader')
        self.assertEqual(exe[-1], str(build_exe.ROOT / 'scryfall_cards_gui.pyw'))

    def test_native_webp_image_plugins_and_tk_are_explicit(self):
        _, exe = build_exe.build_commands()
        imports = {exe[index + 1] for index, value in enumerate(exe) if value == '--hidden-import'}
        expected = {'PIL.Image', 'PIL.features', 'PIL.WebPImagePlugin',
                    'PIL.PngImagePlugin', 'PIL.JpegImagePlugin', 'PIL._webp',
                    'PIL._imaging', 'tkinter', 'tkinter.ttk', '_tkinter'}
        self.assertTrue(expected.issubset(imports))
        self.assertEqual(exe[exe.index('--collect-binaries') + 1], 'PIL')

    def test_output_is_worktree_local_even_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='card build ') as folder:
            root = Path(folder).resolve()
            _, exe = build_exe.build_commands(root)
            for flag, relative in (('--distpath', 'dist'),
                                   ('--workpath', 'build/pyinstaller'),
                                   ('--specpath', 'build/spec')):
                self.assertEqual(Path(exe[exe.index(flag) + 1]), root / relative)

    def test_no_external_source_data_or_embedded_modules(self):
        _, exe = build_exe.build_commands()
        self.assertNotIn('--add-data', exe)
        self.assertNotIn('--collect-all', exe)
        excluded = {exe[index + 1] for index, value in enumerate(exe) if value == '--exclude-module'}
        self.assertEqual(excluded, set(build_exe.EMBEDDED_MODULES))

    def test_dry_run_does_not_check_or_execute(self):
        with patch.object(build_exe, 'check_dependencies') as check, \
             patch.object(build_exe.subprocess, 'call') as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_exe.main(['--dry-run']), 0)
        check.assert_not_called()
        call.assert_not_called()
        self.assertIn('--onefile', output.getvalue())

    def test_check_does_not_generate_or_build(self):
        with patch.object(build_exe, 'check_dependencies') as check, \
             patch.object(build_exe.subprocess, 'call') as call:
            self.assertEqual(build_exe.main(['--check']), 0)
        check.assert_called_once_with()
        call.assert_not_called()

    def test_build_order_inherited_stdio(self):
        with patch.object(build_exe, 'check_dependencies'), \
             patch.object(build_exe.sys, 'platform', 'win32'), \
             patch.object(build_exe.subprocess, 'call', return_value=0) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_exe.main([]), 0)
        self.assertEqual(call.call_args_list,
                         [unittest.mock.call(command, cwd=build_exe.ROOT)
                          for command in build_exe.build_commands()])

    def test_generator_failure_stops_packaging(self):
        with patch.object(build_exe, 'check_dependencies'), \
             patch.object(build_exe.sys, 'platform', 'win32'), \
             patch.object(build_exe.subprocess, 'call', return_value=17) as call:
            self.assertEqual(build_exe.main([]), 17)
        self.assertEqual(call.call_count, 1)

    def test_missing_dependency_stops_packaging(self):
        with patch.object(build_exe, 'check_dependencies', side_effect=ImportError('PIL')), \
             patch.object(build_exe.subprocess, 'call') as call, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_exe.main([]), 1)
        call.assert_not_called()


class EngineStreamTests(unittest.TestCase):
    @staticmethod
    def function_source():
        import ast
        text = (build_exe.ROOT / 'gui_layer.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'ensure_engine_standard_streams')
        return ast.get_source_segment(text, node)

    def test_preserve_valid_streams(self):
        import os
        import types
        streams = types.SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
        before = (streams.stdin, streams.stdout, streams.stderr)
        scope = {'sys': streams, 'os': os}
        exec(self.function_source(), scope)
        scope['ensure_engine_standard_streams']()
        self.assertEqual(before, (streams.stdin, streams.stdout, streams.stderr))

    def test_missing_streams_without_handles_use_devnull(self):
        import os
        import types
        streams = types.SimpleNamespace(stdin=None, stdout=None, stderr=None)
        scope = {'sys': streams, 'os': os}
        exec(self.function_source(), scope)
        with patch.object(os, 'name', 'posix'), patch.object(os, 'dup', side_effect=OSError('no handle')):
            scope['ensure_engine_standard_streams']()
        try:
            self.assertEqual(streams.stdin.read(), '')
            streams.stdout.write('silent')
            streams.stderr.write('silent')
        finally:
            for stream in (streams.stdin, streams.stdout, streams.stderr):
                stream.close()

    def test_restore_real_inherited_pipes(self):
        import subprocess
        import sys
        code = ('import sys, os\n' + self.function_source() +
                '\nsys.stdin=sys.stdout=sys.stderr=None\n'
                'ensure_engine_standard_streams()\n'
                'print("pipe:"+sys.stdin.readline().strip(),flush=True)\n'
                'print("error-pipe",file=sys.stderr,flush=True)\n')
        result = subprocess.run([sys.executable, '-c', code], input='hello\n',
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'pipe:hello')
        self.assertEqual(result.stderr.strip(), 'error-pipe')


if __name__ == '__main__':
    unittest.main()
