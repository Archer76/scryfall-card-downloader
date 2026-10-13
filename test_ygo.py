"""Offline Yu-Gi-Oh regressions. Run: python -m unittest test_ygo -v.

Only the real YGO request boundary/core opener is replaced. Pillow decoding,
PNG CRC validation, disk caches, CLI dispatch and PDF quantity wiring stay real.
"""
import contextlib
import copy
from email.message import Message
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zlib

from PIL import Image
import scryfall_cards_threaded as core
import ygo_engine as ygo


def image_bytes(kind='WEBP'):
    stream = io.BytesIO()
    Image.new('RGB', (8, 12), (51, 102, 153)).save(stream, format=kind)
    return stream.getvalue()


def baige_url(card_id='123', lang='sc'):
    return '%s/%s/%s.webp' % (ygo.YGO_BAIGE_IMAGES, lang, card_id)


def deck_url(card_id='123'):
    return ygo.YGO_DECK_API + '?id=' + card_id


def deck_image(card_id='123'):
    return 'https://images.ygoprodeck.com/images/cards/%s.jpg' % card_id


def deck_data(card_id='123', image_id=None):
    return {'data': [{'id': int(card_id), 'name': 'Offline card', 'card_images': [
        {'id': int(image_id or card_id), 'image_url': deck_image(image_id or card_id)}]}]}


class YGOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.pics = self.base / 'pics'
        self.out = self.base / 'out'
        self.api = self.base / 'api'
        self.calls = []
        self.routes = {}
        self.webp = image_bytes()
        self.jpeg = image_bytes('JPEG')
        # A second safety net makes accidentally unmocked network impossible.
        self.addCleanup(patch.stopall)
        patch.object(core, 'opener', side_effect=AssertionError('REAL NETWORK FORBIDDEN')).start()
        patch.object(core, 'api_get', side_effect=AssertionError('Scryfall must not be used')).start()
        patch.object(core, 'program_cache_directory', return_value=str(self.api)).start()
        patch.object(ygo.YGOClient, 'request', autospec=True, side_effect=self.request).start()
        self.good_card()

    def good_card(self, card_id='123', lang='sc'):
        self.routes[(ygo.YGO_BAIGE_API + card_id, 'GET')] = json.dumps({
            'id': int(card_id), 'text': {'sc_name': '离线卡', 'jp_name': 'Offline JP', 'en_name': 'Offline EN'}}).encode()
        self.routes[(baige_url(card_id, lang), 'GET')] = self.webp
        self.routes[(baige_url(card_id, lang), 'HEAD')] = 'image/webp'

    def request(self, client, url, source, method='GET'):
        self.calls.append((url, source, method))
        value = self.routes.get((url, method), ygo.YGOError('offline missing endpoint'))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    def run_task(self, text='123\n', flags=(), pdf_count=None):
        deck = self.base / 'deck.ydk'
        deck.write_text(text, encoding='utf-8')
        def pdf_result(paths, *args, **kwargs):
            return {'cards': len(paths) if pdf_count is None else pdf_count}
        with patch.object(core, 'make_pdf', side_effect=pdf_result) as pdf, contextlib.redirect_stdout(io.StringIO()):
            code = core.main([str(deck), '--game', 'ygo', '--out', str(self.out), '--pics', str(self.pics),
                              '--workers', '1', '--download-workers', '1', '--image-interval', '0'] + list(flags))
        manifest_path = self.out / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else None
        return code, manifest, pdf

    def urls(self):
        return [call[0] for call in self.calls]

    def image_urls(self):
        return [url for url, source, method in self.calls if source.endswith('image')]

    def success_cache(self):
        return json.loads((self.pics / 'ygo-index.json').read_text(encoding='utf-8'))['cards']

    def enable_fallback(self, card_id='123', data=None):
        self.routes[(deck_url(card_id), 'GET')] = json.dumps(data or deck_data(card_id)).encode()
        self.routes[(deck_image(card_id), 'GET')] = self.jpeg
        self.routes[(deck_image(card_id), 'HEAD')] = 'image/jpeg'

    def test_default_picture_directory_and_unchanged_game_index(self):
        deck = self.base / 'default.ydk'
        deck.write_text('123\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            code = core.main([str(deck), '--game', 'ygo', '--out', str(self.out), '--no-pdf'])
        target = self.base / '卡图输出' / 'yugioh-pics'
        self.assertEqual(code, 0)
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(Path(manifest['out_dir']), target)
        self.assertEqual(manifest['game'], 'ygo')
        self.assertTrue((target / '123.png').is_file())
        self.assertTrue((target / 'ygo-index.json').is_file())
        self.assertFalse(self.pics.exists())

    def test_help_names_default_yugioh_picture_directory(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            core.main(['--help'])
        self.assertEqual(result.exception.code, 0)
        self.assertIn('yugioh-pics', output.getvalue())

    def test_ydk_ascii_positive_one_to_eight_digits_and_duplicates(self):
        text = '\ufeff#created\n#main\n1\n00000001\n123\n00123\n99999999\n#extra\n!side\n42\n'
        self.assertEqual(ygo.parse_ygo_decklist(text), [(2, '1'), (2, '123'), (1, '99999999'), (1, '42')])

    def test_ydk_skips_zero_unicode_long_quantity_name_and_comments(self):
        self.assertEqual(ygo.parse_ygo_decklist('0\n00000000\n123456789\n１２３\n١٢٣\n+123\n-123\n1e3\n1.0\n2 123\n123 #card\n#123\nBlue Eyes\n!side\n'), [])
        self.assertEqual(ygo.parse_ygo_decklist(' 123 \n\t7\t'), [(1, '123'), (1, '7')])

    def test_empty_deck_no_requests_or_output(self):
        code, manifest, pdf = self.run_task('#main\n0\nnot a password')
        self.assertEqual(code, 2)
        self.assertIsNone(manifest)
        self.assertEqual(self.calls, [])
        pdf.assert_not_called()

    def test_sc_success_never_requests_jp_en_or_deck(self):
        self.enable_fallback()
        code, manifest, pdf = self.run_task('123\n00123\n')
        self.assertEqual(code, 0)
        self.assertEqual(self.urls(), [ygo.YGO_BAIGE_API + '123', baige_url()])
        card = manifest['cards'][0]
        self.assertEqual((card['lang_code'], card['source'], card['qty']), ('sc', 'baige', 2))
        self.assertEqual(len(pdf.call_args.args[0]), 2)
        path = card['files'][0]['file']
        self.assertTrue(core.complete_image(path, card['files'][0]['bytes']))
        with Image.open(path) as im:
            self.assertEqual((im.format, im.mode, im.size), ('PNG', 'RGB', (8, 12)))
            im.load()

    def test_sc_failure_jp_precedes_en_and_deck(self):
        self.routes.pop((baige_url(), 'GET'))
        self.good_card(lang='jp')
        self.good_card(lang='en')
        self.enable_fallback()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.image_urls(), [baige_url(), baige_url(lang='jp')])
        self.assertEqual(manifest['cards'][0]['lang_code'], 'jp')
        self.assertNotIn(deck_url(), self.urls())

    def test_en_after_sc_and_jp_failure(self):
        self.routes.pop((baige_url(), 'GET'))
        self.good_card(lang='en')
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.image_urls(), [baige_url(lang=lang) for lang in ygo.YGO_LANGUAGES])
        self.assertEqual(manifest['cards'][0]['lang_code'], 'en')

    def test_all_baige_images_fail_before_deck_query_and_jpeg_decode(self):
        self.routes.pop((baige_url(), 'GET'))
        self.enable_fallback()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.urls(), [ygo.YGO_BAIGE_API + '123'] + [baige_url(lang=lang) for lang in ygo.YGO_LANGUAGES] + [deck_url(), deck_image()])
        self.assertEqual(manifest['cards'][0]['source'], 'ygoprodeck')
        self.assertTrue(core.complete_image(manifest['cards'][0]['files'][0]['file']))

    def test_deck_card_identity_mismatch_fails_without_image_or_success_cache(self):
        self.routes.pop((baige_url(), 'GET'))
        self.enable_fallback(data=deck_data('124'))
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'error')
        self.assertNotIn(deck_image(), self.urls())
        self.assertEqual(self.success_cache(), {})

    def test_deck_image_identity_mismatch_fails_without_image_or_success_cache(self):
        self.routes.pop((baige_url(), 'GET'))
        self.enable_fallback(data=deck_data(image_id='124'))
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'error')
        self.assertNotIn(deck_image('124'), self.urls())
        self.assertEqual(self.success_cache(), {})

    def test_deck_full_image_host_path_and_scheme_guard(self):
        for url in ('http://images.ygoprodeck.com/images/cards/123.jpg',
                    'https://evil.test/images/cards/123.jpg',
                    'https://images.ygoprodeck.com/images/cards_small/123.jpg'):
            with self.subTest(url=url):
                client = ygo.YGOClient(str(self.api), core, force=True)
                data = deck_data()
                data['data'][0]['card_images'][0]['image_url'] = url
                self.enable_fallback(data=data)
                with self.assertRaises(ygo.YGOError):
                    client.deck_card('123')
                self.assertNotIn(url, self.urls())

    def test_metadata_failure_does_not_skip_baige_images(self):
        self.routes[(ygo.YGO_BAIGE_API + '123', 'GET')] = ygo.YGOError('offline API failed')
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.image_urls(), [baige_url()])
        self.assertEqual(manifest['cards'][0]['attempts'][0]['stage'], 'metadata')
        self.assertEqual(list((self.api / 'yugioh').glob('*.json')), [])

    def test_empty_and_corrupt_webp_fall_back_to_jp(self):
        for blob in (b'', b'RIFF\x20\x00\x00\x00WEBPgarbage', self.webp[:20]):
            with self.subTest(blob=blob):
                self.calls.clear()
                self.routes[(baige_url(), 'GET')] = blob
                self.good_card(lang='jp')
                code, manifest, _ = self.run_task(flags=['--force'])
                self.assertEqual(code, 0)
                self.assertEqual(self.image_urls(), [baige_url(), baige_url(lang='jp')])
                self.assertEqual(manifest['cards'][0]['lang_code'], 'jp')
                self.assertEqual(list(self.pics.glob('*.part')), [])

    def test_png_output_crc_and_full_decode(self):
        blob = ygo.YGOClient.png_bytes(self.webp)
        path = self.base / 'verified.png'
        path.write_bytes(blob)
        self.assertTrue(core.complete_image(str(path)))
        pos = 8
        while pos < len(blob):
            length = struct.unpack('>I', blob[pos:pos + 4])[0]
            end = pos + 12 + length
            self.assertEqual(zlib.crc32(blob[pos + 4:end - 4]) & 0xffffffff, struct.unpack('>I', blob[end - 4:end])[0])
            pos = end
        broken = bytearray(blob)
        broken[-1] ^= 1
        path.write_bytes(broken)
        self.assertFalse(core.complete_image(str(path)))
        with self.assertRaises(Exception):
            ygo.YGOClient.png_bytes(blob[:40])

    def test_complete_cache_second_run_zero_requests(self):
        self.run_task()
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(manifest['cards'][0]['cache_hit'])

    def test_corrupt_cached_image_repairs_exact_saved_url_no_api(self):
        self.routes.pop((baige_url(), 'GET'))
        self.good_card(lang='jp')
        _, manifest, _ = self.run_task()
        Path(manifest['cards'][0]['files'][0]['file']).write_bytes(b'corrupt')
        self.good_card()  # a new sc option must not override successful jp repair
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.urls(), [baige_url(lang='jp')])
        self.assertEqual(manifest['cards'][0]['lang_code'], 'jp')

    def test_failed_cache_repair_restarts_language_order(self):
        self.routes.pop((baige_url(), 'GET'))
        self.good_card(lang='jp')
        _, manifest, _ = self.run_task()
        Path(manifest['cards'][0]['files'][0]['file']).write_bytes(b'corrupt')
        self.routes[(baige_url(lang='jp'), 'GET')] = ygo.YGOError('repair failed')
        self.good_card()
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.image_urls(), [baige_url(lang='jp'), baige_url()])
        self.assertEqual(manifest['cards'][0]['lang_code'], 'sc')
        self.assertEqual(manifest['cards'][0]['attempts'][0]['stage'], 'cache-repair')

    def test_force_requeries_metadata_and_redownloads(self):
        self.run_task()
        self.calls.clear()
        code, _, _ = self.run_task(flags=['--force'])
        self.assertEqual(code, 0)
        self.assertEqual(self.urls(), [ygo.YGO_BAIGE_API + '123', baige_url()])

    def test_dry_run_head_no_download_no_success_cache(self):
        code, manifest, pdf = self.run_task(flags=['--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[-1], (baige_url(), 'baige-image', 'HEAD'))
        self.assertFalse(any(source.endswith('image') and method == 'GET' for _, source, method in self.calls))
        self.assertEqual(list(self.pics.glob('*.png')), [])
        self.assertEqual(self.success_cache(), {})
        self.assertEqual(manifest['cards'][0]['status'], 'dry-run')
        pdf.assert_not_called()
        self.calls.clear()
        self.run_task()
        self.assertEqual(self.urls(), [baige_url()])

    def test_dry_run_bad_head_type_falls_back_without_image_get(self):
        self.routes[(baige_url(), 'HEAD')] = 'text/html'
        self.good_card(lang='jp')
        code, manifest, _ = self.run_task(flags=['--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['lang_code'], 'jp')
        self.assertEqual(self.image_urls(), [baige_url(), baige_url(lang='jp')])
        self.assertTrue(all(method == 'HEAD' for _, source, method in self.calls if source.endswith('image')))

    def test_partial_output_pdf_quantity_and_failure_manifest(self):
        code, manifest, pdf = self.run_task('123\n00000123\n456\n123\n')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['summary'], {'processed': 2, 'ok': 1, 'failed': 1})
        self.assertEqual(len(pdf.call_args.args[0]), 3)
        self.assertEqual(manifest['cards'][1]['status'], 'error')
        self.assertEqual(set(self.success_cache()), {'123'})
        self.assertIn('456', (self.out / 'manifest.txt').read_text(encoding='utf-8'))

    def test_pdf_count_mismatch_returns_failure(self):
        code, manifest, pdf = self.run_task('123\n123', pdf_count=1)
        self.assertEqual(code, 1)
        self.assertEqual(len(pdf.call_args.args[0]), 2)

    def test_all_sources_fail_no_success_cache_and_next_run_retries(self):
        self.routes.clear()
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(self.success_cache(), {})
        pdf.assert_not_called()
        self.calls.clear()
        self.good_card()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.urls(), [ygo.YGO_BAIGE_API + '123', baige_url()])
        self.assertNotIn('error', manifest['cards'][0])

    def test_repair_changed_png_size_updates_bytes_index(self):
        _, manifest, _ = self.run_task()
        old_bytes = manifest['cards'][0]['files'][0]['bytes']
        path = Path(manifest['cards'][0]['files'][0]['file'])
        path.write_bytes(b'corrupt')
        stream = io.BytesIO()
        Image.new('RGB', (27, 35), (201, 72, 13)).save(stream, format='WEBP')
        self.routes[(baige_url(), 'GET')] = stream.getvalue()
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        new_bytes = manifest['cards'][0]['files'][0]['bytes']
        self.assertNotEqual(new_bytes, old_bytes)
        self.assertEqual(self.urls(), [baige_url()])
        self.calls.clear()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(manifest['cards'][0]['files'][0]['bytes'], new_bytes)

    def test_repair_and_all_fallbacks_fail_then_retry_not_false_cache_hit(self):
        _, manifest, _ = self.run_task()
        Path(manifest['cards'][0]['files'][0]['file']).write_bytes(b'corrupt')
        self.routes.clear()
        code, manifest, pdf = self.run_task()
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'error')
        pdf.assert_not_called()
        self.calls.clear()
        self.good_card()
        code, manifest, _ = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.image_urls(), [baige_url()])
        self.assertNotIn('error', manifest['cards'][0])

    def test_force_requeries_both_apis_for_deck_fallback(self):
        self.routes.pop((baige_url(), 'GET'))
        self.enable_fallback()
        self.run_task()
        self.calls.clear()
        code, _, _ = self.run_task(flags=['--force'])
        self.assertEqual(code, 0)
        self.assertIn(ygo.YGO_BAIGE_API + '123', self.urls())
        self.assertIn(deck_url(), self.urls())
        self.assertEqual(self.image_urls(), [baige_url(lang=lang) for lang in ygo.YGO_LANGUAGES] + [deck_image()])

    def test_no_pdf_flag_suppresses_pdf_but_keeps_output(self):
        code, manifest, pdf = self.run_task(flags=['--no-pdf'])
        self.assertEqual(code, 0)
        self.assertTrue(core.complete_image(manifest['cards'][0]['files'][0]['file']))
        pdf.assert_not_called()

    def test_bad_deck_image_metadata_not_api_cached_and_recovery_requeries(self):
        bad_images = [None, [], {}, [None],
                      [{'id': 124, 'image_url': deck_image('124')}],
                      [{'id': 123, 'image_url': None}],
                      [{'id': 123, 'image_url': 'https://evil.test/images/cards/123.jpg'}],
                      [{'id': 123, 'image_url': deck_image() + '?not-exact=1'}]]
        for index, images in enumerate(bad_images):
            with self.subTest(images=images):
                cache_dir = self.base / ('deck-cache-%d' % index)
                client = ygo.YGOClient(str(cache_dir), core)
                data = deck_data()
                data['data'][0]['card_images'] = images
                self.routes[(deck_url(), 'GET')] = json.dumps(data).encode()
                self.calls.clear()
                with self.assertRaises(ygo.YGOError):
                    client.deck_card('123')
                self.assertEqual(self.urls(), [deck_url()])
                self.assertEqual(list((cache_dir / 'yugioh').glob('*.json')), [])
                self.routes[(deck_url(), 'GET')] = json.dumps(deck_data()).encode()
                card, url = client.deck_card('123')
                self.assertEqual((card['id'], url), (123, deck_image()))
                self.assertEqual(self.urls(), [deck_url(), deck_url()])
                self.assertEqual(len(list((cache_dir / 'yugioh').glob('*.json'))), 1)
                self.calls.clear()
                self.assertEqual(client.deck_card('123')[1], deck_image())
                self.assertEqual(self.calls, [])

    def test_bad_deck_metadata_cached_by_old_version_is_revalidated(self):
        client = ygo.YGOClient(str(self.api), core)
        invalid = deck_data(image_id='124')
        # Seed an old cache through the previous card-id-only validator.
        self.routes[(deck_url(), 'GET')] = json.dumps(invalid).encode()
        client.json_get(deck_url(), 'deck-api', lambda data: data['data'][0]['id'] == 123)
        self.calls.clear()
        self.routes[(deck_url(), 'GET')] = json.dumps(deck_data()).encode()
        self.assertEqual(client.deck_card('123')[1], deck_image())
        self.assertEqual(self.urls(), [deck_url()])

    def test_invalid_api_json_identity_not_written_to_api_cache(self):
        client = ygo.YGOClient(str(self.api), core)
        for blob in (b'not JSON', b'{}', b'{"id": 124, "text": {}}', b'[]'):
            with self.subTest(blob=blob):
                self.routes[(ygo.YGO_BAIGE_API + '123', 'GET')] = blob
                with self.assertRaises(ygo.YGOError):
                    client.baige_card('123')
                self.assertEqual(list((self.api / 'yugioh').glob('*.json')), [])


