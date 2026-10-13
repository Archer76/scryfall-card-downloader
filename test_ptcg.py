import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import scryfall_cards_threaded as core
import ptcg_engine as ptcg
from test_shared_picture_cache import png


class PTCGTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.deck = self.base / 'deck.txt'
        self.out = self.base / 'out'
        self.calls = []
        self.downloads = []
        self.language = 'en'
        self.data = {
            '/sets/sv10': {'id': 'sv10', 'cards': [{'id': 'sv10-010', 'localId': '010', 'name': 'Shaymin'}]},
            '/cards/sv10-010': {'id': 'sv10-010', 'localId': '010', 'name': 'Shaymin', 'set': {'id': 'sv10', 'name': 'Destined Rivals'}, 'image': 'https://assets.tcgdex.net/en/sv/sv10/010'},
            '/sets/SV2a': {'id': 'SV2a', 'cards': [{'id': 'SV2a-001', 'localId': '001', 'name': '妙蛙種子'}]},
            '/cards/SV2a-001': {'id': 'SV2a-001', 'localId': '001', 'name': '妙蛙種子', 'set': {'id': 'SV2a'}, 'image': 'https://assets.tcgdex.net/zh-tw/SV/SV2a/001'},
        }

    def tearDown(self):
        self.temp.cleanup()

    def get(self, client, path):
        self.calls.append((client.language, path))
        value = self.data.get(path)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def download(self, url, path):
        self.downloads.append(url)
        Path(path).write_bytes(png())
        return len(png())

    def run_task(self, text='Pokémon:19\n2 Shaymin DRI 10\nTotal Cards:60\n', game='ptcg-en', flags=()):
        self.deck.write_text(text, encoding='utf-8')
        with patch.object(ptcg.PTCGClient, 'get', autospec=True, side_effect=self.get), patch.object(core, 'program_cache_directory', return_value=str(self.base / '.api-cache')), patch.object(core, 'download', side_effect=self.download), patch.object(core, 'api_get', side_effect=AssertionError('must not call Scryfall')), patch.object(core, 'make_pdf', return_value={'cards': sum(q for q, *rest in ptcg.parse_ptcg_decklist(text))}) as pdf, contextlib.redirect_stdout(io.StringIO()):
            code = core.main([str(self.deck), '--game', game, '--out', str(self.out)] + list(flags))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        return code, manifest, pdf

    def test_parser_rejects_unversioned_rows_and_preserves_names(self):
        rows = ptcg.parse_ptcg_decklist("Pokémon:19\n1 Marnie's Grimmsnarl ex ASC 287\nTrainer:33\n1 Technical Machine: Evolution PAR 178\nEnergy:8\n8 Basic Darkness Energy SVE 23\nTotal Cards:60")
        self.assertEqual(rows[0], (1, "Marnie's Grimmsnarl ex", 'ASC', '287'))
        self.assertEqual(rows[1][1], 'Technical Machine: Evolution')
        self.assertEqual(sum(r[0] for r in rows), 10)
        for text in ('1 Shaymin', '1 Basic Darkness Energy', '1 Technical Machine: Evolution', '0 Shaymin DRI 10', '2 Shaymin DRI', '1'):
            with self.assertRaises(ValueError):
                ptcg.parse_ptcg_decklist(text)
        self.assertEqual(core.parse_decklist('Lightning Bolt\nPokémon:19\n2 Forest\nTotal Cards:60'), [(2, 'Forest')])

    def test_exact_number_language_cache_quantity_and_no_scryfall(self):
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [('en', '/sets/sv10'), ('en', '/cards/sv10-010')])
        self.assertEqual(manifest['cards'][0]['selected_id'], 'sv10-010')
        self.assertEqual(len(pdf.call_args.args[0]), 2)
        self.assertEqual(Path(manifest['out_dir']), self.base / '卡图输出' / 'ptcg-pics' / 'en')
        self.calls.clear()
        self.downloads.clear()
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertTrue(manifest['cards'][0]['cache_hit'])
        Path(manifest['cards'][0]['files'][0]['file']).write_bytes(b'broken')
        self.calls.clear()
        code, _, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.downloads), 1)

    def test_chinese_is_separate_no_fallback(self):
        code, manifest, _ = self.run_task('1 妙蛙種子 SV2a 1', 'ptcg-zh')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [('zh-tw', '/sets/SV2a'), ('zh-tw', '/cards/SV2a-001')])
        self.assertTrue(manifest['out_dir'].endswith(str(Path('ptcg-pics') / 'zh-tw')))
        self.calls.clear()
        code, manifest, _ = self.run_task('1 Shaymin DRI 10', 'ptcg-zh')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_set')
        self.assertEqual(self.calls, [('zh-tw', '/sets/DRI')])

    def test_wrong_card_name_identity_missing_image_and_partial_output(self):
        self.data['/cards/sv10-010']['image'] = None
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        pdf.assert_not_called()
        self.data['/cards/sv10-010']['image'] = 'https://assets.tcgdex.net/en/sv/sv10/010'
        self.data['/cards/sv10-010']['id'] = 'SV10-010'
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'identity_mismatch')
        self.data['/cards/sv10-010']['id'] = 'sv10-010'
        code, manifest, pdf = self.run_task('2 Shaymin DRI 10\n1 Wrong Name DRI 10')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['summary'], {'processed': 2, 'ok': 1, 'failed': 1})
        self.assertEqual(manifest['cards'][1]['status'], 'name_mismatch')
        self.assertEqual(len(pdf.call_args.args[0]), 2)

    def test_dry_run_force_and_failed_download_retry(self):
        code, manifest, pdf = self.run_task(flags=['--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(self.downloads, [])
        pdf.assert_not_called()
        self.assertEqual(manifest['cards'][0]['status'], 'dry-run')
        self.calls.clear()
        self.run_task()
        self.assertEqual(self.calls, [])
        self.calls.clear()
        self.run_task(flags=['--force'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.downloads), 2)

    def test_image_language_guard_and_pdf_failure(self):
        self.data['/cards/sv10-010']['image'] = 'https://assets.tcgdex.net/zh-tw/SV/SV10/010'
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'invalid_image')
        self.assertEqual(self.downloads, [])
        self.data['/cards/sv10-010']['image'] = 'https://assets.tcgdex.net/en/sv/sv10/010'
        self.deck.write_text('1 Shaymin DRI 10', encoding='utf-8')
        with patch.object(ptcg.PTCGClient, 'get', autospec=True, side_effect=self.get), patch.object(core, 'program_cache_directory', return_value=str(self.base / '.cache')), patch.object(core, 'download', side_effect=self.download), patch.object(core, 'make_pdf', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(core.main([str(self.deck), '--game', 'ptcg-en', '--out', str(self.out)]), 1)

    def test_download_failure_recovery_does_not_keep_error(self):
        good_download = self.download
        self.download = lambda *a: (_ for _ in ()).throw(RuntimeError('offline image'))
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 1)
        self.assertIn('error', manifest['cards'][0])
        self.download = good_download
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertNotIn('error', manifest['cards'][0])
        self.assertNotIn('图片下载失败', (self.out / 'manifest.txt').read_text(encoding='utf-8'))

    def test_malformed_task_preflight_creates_no_output(self):
        self.deck.write_text('1 Shaymin', encoding='utf-8')
        with self.assertRaises(ValueError), patch.object(ptcg.PTCGClient, 'get', side_effect=AssertionError('no API')):
            core.main([str(self.deck), '--game', 'ptcg-en', '--out', str(self.out)])
        self.assertFalse(self.out.exists())

    def test_api_cache_list_and_404_recovery(self):
        import urllib.error
        from email.message import Message
        client = ptcg.PTCGClient('en', str(self.base), core)
        missing = urllib.error.HTTPError('https://example.test', 404, 'missing', Message(), io.BytesIO())
        with patch.object(core, 'opener') as opener:
            opener.return_value.open.side_effect = [missing, io.BytesIO(b'[]')]
            self.assertIsNone(client.get('/cards/example'))
            self.assertEqual(client.get('/cards/example'), [])
            self.assertEqual(client.get('/cards/example'), [])
            self.assertEqual(opener.return_value.open.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
