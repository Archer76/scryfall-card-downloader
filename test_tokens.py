import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scryfall_cards_threaded as app
from test_shared_picture_cache import png
from test_network_policy import Clock


def card(identity, name='Soldier', oracle='soldier', lang='en', **extra):
    result = {'id': identity, 'oracle_id': oracle, 'name': name, 'lang': lang,
              'set': 'tst', 'set_type': 'token', 'type_line': 'Token Creature — Soldier',
              'collector_number': '1', 'image_status': 'highres_scan',
              'image_uris': {'png': 'https://example.test/' + identity + '.png'}}
    if oracle is None:
        result.pop('oracle_id')
    result.update(extra)
    return result


def part(identity, component='token'):
    return {'id': identity, 'component': component}


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.deck = self.base / 'deck.txt'
        self.pics = self.base / 'pics'
        self.output = self.base / 'out'
        self.downloads = []
        self.api_calls = []
        self.objects = {}
        self.main_cards = {}
        app._reset_network_state()
        app._print_cache.clear()

    def tearDown(self):
        self.temp.cleanup()

    def api(self, path, params=None, **kwargs):
        self.api_calls.append(path)
        if path.startswith('/cards/'):
            assert kwargs.get('cache_missing') is False, 'token lookups must not use 404 negative cache'
        value = self.objects.get(path.removeprefix('/cards/'))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def download(self, url, dest):
        self.downloads.append(url)
        Path(dest).write_bytes(png())
        return len(png())

    def run_task(self, deck='2 Maker\n', extra=(), prints=None, pdf=None):
        self.deck.write_text(deck, encoding='utf-8')
        resolved = lambda name: copy.deepcopy(self.main_cards[name])
        selected = lambda oracle, langs, basic: (copy.deepcopy(next(c for c in self.main_cards.values() if c['oracle_id'] == oracle)), 'en')
        with patch.object(app, 'resolve_card', side_effect=resolved), patch.object(app, 'select_print', side_effect=selected), patch.object(app, 'resolve_display_names', side_effect=lambda o, s: (app.face_names(s), 'tst')), patch.object(app, 'api_get', side_effect=self.api), patch.object(app, 'prints_for', side_effect=prints or (lambda *a: [])), patch.object(app, 'download', side_effect=self.download), patch.object(app, 'make_pdf', return_value=None) as pdf_spy, patch.object(app, 'program_cache_directory', return_value=str(self.base / 'api-cache')), contextlib.redirect_stdout(io.StringIO()):
            code = app.main([str(self.deck), '--pics', str(self.pics), '--out', str(self.output)] + list(extra))
        return code, json.loads((self.output / 'manifest.json').read_text(encoding='utf-8')), pdf_spy

    def setup_maker(self, ids=('t1',)):
        self.main_cards['Maker'] = card('m1', 'Maker', 'maker', type_line='Creature', all_parts=[part(i) for i in ids])
        self.objects['t1'] = card('t1')

    def test_parts_union_filter_rules_faces_and_nonrecursive(self):
        resolved = card('r', all_parts=[part('t'), part('c', 'combo_piece'), part('m', 'meld_result')])
        selected = card('s', all_parts=[part('t'), part('u')])
        self.assertEqual(app.inspect_token_parts(resolved, selected)['related_ids'], ['t', 'u'])
        self.assertEqual(app.inspect_token_parts(resolved, selected)['source_ids'], ['r', 's'])
        self.assertEqual(app.inspect_token_parts({'card_faces': [{'oracle_text': 'Create a copy token.'}]})['status'], 'rules_only')
        self.assertEqual(app.inspect_token_parts({'oracle_text': 'Destroy target token.'})['status'], 'none')
        self.setup_maker()
        self.objects['t1']['all_parts'] = [part('should-not-query')]
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, ['/cards/t1'])
        self.assertEqual(len(manifest['tokens']), 1)

    def test_duplicate_relations_cache_reload_zero_requests_and_pdf_quantity(self):
        self.setup_maker(('t1', 't2', 't1'))
        self.objects['t2'] = card('t2')  # different printing, same oracle
        self.main_cards['Other'] = card('m2', 'Other', 'other', all_parts=[part('t1')])
        code, manifest, pdf = self.run_task('3 Maker\n2 Other\n1 Maker\n')
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, ['/cards/t1', '/cards/t2'])
        self.assertEqual(len(manifest['cards']), 3)
        self.assertEqual(len(manifest['tokens']), 1)
        self.assertEqual(len(pdf.call_args.args[0]), 7)
        self.assertEqual(self.downloads.count('https://example.test/t1.png'), 1)
        self.downloads.clear()
        self.api_calls.clear()
        code, second, pdf = self.run_task('3 Maker\n2 Other\n1 Maker\n')
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.downloads, [])
        self.assertTrue(second['tokens'][0]['cache_hit'])
        self.assertEqual(len(pdf.call_args.args[0]), 7)

    def test_same_name_different_identity_and_card_namespace(self):
        self.setup_maker(('t1', 't2', 't3'))
        self.objects['t2'] = card('t2', oracle='other-spec')
        self.objects['t3'] = card('t3', oracle=None)
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        tokens = manifest['tokens']
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len({t['files'][0]['file'] for t in tokens}), 3)
        cache = app.PictureCache(str(self.pics))
        self.assertNotIn('soldier', cache.data['aliases'])
        self.assertEqual({t['cache_identity'] for t in tokens}, {'token:oracle:soldier', 'token:oracle:other-spec', 'token:card:t3'})

    def test_language_guard_fallback_emblem_and_no_oracle_search(self):
        self.objects['t1'] = card('t1', type_line='Emblem — Walker')
        wrong = card('wrong', oracle='wrong', lang='zhs')
        placeholder = card('placeholder', lang='zhs', image_status='placeholder')
        zh = card('zh', lang='zht', printed_name='徽记')
        def prints(oracle, suffix):
            return [wrong, placeholder] if suffix == 'lang:zhs' else [zh]
        with patch.object(app, 'api_get', side_effect=self.api), patch.object(app, 'prints_for', side_effect=prints):
            source, selected, identity = app.query_token('t1', ['zhs', 'zht', 'en'])
        self.assertEqual(selected['id'], 'zh')
        cache = app.PictureCache(str(self.pics))
        entry = app.token_entry(source, selected, identity, cache)
        self.assertEqual(entry['token_type'], 'emblem')
        self.assertEqual(entry['lang_code'], 'zht')
        self.objects['t2'] = card('t2', oracle=None)
        with patch.object(app, 'api_get', side_effect=self.api), patch.object(app, 'prints_for', side_effect=AssertionError('must not search')):
            self.assertEqual(app.query_token('t2', ['zhs', 'en'])[1]['id'], 't2')

    def test_old_cache_specific_id_once_and_failure_retry_keeps_main(self):
        from test_shared_picture_cache import entry
        self.pics.mkdir()
        cached = entry(self.pics, 'Maker', 'Maker')
        cached['selected_id'] = 'old'
        Path(cached['files'][0]['file']).write_bytes(png())
        cache = app.PictureCache(str(self.pics))
        cache.store(cached)
        cache.save()
        self.objects['old'] = RuntimeError('offline')
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'exists')
        self.assertEqual(manifest['token_summary']['check_failed'], 1)
        self.assertEqual(len(pdf.call_args.args[0]), 2)
        self.objects['old'] = card('old', 'Maker', 'maker', all_parts=[])
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['token_check']['status'], 'none')
        self.api_calls.clear()
        self.run_task()
        self.assertEqual(self.api_calls, [])

    def test_legacy_image_uuid_and_local_image_unavailable(self):
        uuid = '12345678-1234-1234-1234-123456789abc'
        for host in ('cards.scryfall.io', 'c1.scryfall.com'):
            entry = {'files': [{'url': 'https://' + host + '/png/front/1/2/' + uuid + '.png?1'}]}
            self.assertEqual(app.cached_print_id(entry), uuid)
        self.assertIsNone(app.cached_print_id({'files': [{'url': 'https://example.test/' + uuid + '.png'}]}))
        self.pics.mkdir()
        (self.pics / 'Maker.png').write_bytes(png())
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(manifest['cards'][0]['token_check']['status'], 'unavailable')

    def test_token_detail_failure_preserves_main_and_retries(self):
        self.setup_maker(('t1', 'bad'))
        self.objects['bad'] = RuntimeError('token offline')
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['summary'], {'processed': 1, 'ok': 1, 'failed': 0})
        self.assertEqual(manifest['token_summary']['failed'], 1)
        self.assertTrue(manifest['cards'][0]['token_check']['checked'])
        self.assertEqual(len(manifest['cards'][0]['token_refs']), 2)
        self.assertEqual(len(pdf.call_args.args[0]), 3)
        self.objects['bad'] = card('bad', oracle='other')
        self.api_calls.clear()
        code, _, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, ['/cards/bad'])

    def test_double_token_repair_only_missing_face_no_api(self):
        self.setup_maker()
        self.objects['t1'] = card('t1', image_uris=None, card_faces=[{'name': 'Front', 'image_uris': {'png': 'https://example.test/front.png'}}, {'name': 'Back', 'image_uris': {'png': 'https://example.test/back.png'}}])
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual([f['label'] for f in manifest['tokens'][0]['files']], ['正面', '背面'])
        self.assertEqual(len(pdf.call_args.args[0]), 4)
        Path(manifest['tokens'][0]['files'][1]['file']).write_bytes(png()[:-5])
        self.api_calls.clear()
        self.downloads.clear()
        self.run_task()
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.downloads, ['https://example.test/back.png'])

    def test_token_download_failure_retries_url_and_keeps_relation(self):
        self.setup_maker()
        real = self.download
        def fail(url, dest):
            if url.endswith('/t1.png'):
                raise RuntimeError('image offline')
            return real(url, dest)
        self.download = fail
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['tokens'][0]['status'], 'error')
        self.assertTrue(manifest['cards'][0]['token_check']['checked'])
        self.assertEqual(len(pdf.call_args.args[0]), 2)
        self.download = real
        self.api_calls.clear()
        code, _, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.api_calls, [])

    def test_force_refresh_and_dry_run(self):
        self.setup_maker()
        self.run_task()
        self.main_cards['Maker']['id'] = 'new-main'
        self.main_cards['Maker']['all_parts'] = [part('new-token')]
        self.objects['new-token'] = card('new-token', oracle='new')
        code, manifest, _ = self.run_task(extra=['--force'])
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['token_check']['source_ids'], ['new-main'])
        self.assertEqual(manifest['tokens'][0]['related_ids'], ['new-token'])
        self.downloads.clear()
        code, manifest, pdf = self.run_task(extra=['--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(self.downloads, [])
        pdf.assert_not_called()
        self.assertEqual(manifest['tokens'][0]['status'], 'dry-run')

    def test_jpeg_token_is_embedded_in_pdf_not_skipped(self):
        import struct
        jpeg = (b'\xff\xd8' + b'\xff\xe0' + struct.pack('>H', 4) + b'\x00\x00'
                + b'\xff\xc0' + struct.pack('>HBHHB', 11, 8, 88, 63, 3) + b'\x01\x11\x00'
                + b'\xff\xd9')
        self.pics.mkdir()
        image = self.pics / 'token.jpg'
        image.write_bytes(jpeg)
        self.assertEqual(app.jpeg_info(str(image))[:2], (63, 88))
        pdf = self.base / 'jpeg.pdf'
        with contextlib.redirect_stdout(io.StringIO()):
            info = app.make_pdf([str(image)], 'A4', str(pdf))
        self.assertEqual(info['cards'], 1)
        self.assertIn(b'/DCTDecode', pdf.read_bytes())
        broken = self.pics / 'broken.jpg'
        broken.write_bytes(b'not jpeg')
        with self.assertRaises(ValueError):
            app.jpeg_info(str(broken))

    def test_real_api_get_token_404_not_negatively_cached(self):
        import urllib.error
        from email.message import Message
        app._api_disk_cache_dir = str(self.base / 'api-cache')
        try:
            missing = urllib.error.HTTPError('https://example.test', 404, 'missing', Message(), io.BytesIO())
            ok = io.BytesIO(json.dumps(card('t9')).encode())
            with patch.object(app, 'opener') as opener, patch.object(app, 'prints_for', return_value=[]), patch.object(app, '_throttle'), patch.object(app, '_query_log'):
                opener.return_value.open.side_effect = [missing, ok]
                with self.assertRaises(RuntimeError):
                    app.query_token('t9', ['en'])
                self.assertEqual(list((self.base / 'api-cache').glob('*.json')), [])
                self.assertEqual(app.query_token('t9', ['en'])[1]['id'], 't9')
                self.assertEqual(opener.return_value.open.call_count, 2)
        finally:
            app._api_disk_cache_dir = None

    def test_query_and_download_workers_are_independent(self):
        self.assertEqual(app.DEFAULT_WORKERS, 4)
        self.assertEqual(app.DEFAULT_DOWNLOAD_WORKERS, 8)
        self.assertEqual(app.MIN_API_INTERVAL, 0.6)
        self.assertEqual(app.MIN_IMAGE_INTERVAL, 0)
        self.setup_maker(ids=())
        with patch.object(app, 'query_all', wraps=app.query_all) as query, patch.object(app, 'download_all', wraps=app.download_all) as download:
            code, _, _ = self.run_task()
            self.assertEqual(code, 0)
            self.assertEqual(query.call_args.args[2], 4)
            self.assertEqual(download.call_args.args[1], 8)
        with patch.object(app, 'query_all', wraps=app.query_all) as query, patch.object(app, 'download_all', wraps=app.download_all) as download:
            code, _, _ = self.run_task(extra=['--force', '--workers', '2', '--download-workers', '32'])
            self.assertEqual(code, 0)
            self.assertEqual(query.call_args.args[2], 2)
            self.assertEqual(download.call_args.args[1], 32)
        for invalid in ('0', '33'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app._main(['--download-workers', invalid])

    def test_cli_image_interval_validation_and_separate_clocks(self):
        for invalid in ('-1', 'nan', 'inf', '-inf'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app._main(['--image-interval=' + invalid])
        self.setup_maker(ids=())
        for interval in ('0', '0.1', '0.2'):
            code, _, _ = self.run_task(extra=['--image-interval', interval, '--no-pdf'])
            self.assertEqual(code, 0)
            self.assertEqual(app._image_interval, float(interval))
            self.assertEqual(app.MIN_API_INTERVAL, 0.6)
        clock = Clock()
        with patch.object(app, 'time', clock):
            app._reset_network_state()
            app._image_interval = 0.1
            app._throttle_images()
            before = clock.value
            app._throttle_images()
            self.assertAlmostEqual(clock.value - before, 0.1)
            app._image_interval = 0
            app._pause_images(7)
            before = clock.value
            app._throttle_images()
            self.assertGreaterEqual(clock.value - before, 7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