class YGORequestBudgetTests(unittest.TestCase):
    """Exercise actual request method; never use real urllib opener."""
    def test_403_429_stop_only_rejecting_source_and_preserve_retry_after(self):
        for source in ('baige-api', 'baige-image'):
            for code in (403, 429):
                with self.subTest(source=source, code=code), tempfile.TemporaryDirectory() as tmp:
                    client = ygo.YGOClient(tmp, core)
                    headers = Message()
                    headers['Retry-After'] = '37'
                    error = urllib.error.HTTPError('https://offline.test', code, 'rejected', headers, io.BytesIO())
                    with patch.object(core, 'opener') as opener, patch.object(ygo.time, 'monotonic', return_value=100.0):
                        opener.return_value.open.side_effect = [error, io.BytesIO(b'deck API'), io.BytesIO(b'deck image')]
                        with self.assertRaises(ygo.YGOError):
                            client.request('https://offline.test', source)
                        with self.assertRaises(ygo.YGOError):
                            client.request('https://offline.test/again', source)
                        self.assertEqual(opener.return_value.open.call_count, 1)
                        self.assertEqual(client.budgets[source].cooldown, 137.0)
                        self.assertEqual(client.request('https://offline.test/deck', 'deck-api'), b'deck API')
                        self.assertEqual(client.request('https://offline.test/image', 'deck-image'), b'deck image')
                        other_baige = 'baige-image' if source == 'baige-api' else 'baige-api'
                        self.assertIsNone(client.budgets[other_baige].stopped)
                        self.assertIsNone(client.budgets['deck-api'].stopped)
                        self.assertIsNone(client.budgets['deck-image'].stopped)

    def test_rejected_baige_image_still_selects_deck_fallback(self):
        for code in (403, 429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                client = ygo.YGOClient(tmp, core)
                for budget in client.budgets.values():
                    budget.interval = 0
                calls = []
                def open_offline(req, **kwargs):
                    calls.append(req.full_url)
                    if req.full_url == ygo.YGO_BAIGE_API + '123':
                        return io.BytesIO(b'{"id":123,"text":{}}')
                    if req.full_url == baige_url():
                        headers = Message()
                        headers['Retry-After'] = '37'
                        raise urllib.error.HTTPError(req.full_url, code, 'blocked', headers, io.BytesIO())
                    if req.full_url == deck_url():
                        return io.BytesIO(json.dumps(deck_data()).encode())
                    if req.full_url == deck_image():
                        return io.BytesIO(image_bytes('JPEG'))
                    raise AssertionError('unexpected offline URL: ' + req.full_url)
                with patch.object(core, 'opener') as opener:
                    opener.return_value.open.side_effect = open_offline
                    entry = client.select('123', str(Path(tmp) / '123.png'))
                self.assertEqual(entry['status'], 'ok')
                self.assertEqual(entry['source'], 'ygoprodeck')
                self.assertEqual(calls, [ygo.YGO_BAIGE_API + '123', baige_url(), deck_url(), deck_image()])
                self.assertEqual([a['lang_code'] for a in entry['attempts']], ['sc', 'jp', 'en'])

    def test_head_reads_headers_not_body(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'opener') as opener:
            client = ygo.YGOClient(tmp, core)
            response = opener.return_value.open.return_value.__enter__.return_value
            response.headers = {'Content-Type': 'image/webp; charset=binary'}
            self.assertEqual(client.request('https://offline.test', 'baige-image', 'HEAD'), 'image/webp')
            response.read.assert_not_called()
            self.assertEqual(opener.return_value.open.call_args.args[0].get_method(), 'HEAD')


if __name__ == '__main__':
    unittest.main(verbosity=2)
