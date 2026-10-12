import contextlib
import gc
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('gui_app', str(Path(__file__).with_name('scryfall_cards_gui.pyw')))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)

class GUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = tk.Tk()
        self.root.withdraw()
        self.gui = app.CardDownloaderGUI(self.root, self.temp.name)
        self.root.update()

    def tearDown(self):
        self.gui.close()
        self.gui = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def pump(self, timeout=8):
        deadline = time.monotonic() + timeout
        while self.gui.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.gui.running, 'GUI worker did not finish')

    def test_defaults_options_and_program_directory(self):
        self.assertEqual(self.gui.workers.get(), '4')
        self.assertTrue(self.gui.pdf.get())
        self.assertTrue(self.gui.cut_marks.get())
        self.assertEqual(self.gui.output_dir, Path(self.temp.name) / '卡图输出')
        self.assertEqual(app.program_directory(), Path(app.__file__).resolve().parent)
        self.gui.example()
        self.assertIn('17 张', self.gui.count.get())
        text, workers, paper, langs = self.gui.configuration()
        self.assertEqual((workers, paper, langs), (4, 'A4', 'zhs,zht,en'))
        self.gui.dry_run.set(True)
        self.gui.sync_options()
        self.assertEqual(str(self.gui.paper_box['state']), 'disabled')
        self.gui.dry_run.set(False)
        self.gui.pdf.set(False)
        self.gui.sync_options()
        self.assertEqual(str(self.gui.mark_check['state']), 'disabled')
        self.gui.workers.set('0')
        with self.assertRaises(ValueError): self.gui.configuration()
        self.gui.workers.set('4')
        self.gui.language.set('英文 nonsense')
        with self.assertRaises(ValueError): self.gui.configuration()

    def test_all_flags_forwarded(self):
        command = app.engine_command('deck.txt', self.gui.output_dir, 32, 'A3', 'zht,zhs,en', False, False, True, True)
        for flag in ['--engine', '--no-pdf', '--no-cut-marks', '--dry-run', '--force']:
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--workers') + 1], '32')
        self.assertEqual(command[command.index('--paper') + 1], 'A3')
        self.assertEqual(command[command.index('--out') + 1], str(self.gui.output_dir))

    def test_real_subprocess_logs_progress_and_old_pdf(self):
        self.gui.set_deck('1 Lightning Bolt')
        self.gui.output_dir.mkdir()
        (self.gui.output_dir / 'cards.pdf').write_bytes(b'old PDF')
        worker = Path(self.temp.name) / 'worker.py'
        worker.write_text("import time\nprint('[查询 1/1] Lightning Bolt', flush=True)\nprint('开始下载：1 张图片，初始并发 32。', flush=True)\nprint('自动降并发：32 -> 16', flush=True)\ntime.sleep(0.1)\nprint('下载完成 [1/1] test.png', flush=True)\nprint('完成：成功 1 / 失败 0', flush=True)\n", encoding='utf-8')
        with patch.object(app, 'engine_command', return_value=[sys.executable, '-u', str(worker)]):
            self.gui.start()
            self.assertTrue(self.gui.running)
            self.assertEqual(str(self.gui.start_button['state']), 'disabled')
            self.pump()
        self.assertEqual(self.gui.status.get(), '任务完成。')
        self.assertIn('32 -> 16', self.gui.log_text.get('1.0', 'end'))
        self.assertEqual(self.gui.summary, (1, 0))
        self.assertEqual(str(self.gui.pdf_button['state']), 'disabled', 'Must not offer an old PDF')
        self.assertTrue((self.gui.output_dir / '运行日志.txt').exists())
        self.assertTrue((self.gui.output_dir / '本次牌表.txt').exists())
        self.assertEqual(str(self.gui.start_button['state']), 'normal')

    def test_stop_process_and_reenable(self):
        self.gui.set_deck('1 Forest')
        worker = Path(self.temp.name) / 'slow.py'
        worker.write_text("import time\nprint('started',flush=True)\ntime.sleep(30)\n", encoding='utf-8')
        with patch.object(app, 'engine_command', return_value=[sys.executable, '-u', str(worker)]):
            self.gui.start()
            deadline = time.monotonic() + 3
            while self.gui.process is None and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIsNotNone(self.gui.process)
            self.gui.cancel()
            self.pump()
        self.assertIn('已停止', self.gui.status.get())
        self.assertIsNone(self.gui.process)
        self.assertEqual(str(self.gui.start_button['state']), 'normal')

    def test_partial_result_and_new_pdf(self):
        self.gui.set_deck('1 Forest')
        worker = Path(self.temp.name) / 'partial.py'
        worker.write_text("import sys\nfrom pathlib import Path\n(Path(sys.argv[1]) / 'cards.pdf').write_bytes(b'new PDF')\nprint('完成：成功 1 / 失败 1', flush=True)\nsys.exit(1)\n", encoding='utf-8')
        with patch.object(app, 'engine_command', side_effect=lambda deck, output, *args: [sys.executable, '-u', str(worker), str(output)]):
            self.gui.start()
            self.pump()
        self.assertIn('部分卡牌失败', self.gui.status.get())
        self.assertEqual(str(self.gui.pdf_button['state']), 'normal')
        self.assertEqual(self.gui.summary, (1, 1))

    def test_tasks_isolated_even_in_same_second(self):
        worker = Path(self.temp.name) / 'isolated.py'
        worker.write_text("import sys\nfrom pathlib import Path\nout=Path(sys.argv[1])\n(out/'cards').mkdir()\n(out/'cards'/'test.png').write_bytes(b'image')\n(out/'cards.pdf').write_bytes(b'PDF')\nprint('完成：成功 1 / 失败 0',flush=True)\n", encoding='utf-8')
        with patch.object(app.time, 'strftime', return_value='261012-101030'), patch.object(app, 'engine_command', side_effect=lambda deck, output, *args: [sys.executable, '-u', str(worker), str(output)]):
            self.gui.set_deck('1 Forest')
            self.gui.start()
            self.pump()
            first = self.gui.output_dir
            first_log = (first / '运行日志.txt').read_bytes()
            self.assertEqual(first.name, '261012-101030')
            self.assertEqual(first.parent, self.gui.output_root)
            self.gui.set_deck('2 Lightning Bolt')
            self.gui.start()
            self.pump()
            second = self.gui.output_dir
            self.assertEqual(second.name, '261012-101030_01')
            self.assertNotEqual(first, second)
            self.assertEqual((first / '本次牌表.txt').read_text(encoding='utf-8'), '1 Forest\n')
            self.assertEqual((second / '本次牌表.txt').read_text(encoding='utf-8'), '2 Lightning Bolt\n')
            self.assertEqual((first / '运行日志.txt').read_bytes(), first_log)
            self.assertTrue((first / 'cards' / 'test.png').exists())
            self.assertTrue((second / 'cards' / 'test.png').exists())
            self.assertEqual(self.gui.output_label.get(), str(second))
            with patch.object(self.gui, '_open_path') as opened:
                self.gui.open_output()
                opened.assert_called_once_with(second)
            with patch.object(self.gui, '_open_path') as opened:
                self.gui.open_pdf()
                opened.assert_called_once_with(second / 'cards.pdf')

    def test_invalid_input_does_not_create_task(self):
        self.gui.set_deck('')
        with patch.object(app.messagebox, 'showerror') as error:
            self.gui.start()
            error.assert_called_once()
        self.assertFalse(self.gui.output_root.exists())

    def test_parallel_query_progress_and_cooldown(self):
        self.gui.interpret_log('开始查询：4 种牌，查询并发 32（与下载共用线程设置）。')
        self.assertEqual(float(self.gui.progress['maximum']), 4)
        self.assertEqual(str(self.gui.progress['mode']), 'determinate')
        self.gui.interpret_log('[查询 2/4] Lightning Bolt')
        self.assertEqual(float(self.gui.progress['value']), 2)
        self.gui.interpret_log('  API 受限 HTTP 429：所有查询暂停新请求 5 秒后重试。')
        self.assertIn('查询请求受限', self.gui.status.get())
        self.gui.interpret_log('  查询进度 2/4 | 未完成 2 | API 冷却 4 秒')
        self.assertIn('API 冷却 4 秒', self.gui.detail.get())

    def test_shared_pics_path_progress_and_open(self):
        task = self.gui.output_root / '261012-130000'
        command = app.engine_command('deck.txt', task, 4, 'A4', 'zhs,zht,en', True, True, False, False)
        self.assertEqual(command[command.index('--pics') + 1], str(self.gui.output_root / 'pics'))
        self.gui.interpret_log('[缓存 1/2] Lightning Bolt -> 闪电击（1 张已有，0 张待补下载）')
        self.assertIn('本地缓存 1/2', self.gui.status.get())
        self.assertEqual(float(self.gui.progress['value']), 1)
        with patch.object(self.gui, '_open_path') as opened:
            self.gui.open_pics()
        opened.assert_called_once_with(self.gui.output_root / 'pics')
        self.assertTrue((self.gui.output_root / 'pics').is_dir())

    def test_engine_help(self):
        completed = subprocess.run([sys.executable, str(Path(app.__file__)), '--engine', '--help'], capture_output=True, encoding='utf-8', errors='replace', env={**app.os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertIn('--workers', completed.stdout)
        self.assertIn('--no-cut-marks', completed.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
