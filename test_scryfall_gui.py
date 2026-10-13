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

# Compose current sources in memory, as the standalone builder does. No generated
# artifact needs to be modified to test gui_layer.py before integration.
base = Path(__file__).resolve().parent
app = importlib.util.module_from_spec(importlib.machinery.ModuleSpec('gui_app', loader=None))
app.__file__ = str(base / 'scryfall_cards_gui.pyw')
sys.modules[app.__name__] = app
core = (base / 'scryfall_cards_threaded.py').read_text(encoding='utf-8')
core = core.split('\nif __name__ == "__main__":')[0]
ptcg_path = base / 'ptcg_engine.py'
if not ptcg_path.exists():
    raise RuntimeError('GUI integration tests require ptcg_engine.py; integrate the PTCG engine first.')
ptcg = ptcg_path.read_text(encoding='utf-8')
layer = (base / 'gui_layer.py').read_text(encoding='utf-8')
exec(compile(core + '\n' + ptcg + '\n' + layer, app.__file__, 'exec'), app.__dict__)

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
        self.assertIn('19 张', self.gui.count.get())
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

    def test_six_games_concurrency_language_and_examples(self):
        games = ('万智牌', '宝可梦TCG（中文）', '宝可梦TCG（英文）',
                 '游戏王（简中）', '游戏王（日文）', '游戏王（TCG）')
        self.assertEqual(tuple(self.gui.game_box['values']), games)
        self.assertEqual(str(self.gui.game_box['state']), 'readonly')
        for game in games:
            with self.subTest(game=game):
                self.gui.game.set(game)
                self.gui.example()
                self.assertNotIn('格式错误', self.gui.count.get())
                code = self.gui.game_code()
                if code.startswith('ygo-'):
                    self.assertEqual(self.gui.count.get(), '3 种牌 · 3 张')
                    self.assertIn('占位', self.gui.network_policy.get())
                    continue
                text, workers, paper, langs = self.gui.configuration()
                self.assertEqual(len(self.gui.configuration()), 4)
                self.assertEqual(self.gui.concurrency(), (4, 8) if code == 'mtg' else (64, 64))
                self.assertEqual(langs, {'mtg': 'zhs,zht,en', 'ptcg-zh': 'zh-tw', 'ptcg-en': 'en'}[code])
                if code != 'mtg':
                    self.assertEqual(str(self.gui.language_box['state']), 'disabled')
                    self.assertNotIn('待接入', self.gui.network_policy.get())
                    self.gui.language.set('zhs,en')
                    self.assertEqual(self.gui.configuration()[3], langs)
                    self.gui.set_running(True)
                    self.gui.set_running(False)
                    self.assertEqual(str(self.gui.language_box['state']), 'disabled')
        self.gui.game.set('万智牌')
        self.assertEqual(str(self.gui.language_box['state']), 'normal')

    def test_game_command_routes_paths_and_language(self):
        task = self.gui.output_root / 'task'
        for code, folder in [('mtg', 'mtg-pics'), ('ptcg-zh', 'ptcg-pics/zh-tw'), ('ptcg-en', 'ptcg-pics/en')]:
            command = app.engine_command('deck.txt', task, 64, 'A4', 'zhs,zht,en',
                                         True, True, False, False, 64, code)
            self.assertEqual(command[command.index('--game') + 1], code)
            self.assertEqual(command[command.index('--pics') + 1], str(self.gui.output_root / folder))
            self.assertEqual('--langs' in command, code == 'mtg')
        for code in ('ygo-zh', 'ygo-ja', 'ygo-tcg'):
            with self.assertRaises(ValueError):
                app.engine_command('deck.txt', task, 4, 'A4', 'en', True, True, False, False, game=code)

    def test_ptcg_invalid_decks_never_create_tasks(self):
        for game in ('宝可梦TCG（中文）', '宝可梦TCG（英文）'):
            self.gui.game.set(game)
            for text in ('4 Pikachu', '4 Pikachu sv1', '0 Pikachu sv1 062', 'Pokémon\nTrainer'):
                with self.subTest(game=game, text=text):
                    self.gui.set_deck(text)
                    with patch.object(app.messagebox, 'showerror') as error, \
                         patch.object(app, 'engine_command') as engine, \
                         patch.object(app.subprocess, 'Popen') as process:
                        self.gui.start()
                    error.assert_called_once()
                    engine.assert_not_called()
                    process.assert_not_called()
                    self.assertFalse(self.gui.output_root.exists())
                    self.assertFalse(self.gui.running)

    def test_yugioh_placeholder_names_no_download_or_directory(self):
        for game in ('游戏王（简中）', '游戏王（日文）', '游戏王（TCG）'):
            self.gui.game.set(game)
            self.gui.set_deck('Blue-Eyes White Dragon\n2 Dark Magician\n# comment')
            self.assertEqual(self.gui.count.get(), '2 种牌 · 3 张')
            self.assertEqual(self.gui.pics_label.get(), '尚未接入，暂无图片目录')
            with patch.object(app.messagebox, 'showerror') as error, \
                 patch.object(app, 'engine_command') as engine, \
                 patch.object(app.subprocess, 'Popen') as process:
                self.gui.start()
                self.gui.open_pics()
            self.assertEqual(error.call_count, 2)
            self.assertIn('尚未接入', error.call_args_list[0].args[1])
            engine.assert_not_called()
            process.assert_not_called()
            self.assertFalse(self.gui.output_root.exists())

    def test_titles_skipped_and_ptcg_prints_counted(self):
        self.gui.set_deck('Main Deck\n  4 Lightning Bolt\nSideboard\n1 Forest')
        self.assertEqual(self.gui.count.get(), '2 种牌 · 5 张')
        self.gui.game.set('宝可梦TCG（英文）')
        self.gui.set_deck('Pokémon\n4 Pikachu sv1 062\nTrainer\n2 Pikachu sv2 025')
        self.assertEqual(self.gui.count.get(), '2 种牌 · 6 张')

    def test_ptcg_start_routes_real_parser_without_network(self):
        for game, code in [('宝可梦TCG（中文）', 'ptcg-zh'), ('宝可梦TCG（英文）', 'ptcg-en')]:
            self.gui.game.set(game)
            self.gui.example()
            with patch.object(app, 'engine_command', return_value=['unused']) as engine, \
                 patch.object(app.threading, 'Thread') as thread:
                self.gui.start()
            self.assertTrue(self.gui.running)
            self.assertEqual(engine.call_args.kwargs, {'download_workers': 64, 'game': code})
            self.assertEqual(engine.call_args.args[2], 64)
            self.assertTrue((self.gui.output_dir / '本次牌表.txt').exists())
            thread.return_value.start.assert_called_once()
            self.gui.progress.stop()
            self.gui.set_running(False)

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
        self.assertEqual(command[command.index('--pics') + 1], str(self.gui.output_root / 'mtg-pics'))
        self.gui.interpret_log('[缓存 1/2] Lightning Bolt -> 闪电击（1 张已有，0 张待补下载）')
        self.assertIn('本地缓存 1/2', self.gui.status.get())
        self.assertEqual(float(self.gui.progress['value']), 1)
        with patch.object(self.gui, '_open_path') as opened:
            self.gui.open_pics()
        opened.assert_called_once_with(self.gui.output_root / 'mtg-pics')
        self.assertTrue((self.gui.output_root / 'mtg-pics').is_dir())

    def test_game_picture_directories_labels_and_open_are_isolated(self):
        root = self.gui.output_root
        for game, folder in [('万智牌', 'mtg-pics'), ('宝可梦TCG（中文）', 'ptcg-pics/zh-tw'), ('宝可梦TCG（英文）', 'ptcg-pics/en')]:
            self.gui.game.set(game)
            target = root / folder
            self.assertEqual(self.gui.picture_directory(), target)
            self.assertEqual(self.gui.pics_label.get(), str(target))
            with patch.object(self.gui, '_open_path') as opened:
                self.gui.open_pics()
            opened.assert_called_once_with(target)
            (target / 'same-name.png').write_bytes(folder.encode())
        for folder in ('mtg-pics', 'ptcg-pics/zh-tw', 'ptcg-pics/en'):
            self.assertEqual((root / folder / 'same-name.png').read_bytes(), folder.encode())
        self.assertFalse((root / 'pics').exists())

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
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            app.main(['--help'])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn('--workers', output.getvalue())
        self.assertIn('--no-cut-marks', output.getvalue())
        self.assertIn('--game', output.getvalue())
        self.assertIn('ptcg-zh', output.getvalue())
        self.assertNotIn('ygo-', output.getvalue())

if __name__ == '__main__':
    unittest.main(verbosity=2)
