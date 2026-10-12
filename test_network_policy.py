import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import scryfall_cards_threaded as app


class Clock:
    def __init__(self): self.value = 100.0
    def monotonic(self): return self.value
    def time(self): return 200000.0 + self.value
    def sleep(self, seconds): self.value += max(seconds, 0.000001)


def error(code, retry=None):
    headers = Message()
    if retry is not None: headers['Retry-After'] = retry
    return urllib.error.HTTPError('https://example.test', code, 'blocked', headers, io.BytesIO())


class NetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        app._reset_network_state()
        app._api_disk_cache_dir = None

    def test_retry_after_seconds_dates_and_no_truncation(self):
        for raw, expected in [('7', 7), ('120', 120), ('0', 0), ('-2', 0), ('bad', 0), ('nan', 0), ('inf', 0)]:
            headers = Message()
            headers['Retry-After'] = raw
            self.assertEqual(app.retry_after_seconds(headers), expected)
        with patch.object(app.time, 'time', return_value=100000):
            headers = Message()
            headers['Retry-After'] = formatdate(100125, usegmt=True)
            self.assertEqual(app.retry_after_seconds(headers), 125)
            headers.replace_header('Retry-After', formatdate(99990, usegmt=True))
            self.assertEqual(app.retry_after_seconds(headers), 0)
        self.assertEqual(app.blocked_delay(429, 1, 7), 30)
        self.assertEqual(app.blocked_delay(429, 2, 120), 120)
        self.assertEqual(app.blocked_delay(429, 3), 120)

    def test_actual_default_api_spacing_allows_overlapping_responses(self):
        stats = {'starts': [], 'active': 0, 'peak': 0}
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                with lock:
                    stats['starts'].append(time.monotonic())
                    stats['active'] += 1
                    stats['peak'] = max(stats['peak'], stats['active'])
                time.sleep(0.8)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                with lock: stats['active'] -= 1
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(app.DEFAULT_WORKERS, 4)
            self.assertEqual(app.MIN_API_INTERVAL, 0.6)
            with patch.object(app, 'API_BASE', 'http://127.0.0.1:%d' % server.server_port), patch.object(app, '_query_log'), ThreadPoolExecutor(max_workers=4) as pool:
                result = list(pool.map(lambda n: app.api_get('/cards/named', {'n': n}), range(3)))
            self.assertEqual(result, [{'ok': True}] * 3)
            starts = sorted(stats['starts'])
            self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.55)
            self.assertGreater(stats['peak'], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_default_image_spacing_and_inner_retry_respects_cooldown(self):
        clock = Clock()
        attempts = []
        body = b'\x89PNG\r\n\x1a\nimage'
        def open_image(*args, **kwargs):
            attempts.append(clock.monotonic())
            if len(attempts) == 1:
                app._pause_images(10)
                raise urllib.error.URLError('temporary')
            return io.BytesIO(body)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, 'time', clock), patch.object(app, 'opener') as opener:
            opener.return_value.open.side_effect = open_image
            app.download('https://example.test/a.png', str(Path(temp) / 'a.png'))
            app.download('https://example.test/b.png', str(Path(temp) / 'b.png'))
        self.assertGreaterEqual(attempts[1] - attempts[0], 10)
        self.assertGreaterEqual(attempts[2] - attempts[1], 0.199)

    def test_403_circuit_breaker_finishes_queued_queries_without_retries(self):
        with patch.object(app, 'MIN_API_INTERVAL', 0.01), patch.object(app, '_query_log'), patch.object(app, 'opener') as opener, patch.object(app, 'resolve_card', side_effect=lambda name: app.api_get('/cards/named', {'fuzzy': name})):
            opener.return_value.open.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(error(403))
            results = app.query_all([(1, str(n)) for n in range(8)], ['en'], 4)
        self.assertEqual(len(results), 8)
        self.assertTrue(all('error' in result for result in results.values()))
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertIn('403', app._api_abort_reason[0])

    def test_429_protection_accumulates_across_different_cards(self):
        clock = Clock()
        responses = []
        for _ in range(4): responses += [error(429), io.BytesIO(b'{"ok": true}')]
        with patch.object(app, 'time', clock), patch.object(app, '_query_log'), patch.object(app, 'opener') as opener:
            opener.return_value.open.side_effect = responses
            for n in range(3):
                self.assertEqual(app.api_get('/cards/named', {'n': n}, retries=2), {'ok': True})
            with self.assertRaisesRegex(RuntimeError, '4 次'):
                app.api_get('/cards/named', {'n': 3}, retries=2)
            count = opener.return_value.open.call_count
            with self.assertRaises(RuntimeError):
                app.api_get('/cards/named', {'n': 99}, retries=2)
            self.assertEqual(opener.return_value.open.call_count, count)
        self.assertEqual(count, 7)
        self.assertEqual(app._api_rate_limit_events[0], 4)
        self.assertEqual(app._api_backoff_interval[0], 4.8)

    def test_success_and_404_cache_reuse_and_24_hour_expiry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, '_api_disk_cache_dir', temp), patch.object(app, '_throttle'), patch.object(app, '_query_log'), patch.object(app, 'opener') as opener:
            opener.return_value.open.side_effect = [io.BytesIO(b'{"name":"Cached"}'), error(404)]
            self.assertEqual(app.api_get('/cards/named', {'fuzzy': 'Cached'}), {'name': 'Cached'})
            self.assertEqual(app.api_get('/cards/named', {'fuzzy': 'Cached'}), {'name': 'Cached'})
            self.assertIsNone(app.api_get('/cards/named', {'fuzzy': 'Missing'}))
            self.assertIsNone(app.api_get('/cards/named', {'fuzzy': 'Missing'}))
            self.assertEqual(opener.return_value.open.call_count, 2)
            url = app.API_BASE + '/cards/named?fuzzy=Cached'
            cache = Path(app._api_cache_path(url))
            data = json.loads(cache.read_text(encoding='utf-8'))
            data['saved_at'] = time.time() - app.API_CACHE_TTL - 1
            cache.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(app._read_api_cache(url), (False, None))
            self.assertNotEqual(app._api_cache_path(url), app._api_cache_path(url + '&lang=zhs'))

    def test_corrupt_cache_and_atomic_concurrent_writes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, '_api_disk_cache_dir', temp), patch.object(app, '_query_log'):
            url = app.API_BASE + '/cards/search?q=test'
            path = Path(app._api_cache_path(url))
            path.write_text('broken', encoding='utf-8')
            self.assertEqual(app._read_api_cache(url), (False, None))
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(lambda n: app._write_api_cache(url, {'n': n}), range(16)))
            hit, data = app._read_api_cache(url)
            self.assertTrue(hit)
            self.assertIn(data['n'], range(16))
            self.assertEqual(list(Path(temp).glob('*.part')), [])
            with patch.object(app.os, 'replace', side_effect=OSError('unwritable')):
                app._write_api_cache(url, {'n': 100})
            self.assertEqual(list(Path(temp).glob('*.part')), [])

    def test_no_transient_error_or_invalid_json_cached(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, '_api_disk_cache_dir', temp), patch.object(app, '_throttle'), patch.object(app, '_pause_api'), patch.object(app, '_query_log'), patch.object(app, 'opener') as opener:
            for status in (403, 429, 503):
                app._reset_network_state()
                opener.return_value.open.side_effect = error(status)
                with self.assertRaises(RuntimeError):
                    app.api_get('/test%d' % status, retries=1)
            app._reset_network_state()
            opener.return_value.open.side_effect = None
            opener.return_value.open.return_value = io.BytesIO(b'not JSON')
            with self.assertRaises(RuntimeError): app.api_get('/invalid', retries=1)
            self.assertEqual(list(Path(temp).glob('*.json')), [])

    def test_second_window_cannot_overlap_and_lock_releases(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.object(app, 'program_cache_directory', return_value=temp):
            with app.network_task_lock():
                with self.assertRaisesRegex(RuntimeError, '另一个窗口'):
                    with app.network_task_lock(): pass
            with app.network_task_lock(): pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
