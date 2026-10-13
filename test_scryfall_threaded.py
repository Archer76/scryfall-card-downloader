import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import Future
import struct
import zlib
import scryfall_cards_threaded as app


def png():
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')

class ImmediatePool:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def submit(self, func, *args):
        f = Future()
        try: f.set_result(func(*args))
        except Exception as exc: f.set_exception(exc)
        return f

class Clock:
    def __init__(self): self.value = 0
    def monotonic(self): return self.value
    def sleep(self, seconds): self.value += seconds

class Tests(unittest.TestCase):
    def setUp(self):
        app._reset_network_state()
        app._api_disk_cache_dir = None

    def test_real_parallel_http_and_dedup(self):
        lock = threading.Lock()
        stats = {'active': 0, 'peak': 0, 'count': 0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                with lock:
                    stats['active'] += 1
                    stats['count'] += 1
                    stats['peak'] = max(stats['peak'], stats['active'])
                time.sleep(0.08)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(png())
                with lock: stats['active'] -= 1
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
                url = 'http://127.0.0.1:%d/card' % server.server_port
                records = [{'file': str(Path(temp) / ('%d.png' % n)), 'url': url, 'status': 'pending'} for n in range(4)]
                records.append(dict(records[0]))
                with patch.object(app, 'MIN_IMAGE_INTERVAL', 0.02), contextlib.redirect_stdout(io.StringIO()):
                    app.download_all([{'files': records}], 32)
                self.assertEqual(stats['count'], 4)
                self.assertGreater(stats['peak'], 1)
                self.assertTrue(all(r['status'] == 'ok' for r in records))
                self.assertFalse(list(Path(temp).glob('*.part')))
                self.assertEqual(app.decode_png_rgb(records[0]['file']), (1, 1, b'\xff\x00\x00'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_blocked_download_response(self):
        import urllib.error
        from email.message import Message
        headers = Message()
        headers['Retry-After'] = '7'
        for code in (403, 429, 503):
            err = urllib.error.HTTPError('https://example.test', code, 'blocked', headers, io.BytesIO())
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, '_throttle_images'), patch.object(app, '_pause_images'), patch.object(app, 'opener') as op:
                op.return_value.open.side_effect = err
                with self.assertRaises(app.DownloadBlocked) as ctx:
                    app.download('https://example.test', str(Path(temp) / 'x.png'))
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(ctx.exception.retry_after, {403: 60, 429: 30, 503: 15}[code])

    def test_adaptive_retry_and_floor(self):
        record = {'file': 'fake.png', 'url': 'fake', 'status': 'pending'}
        calls = []
        def blocked(*args):
            calls.append(args)
            raise app.DownloadBlocked(429)
        output = io.StringIO()
        with patch.object(app, 'ThreadPoolExecutor', ImmediatePool), patch.object(app, 'time', Clock()), patch.object(app, 'download', blocked), contextlib.redirect_stdout(output):
            app.download_all([{'files': [record]}], 32)
        self.assertEqual(len(calls), 5)
        self.assertEqual(record['status'], 'error')
        for transition in ('32 -> 16', '16 -> 8', '8 -> 4', '4 -> 2', '2 -> 1'):
            self.assertIn(transition, output.getvalue())

    def test_block_then_success(self):
        record = {'file': 'fake.png', 'url': 'fake', 'status': 'pending'}
        with patch.object(app, 'ThreadPoolExecutor', ImmediatePool), patch.object(app, 'time', Clock()), patch.object(app, 'download', side_effect=[app.DownloadBlocked(403), 100]), contextlib.redirect_stdout(io.StringIO()):
            app.download_all([{'files': [record]}], 32)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['bytes'], 100)

    def test_main_order_duplicate_failure_and_existing(self):
        def resolved(name): return {'oracle_id': name, 'name': name, 'type_line': 'Creature'}
        def selected(oracle, langs, basic):
            card = {'lang': 'en', 'name': oracle, 'set': 'tst', 'collector_number': '1'}
            if oracle == 'Double':
                card['card_faces'] = [{'name': 'Front', 'image_uris': {'png': 'Front'}}, {'name': 'Back', 'image_uris': {'png': 'Back'}}]
            else: card['image_uris'] = {'png': oracle}
            return card, 'en'
        requests = []
        def download(url, dest):
            requests.append(url)
            if url == 'Bad': raise RuntimeError('test failure')
            time.sleep(0.03 if url == 'Slow' else 0.001)
            Path(dest).write_bytes(png())
            return len(png())
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            base = Path(temp)
            deck = base / 'deck.txt'
            deck.write_text('2 Slow\n1 Fast\n3 Slow\n1 Double\n1 Bad\n', encoding='utf-8')
            args = [str(deck), '--out', str(base / 'out'), '--pics', str(base / 'pics'), '--workers', '4', '--paper', 'A3', '--no-cut-marks']
            with patch.object(app, 'resolve_card', resolved), patch.object(app, 'select_print', selected), patch.object(app, 'resolve_display_names', lambda o, s: (app.face_names(s), 'tst')), patch.object(app, 'download', download), patch.object(app, 'make_pdf', return_value=None) as pdf, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(args), 1)
                paths = [Path(p).name for p in pdf.call_args.args[0]]
                self.assertEqual(paths, ['Slow.png'] * 2 + ['Fast.png'] + ['Slow.png'] * 3 + ['Front-Back_正面.png', 'Front-Back_背面.png'])
                self.assertFalse(pdf.call_args.kwargs['cut_marks'])
                self.assertNotIn('card_size_mm', pdf.call_args.kwargs)
                self.assertEqual(requests.count('Slow'), 1)
                manifest = json.loads((base / 'out' / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['summary'], {'processed': 5, 'ok': 4, 'failed': 1})
                requests.clear()
                self.assertEqual(app.main(args + ['--no-pdf']), 1)
                self.assertEqual(requests, ['Bad'])
                requests.clear()
                self.assertEqual(app.main(args + ['--dry-run']), 0)
                self.assertEqual(requests, [])

    def test_actual_pdf_output(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, contextlib.redirect_stdout(io.StringIO()):
            image = Path(temp) / 'card.png'
            image.write_bytes(png())
            for paper, count in [('A4', 9), ('A3', 18)]:
                dest = Path(temp) / (paper + '.pdf')
                result = app.make_pdf([str(image)] * (count + 1), paper, str(dest), cut_marks=False)
                self.assertEqual(result['pages'], 2)
                self.assertEqual(result['per_page'], count)
                self.assertTrue(dest.read_bytes().startswith(b'%PDF-1.4'))
                self.assertTrue(dest.read_bytes().endswith(b'%%EOF\n'))
                self.assertNotIn(b're S', dest.read_bytes())

if __name__ == '__main__':
    unittest.main(verbosity=2)
