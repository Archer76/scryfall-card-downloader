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


def card(code='m11', number='146', lang='en', **extra):
    value = dict(id=code + number + lang, oracle_id='bolt', name='Lightning Bolt',
                 set=code, collector_number=number, lang=lang, type_line='Instant',
                 set_type='core', image_status='full', highres_image=False,
                 image_uris={'png': 'https://example.test/%s/%s/%s.png' % (code, number, lang)})
    value.update(extra)
    return value


class ExactPrintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.objects = {}
        self.calls = []
        self.downloads = []
        self.listing = []
        self.previous_cache = app._api_disk_cache_dir
        self.previous_refresh = app._exact_print_refresh
        app._exact_print_refresh = False
        app._reset_network_state()
        app._print_cache.clear()

    def tearDown(self):
        app._api_disk_cache_dir = self.previous_cache
        app._exact_print_refresh = self.previous_refresh
        app._reset_network_state()
        self.temp.cleanup()

    def api(self, path, params=None, **kwargs):
        self.calls.append((path, params, kwargs))
        self.assertIs(kwargs.get('cache_missing'), False)
        if path == '/cards/search':
            self.assertEqual(params['unique'], 'prints')
            self.assertEqual(params['include_multilingual'], 'true')
            return {'data': copy.deepcopy(self.listing)}
        result = self.objects.get(path)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def query(self, name='Lightning Bolt (M11) 146'):
        with patch.object(app, 'resolve_card', return_value=card()), patch.object(app, 'api_get', side_effect=self.api), patch.object(app, 'prints_for', side_effect=AssertionError('cross-set search')), patch.object(app, 'select_print', side_effect=AssertionError('automatic selection')):
            return app.query_card(name, ['zhs', 'zht', 'en'])

    def test_parse_normalization_and_card_name_numbers(self):
        self.assertEqual(app.parse_decklist('4 Lightning Bolt\n1 Lightning Bolt M11 146\n2 Lightning Bolt (m11) 146'), [(4, 'Lightning Bolt'), (1, 'Lightning Bolt (M11) 146'), (2, 'Lightning Bolt (M11) 146')])
        for name in ('Circle of Protection: Red', 'Circle of Protection: Red 123', 'Card 123', 'Card (M11)'):
            self.assertIsNone(app.parse_print_spec(name))
        self.assertEqual(app.parse_print_spec('Card (10e) 57★'), ('Card', '10e', '57★'))

    def test_lowres_small_basic_and_actual_language(self):
        self.objects['/cards/m11/146/zhs'] = card(lang='zhs', image_status='lowres', type_line='Basic Land', image_uris={'small': 'https://example.test/small.jpg'})
        result = self.query()
        self.assertEqual(result['used_lang'], 'zhs')
        self.assertFalse(result['selected']['highres_image'])
        self.assertEqual(len(self.calls), 1)

    def test_fictional_all_same_rank(self):
        for winner in app.FICTIONAL_LANGS:
            self.calls.clear()
            self.objects = {'/cards/m11/146/' + lang: card(lang=lang, highres_image=(lang == winner)) for lang in app.FICTIONAL_LANGS}
            self.assertEqual(self.query()['selected']['lang'], winner)
            self.assertEqual(len(self.calls), 7)

    def test_real_languages_quality_and_strict_identity(self):
        self.listing = [card(lang='ja'), card(lang='fr', highres_image=True), card('m10', lang='de', highres_image=True), card(number='147', lang='ru', highres_image=True)]
        self.assertEqual(self.query()['selected']['lang'], 'fr')
        self.assertIn('set:m11 cn:"146" lang:any', self.calls[-1][1]['q'])

    def test_missing_placeholder_mismatch_and_no_cross_version(self):
        self.objects['/cards/m11/146/zhs'] = card(lang='zhs', image_status='placeholder')
        self.objects['/cards/m11/146/zht'] = card(lang='zht', image_status='missing')
        self.objects['/cards/m11/146/en'] = card(oracle_id='wrong')
        self.listing = [card('m10'), card(number='147'), card(oracle_id='wrong')]
        self.assertIsNone(self.query()['selected'])

    def test_missing_dfc_face_does_not_suppress_english(self):
        self.objects['/cards/m11/146/zhs'] = card(lang='zhs', image_uris=None, card_faces=[{'name': 'Front', 'image_uris': {'png': 'front'}}, {'name': 'Back'}])
        self.objects['/cards/m11/146/en'] = card()
        self.assertEqual(self.query()['used_lang'], 'en')

    def test_clean_404_next_query_recovers_and_network_errors_propagate(self):
        self.assertIsNone(self.query()['selected'])
        self.objects['/cards/m11/146/en'] = card()
        self.assertEqual(self.query()['selected']['set'], 'm11')
        self.objects['/cards/m11/146/zhs'] = RuntimeError('429 exhausted')
        with self.assertRaisesRegex(RuntimeError, '429'):
            self.query()

    def test_api_404_not_cached_and_force_refreshes_positive(self):
        import urllib.error
        old_cache = app._api_disk_cache_dir
        app._api_disk_cache_dir = str(self.root / 'metadata')
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps(card()).encode('utf-8')
        from unittest.mock import Mock
        handler = Mock()
        handler.open.side_effect = [urllib.error.HTTPError('url', 404, 'missing', {}, None), Response(), Response()]
        try:
            with patch.object(app, 'opener', return_value=handler), patch.object(app, '_throttle'):
                self.assertIsNone(app.api_get('/cards/m11/146/en', cache_missing=False))
                self.assertEqual(list((self.root / 'metadata').glob('*.json')), [])
                self.assertEqual(app.api_get('/cards/m11/146/en', cache_missing=False)['set'], 'm11')
                app.api_get('/cards/m11/146/en', cache_missing=False)
                self.assertEqual(handler.open.call_count, 2)
                app.api_get('/cards/m11/146/en', cache_missing=False, refresh=True)
                self.assertEqual(handler.open.call_count, 3)
        finally:
            app._api_disk_cache_dir = old_cache

    def test_path_quotes_collector_and_validation(self):
        self.query('Lightning Bolt (10e) 57★')
        self.assertEqual(self.calls[0][0], '/cards/10e/57%E2%98%85/zhs')

    def run_main(self, deck, extra=()):
        path = self.root / 'deck.txt'
        path.write_text(deck, encoding='utf-8')
        def download(url, dest):
            self.downloads.append(url)
            Path(dest).write_bytes(png())
            return len(png())
        with patch.object(app, 'resolve_card', return_value=card()), patch.object(app, 'api_get', side_effect=self.api), patch.object(app, 'select_print', return_value=(card('m10'), 'en')), patch.object(app, 'resolve_display_names', side_effect=lambda o, s: (app.face_names(s), s['set'])), patch.object(app, 'download', side_effect=download), patch.object(app, 'make_pdf', return_value=None) as pdf, patch.object(app, 'program_cache_directory', return_value=str(self.root / 'api-cache')), contextlib.redirect_stdout(io.StringIO()):
            rc = app.main([str(path), '--out', str(self.root / 'out'), '--pics', str(self.root / 'pics')] + list(extra))
        return rc, json.loads((self.root / 'out' / 'manifest.json').read_text(encoding='utf-8')), pdf

    def test_mixed_versions_auto_cache_isolation_force_repair_pdf_token(self):
        self.objects['/cards/m11/146/en'] = card()
        self.objects['/cards/m12/150/en'] = card('m12', '150')
        deck = '4 Lightning Bolt\n2 Lightning Bolt M11 146\n3 Lightning Bolt (M12) 150\n1 Lightning Bolt (m11) 146\n'
        rc, manifest, pdf = self.run_main(deck)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.downloads), 3)
        self.assertEqual(len(pdf.call_args.args[0]), 10)
        self.assertEqual([c['set_code'] for c in manifest['cards']], ['m10', 'm11', 'm12', 'm11'])
        self.assertTrue(all(c['token_check']['status'] == 'none' for c in manifest['cards']))
        cache = app.PictureCache(str(self.root / 'pics'))
        self.assertEqual(len(cache.data['cards']), 3)
        self.assertEqual(cache.lookup('Lightning Bolt')['set_code'], 'm10')
        self.assertEqual(cache.lookup('Lightning Bolt M11 146')['set_code'], 'm11')
        self.assertIsNone(cache.lookup('Lightning Bolt M13 1'))
        self.calls.clear()
        self.downloads.clear()
        self.run_main(deck)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        Path(manifest['cards'][1]['files'][0]['file']).write_bytes(b'broken')
        self.run_main(deck)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.calls, [])
        self.downloads.clear()
        self.run_main(deck, ['--force'])
        self.assertEqual(len(self.downloads), 3)
        self.assertTrue(self.calls)

    def test_exact_first_does_not_become_automatic_filename_cache(self):
        self.objects['/cards/m11/146/en'] = card()
        self.run_main('1 Lightning Bolt M11 146')
        cache = app.PictureCache(str(self.root / 'pics'))
        self.assertIsNone(cache.lookup('Lightning Bolt'))
        _, manifest, _ = self.run_main('1 Lightning Bolt')
        self.assertEqual(manifest['cards'][0]['set_code'], 'm10')
        self.assertEqual(len(self.downloads), 2)

    def test_dry_run_small_jpeg_no_download_no_pdf(self):
        self.objects['/cards/m11/146/en'] = card(image_uris={'small': 'https://example.test/a.jpg'})
        rc, manifest, pdf = self.run_main('2 Lightning Bolt M11 146', ['--dry-run'])
        self.assertEqual(rc, 0)
        self.assertEqual(self.downloads, [])
        pdf.assert_not_called()
        entry = manifest['cards'][0]
        self.assertEqual(entry['files'][0]['format'], 'jpg')
        self.assertEqual(entry['printing_spec']['name'], 'Lightning Bolt')
        self.assertEqual(entry['collector_number'], '146')


if __name__ == '__main__':
    unittest.main()
