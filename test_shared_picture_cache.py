import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib
import scryfall_cards_threaded as app


def png(red=255):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes([0, red, 0, 0]))) + chunk(b'IEND', b'')


def entry(directory, english='Lightning Bolt', chinese='闪电击', url='https://example.test/bolt.png'):
    return {'input': english, 'qty': 1, 'english_name': english, 'display_name': chinese,
            'lang': '简体中文', 'lang_code': 'zhs', 'set_code': 'tst', 'set_name': 'Test',
            'collector_number': '1', 'chinese_name': True, 'status': 'ok',
            'files': [{'file': str(directory / (chinese + '.png')), 'format': 'png', 'label': None,
                       'url': url, 'status': 'ok', 'bytes': len(png())}]}


class SharedPictureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.pics = self.base / '卡图输出' / 'pics'
        self.pics.mkdir(parents=True)
        app._reset_network_state()

    def tearDown(self): self.temp.cleanup()

    def test_english_chinese_and_case_aliases_reuse_relative_index(self):
        (self.pics / '闪电击.png').write_bytes(png())
        cache = app.PictureCache(str(self.pics))
        cache.store(entry(self.pics))
        cache.save()
        raw = json.loads((self.pics / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['cards']['lightning bolt']['files'][0]['file'], '闪电击.png')
        reloaded = app.PictureCache(str(self.pics))
        for name in ('Lightning Bolt', '  LIGHTNING   BOLT ', '闪电击'):
            hit = reloaded.lookup(name)
            self.assertEqual(hit['files'][0]['status'], 'exists')
            self.assertEqual(Path(hit['files'][0]['file']), self.pics / '闪电击.png')

    def test_all_cached_skips_query_api_and_download_and_preserves_pdf_quantity(self):
        (self.pics / '闪电击.png').write_bytes(png())
        cache = app.PictureCache(str(self.pics))
        cache.store(entry(self.pics))
        cache.save()
        deck = self.base / 'deck.txt'
        deck.write_text('2 Lightning Bolt\n3 闪电击\n', encoding='utf-8')
        output = self.base / '卡图输出' / '261012-130000'
        with patch.object(app, 'query_all', side_effect=AssertionError('must not query')) as query, patch.object(app, 'api_get', side_effect=AssertionError('must not access API')) as api, patch.object(app, 'download', side_effect=AssertionError('must not download')) as download, patch.object(app, 'make_pdf', return_value={}) as pdf, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main([str(deck), '--out', str(output), '--pics', str(self.pics), '--langs', 'en']), 0)
        query.assert_not_called()
        api.assert_not_called()
        download.assert_not_called()
        self.assertEqual(pdf.call_args.args[0], [str(self.pics / '闪电击.png')] * 5)
        self.assertFalse((output / 'cards').exists())
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['out_dir'], str(self.pics))
        self.assertEqual(manifest['cards'][1]['qty'], 3)
        self.assertTrue(all(card['cache_hit'] for card in manifest['cards']))

    def test_missing_and_corrupt_cached_files_download_without_query(self):
        cache = app.PictureCache(str(self.pics))
        cached = entry(self.pics)
        cache.store(cached)
        cache.save()
        deck = self.base / 'deck.txt'
        deck.write_text('1 闪电击\n', encoding='utf-8')
        calls = []
        def download(url, dest):
            calls.append(url)
            Path(dest).write_bytes(png())
            return len(png())
        for n in range(2):
            if n: (self.pics / '闪电击.png').write_bytes(png()[:-5])
            out = self.base / '卡图输出' / ('261012-13000%d' % n)
            with patch.object(app, 'query_all', side_effect=AssertionError('must not requery')), patch.object(app, 'download', download), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main([str(deck), '--out', str(out), '--pics', str(self.pics), '--no-pdf']), 0)
            self.assertTrue(app.complete_image(str(self.pics / '闪电击.png')))
        self.assertEqual(calls, [cached['files'][0]['url']] * 2)

    def test_double_faces_restore_missing_back_only(self):
        cache = app.PictureCache(str(self.pics))
        cached = entry(self.pics, 'Front // Back', '正面-背面')
        cached['aliases'] = ['中文正面', '中文背面']
        cached['files'] = [{'label': label, 'file': str(self.pics / ('双面_' + label + '.png')), 'format': 'png', 'url': 'https://example.test/' + label, 'status': 'ok', 'bytes': len(png())} for label in ('正面', '背面')]
        Path(cached['files'][0]['file']).write_bytes(png())
        cache.store(cached)
        cache.save()
        hit = cache.lookup('中文正面')
        self.assertEqual([record['status'] for record in hit['files']], ['exists', 'pending'])
        self.assertEqual(cache.lookup('Front')['display_name'], '正面-背面')
        self.assertEqual(cache.lookup('Front//Back')['display_name'], '正面-背面')

    def test_migration_deduplicates_equal_images_keeps_colliding_versions_and_pdfs(self):
        for stamp, color in [('261012-120000', 255), ('261012-120001', 255), ('261012-120002', 128)]:
            task = self.pics.parent / stamp
            cards = task / 'cards'
            cards.mkdir(parents=True)
            (cards / '闪电击.png').write_bytes(png(color))
            (task / 'cards.pdf').write_bytes(b'keep PDF')
            card = entry(cards, url='https://example.test/' + stamp)
            card['files'][0]['bytes'] = len(png(color))
            (task / 'manifest.json').write_text(json.dumps({'cards': [card], 'out_dir': str(cards)}, ensure_ascii=False), encoding='utf-8')
            (task / 'manifest.txt').write_text('    闪电击.png\n', encoding='utf-8')
        cache = app.PictureCache(str(self.pics))
        with contextlib.redirect_stdout(io.StringIO()): stats = cache.migrate_legacy()
        self.assertEqual(stats['moved'], 3)
        images = list(self.pics.glob('*.png'))
        self.assertEqual(len(images), 2)
        self.assertEqual({image.read_bytes() for image in images}, {png(), png(128)})
        for task in self.pics.parent.glob('261012-*'):
            self.assertFalse((task / 'cards').exists())
            self.assertEqual((task / 'cards.pdf').read_bytes(), b'keep PDF')
            manifest = json.loads((task / 'manifest.json').read_text(encoding='utf-8'))
            record = manifest['cards'][0]['files'][0]
            self.assertEqual(Path(record['file']).parent, self.pics)
            self.assertIn(Path(record['file']).name, (task / 'manifest.txt').read_text(encoding='utf-8'))
        selected = cache.lookup('Lightning Bolt')
        self.assertEqual(Path(selected['files'][0]['file']).read_bytes(), png(128))
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(app.PictureCache(str(self.pics)).migrate_legacy()['moved'], 0)

    def test_failed_migration_commit_keeps_original_and_can_retry(self):
        task = self.pics.parent / '261012-120000'
        cards = task / 'cards'
        cards.mkdir(parents=True)
        (cards / '闪电击.png').write_bytes(png())
        manifest = task / 'manifest.json'
        manifest.write_text(json.dumps({'cards': [entry(cards)]}, ensure_ascii=False), encoding='utf-8')
        real_atomic = app.atomic_json
        def fail_manifest(path, data):
            if Path(path) == manifest: raise OSError('simulated commit failure')
            return real_atomic(path, data)
        with patch.object(app, 'atomic_json', side_effect=fail_manifest), contextlib.redirect_stdout(io.StringIO()):
            app.PictureCache(str(self.pics)).migrate_legacy()
        self.assertEqual((cards / '闪电击.png').read_bytes(), png())
        with contextlib.redirect_stdout(io.StringIO()): app.PictureCache(str(self.pics)).migrate_legacy()
        self.assertFalse(cards.exists())
        self.assertIsNotNone(app.PictureCache(str(self.pics)).lookup('Lightning Bolt'))

    def test_raw_picture_and_alias_collision_and_unsafe_path(self):
        (self.pics / '自制卡.png').write_bytes(png())
        cache = app.PictureCache(str(self.pics))
        self.assertEqual(cache.lookup('自制卡')['files'][0]['status'], 'exists')
        first = entry(self.pics, 'First', '共享名', 'a')
        second = entry(self.pics, 'Second', '共享名', 'b')
        cache.store(first)
        cache.store(second)
        self.assertIsNone(cache.lookup('共享名'))
        first_name = cache.allocate('重复文件名', 'png', 'https://example.test/a')
        second_name = cache.allocate('重复文件名', 'png', 'https://example.test/b')
        self.assertNotEqual(first_name, second_name)
        self.assertEqual(cache.allocate('另外名字', 'png', 'https://example.test/a?newstamp'), first_name)
        with self.assertRaises(ValueError): cache.image_path('../outside.png')
        with self.assertRaises(ValueError): cache.image_path('..\\outside.png')

    def test_force_requeries_and_redownloads_ignoring_image_cache(self):
        (self.pics / '闪电击.png').write_bytes(png())
        cache = app.PictureCache(str(self.pics))
        cache.store(entry(self.pics))
        cache.save()
        deck = self.base / 'deck.txt'
        deck.write_text('1 Lightning Bolt\n', encoding='utf-8')
        out = self.base / '卡图输出' / '261012-130000'
        selected = {'name': 'Lightning Bolt', 'lang': 'zhs', 'set': 'new', 'image_uris': {'png': 'https://example.test/new.png'}}
        def write(url, dest):
            Path(dest).write_bytes(png(128))
            return len(png(128))
        with patch.object(app, 'resolve_card', return_value={'oracle_id': 'bolt', 'name': 'Lightning Bolt', 'type_line': 'Instant'}) as resolve, patch.object(app, 'select_print', return_value=(selected, '简体中文')), patch.object(app, 'resolve_display_names', return_value=(['闪电击'], 'new')), patch.object(app, 'download', side_effect=write) as download, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main([str(deck), '--out', str(out), '--pics', str(self.pics), '--force', '--no-pdf']), 0)
        resolve.assert_called_once()
        download.assert_called_once()
        hit = app.PictureCache(str(self.pics)).lookup('闪电击')
        self.assertEqual(Path(hit['files'][0]['file']).read_bytes(), png(128))
        self.assertEqual(hit['set_code'], 'new')

    def test_invalid_png_crc_is_not_cache_hit(self):
        damaged = bytearray(png())
        damaged[40] ^= 1
        path = self.pics / '损坏.png'
        path.write_bytes(damaged)
        self.assertFalse(app.complete_image(str(path)))
        self.assertIsNone(app.PictureCache(str(self.pics)).lookup('损坏'))


if __name__ == '__main__': unittest.main(verbosity=2)
