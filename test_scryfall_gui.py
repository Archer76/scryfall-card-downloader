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
        self.assertFalse(hasattr(self.gui, 'workers'))
        self.assertFalse(hasattr(self.gui, 'worker_box'))
        widgets = list(self.root.winfo_children())
        labels = []
        while widgets:
            widget = widgets.pop()
            widgets.extend(widget.winfo_children())
            self.assertNotIsInstance(widget, app.ttk.Spinbox)
            if isinstance(widget, app.ttk.Label):
                labels.append(str(widget.cget('text')))
        self.assertIn('查询并发 4 · 下载并发 8', labels)
        self.assertIn('自动处理网络请求，受限时等待并重试。', labels)
        self.assertEqual(app.DEFAULT_WORKERS, 4)
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
        self.gui.language.set('英文 nonsense')
        with self.assertRaises(ValueError): self.gui.configuration()

    def test_game_selection_and_concurrency(self):
        self.gui.set_deck('1 皮卡丘')
        self.assertEqual(tuple(self.gui.game_box['values']), ('万智牌', 'PTCG'))
        self.assertEqual(str(self.gui.game_box['state']), 'readonly')
        self.assertEqual(self.gui.concurrency(), (4, 8))
        siblings = self.gui.game_box.master.winfo_children()
        section = next(w for w in siblings if isinstance(w, app.ttk.Label)
                       and w.cget('text') == '下载与排版')
        self.assertLess(siblings.index(self.gui.game_box), siblings.index(section))
        self.gui.game.set('PTCG')
        self.assertEqual(self.gui.concurrency(), (64, 64))
        self.assertEqual(self.gui.configuration()[1], 64)
        self.assertIn('查询并发 64 · 下载并发 64', self.gui.network_policy.get())
        self.assertIn('本机实测择优', self.gui.network_policy.get())
        self.assertIn('未公布硬性限额', self.gui.network_policy.get())
        command = app.engine_command('deck.txt', self.gui.output_dir, 64, 'A4', 'zht,en',
                                     True, True, False, False,
                                     download_workers=self.gui.concurrency()[1])
        self.assertEqual(command[command.index('--workers') + 1], '64')
        self.assertEqual(command[command.index('--download-workers') + 1], '64')
        with patch.object(app.messagebox, 'showerror') as error, \
             patch.object(app, 'engine_command') as engine:
            self.gui.start()
        error.assert_called_once()
        self.assertIn('查询 64、下载 64', error.call_args.args[1])
        engine.assert_not_called()
        self.assertFalse(self.gui.running)
        self.assertFalse(self.gui.output_root.exists())
        self.gui.game.set('万智牌')
        self.assertEqual(self.gui.concurrency(), (4, 8))
        self.assertEqual(self.gui.configuration()[1], 4)
        self.gui.set_running(True)
        self.assertEqual(str(self.gui.game_box['state']), 'disabled')
        self.gui.set_running(False)
        self.assertEqual(str(self.gui.game_box['state']), 'readonly')

    def test_all_flags_forwarded(self):
        command = app.engine_command('deck.txt', self.gui.output_dir, 32, 'A3', 'zht,zhs,en', False, False, True, True)
        for flag in ['--engine', '--no-pdf', '--no-cut-marks', '--dry-run', '--force']:
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--workers') + 1], '32')
        self.assertEqual(command[command.index('--download-workers') + 1], '8')
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
        with patch.object(app, 'engine_command', side_effect=lambda deck, output, *args, **kwargs: [sys.executable, '-u', str(worker), str(output)]):
            self.gui.start()
            self.pump()
        self.assertIn('部分卡牌失败', self.gui.status.get())
        self.assertEqual(str(self.gui.pdf_button['state']), 'normal')
        self.assertEqual(self.gui.summary, (1, 1))

    def test_tasks_isolated_even_in_same_second(self):
        worker = Path(self.temp.name) / 'isolated.py'
        worker.write_text("import sys\nfrom pathlib import Path\nout=Path(sys.argv[1])\n(out/'cards').mkdir()\n(out/'cards'/'test.png').write_bytes(b'image')\n(out/'cards.pdf').write_bytes(b'PDF')\nprint('完成：成功 1 / 失败 0',flush=True)\n", encoding='utf-8')
        with patch.object(app.time, 'strftime', return_value='261012-101030'), patch.object(app, 'engine_command', side_effect=lambda deck, output, *args, **kwargs: [sys.executable, '-u', str(worker), str(output)]):
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

    def test_token_progress_and_separate_summary(self):
        self.gui.interpret_log('完成：成功 4 / 失败 0\n')
        self.gui.interpret_log('[Token] Forest -> Saproling：命中缓存\n')
        self.assertIn('[Token]', self.gui.status.get())
        self.assertEqual(self.gui.summary, (4, 0))
        self.gui.interpret_log('Token 完成：成功 2 / 失败 1 / 关联检查失败 3\n')
        self.assertEqual(self.gui.token_summary, (2, 1, 3))
        self.assertEqual(self.gui.summary, (4, 0), 'Token summary must not overwrite main cards')
        self.assertIn('Token 成功 2', self.gui.detail.get())
        self.assertIn('关联检查失败 3', self.gui.detail.get())

    def test_token_only_summary_is_not_main_summary(self):
        self.gui.interpret_log('Token 完成：成功 2 / 失败 0 / 关联检查失败 0\n')
        self.assertIsNone(self.gui.summary)
        self.assertEqual(self.gui.token_summary, (2, 0, 0))

    def test_token_failure_is_partial_even_with_zero_exit(self):
        for token_counts in [(2, 1, 0), (2, 0, 1), (2, 0, 0)]:
            with self.subTest(token_counts=token_counts):
                self.gui.summary = (4, 0)
                self.gui.token_summary = token_counts
                self.gui.output_dir.mkdir(exist_ok=True)
                self.gui.finish(0)
                self.assertEqual('部分卡牌失败' in self.gui.status.get(), any(token_counts[1:]))
                self.assertIn('主卡成功 4', self.gui.detail.get())
                self.assertIn('Token 成功 2', self.gui.detail.get())
                self.assertIn('关联检查失败 %d' % token_counts[2], self.gui.detail.get())

    def test_token_subprocess_and_next_task_reset(self):
        self.gui.set_deck('1 Forest')
        worker = Path(self.temp.name) / 'tokens.py'
        worker.write_text("print('完成：成功 1 / 失败 0', flush=True)\nprint('[Token] 正在下载 Saproling', flush=True)\nprint('Token 完成：成功 2 / 失败 0 / 关联检查失败 1', flush=True)\n", encoding='utf-8')
        with patch.object(app, 'engine_command', return_value=[sys.executable, '-u', str(worker)]) as command:
            self.gui.start()
            self.assertEqual(command.call_args.args[2], 4)
            self.assertNotIn('线程', self.gui.detail.get())
            self.pump()
        self.assertEqual(self.gui.summary, (1, 0))
        self.assertEqual(self.gui.token_summary, (2, 0, 1))
        self.assertIn('部分卡牌失败', self.gui.status.get())
        worker.write_text("print('完成：成功 1 / 失败 0', flush=True)\n", encoding='utf-8')
        with patch.object(app, 'engine_command', return_value=[sys.executable, '-u', str(worker)]):
            self.gui.start()
            self.assertIsNone(self.gui.token_summary)
            self.pump()
        self.assertEqual(self.gui.status.get(), '任务完成。')
        self.assertNotIn('Token', self.gui.detail.get())

    def test_engine_help(self):
        completed = subprocess.run([sys.executable, str(Path(app.__file__)), '--engine', '--help'], capture_output=True, encoding='utf-8', errors='replace', env={**app.os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertIn('--workers', completed.stdout)
        self.assertIn('--no-cut-marks', completed.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
