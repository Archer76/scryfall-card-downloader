import contextlib
import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import scryfall_cards_threaded as app


class QueryTests(unittest.TestCase):
    def setUp(self):
        with app._print_cache_lock:
            app._print_cache.clear()
        app._reset_network_state()
        app._api_disk_cache_dir = None

    def test_parallel_query_uses_workers_and_deduplicates_names(self):
        stats = {'active': 0, 'peak': 0, 'calls': []}
        lock = threading.Lock()
        def resolve(name):
            with lock:
                stats['active'] += 1
                stats['peak'] = max(stats['peak'], stats['active'])
                stats['calls'].append(name)
            try:
                time.sleep(0.10 if name == 'Slow' else 0.02)
                if name == 'Bad': raise RuntimeError('query failure')
                if name == 'Missing': return None
                return {'oracle_id': name, 'name': name, 'type_line': 'Creature'}
            finally:
                with lock: stats['active'] -= 1
        def select(oracle, langs, basic):
            return {'name': oracle, 'image_uris': {'png': oracle}, 'lang': 'en'}, 'en'
        rows = [(2, 'Slow'), (1, 'Fast'), (3, 'Slow'), (1, 'Bad'), (1, 'Missing')]
        with patch.object(app, 'resolve_card', resolve), patch.object(app, 'select_print', select), patch.object(app, 'resolve_display_names', lambda oracle, selected: ([oracle], 'tst')), contextlib.redirect_stdout(io.StringIO()):
            results = app.query_all(rows, ['en'], 32)
        self.assertGreater(stats['peak'], 1)
        self.assertEqual(stats['calls'].count('Slow'), 1)
        self.assertEqual(len(results), 4)
        self.assertEqual(results['Slow']['display'], ['Slow'])
        self.assertEqual(results['Bad']['error'], 'query failure')
        self.assertIsNone(results['Missing']['resolved'])

    def test_singleflight_cache_for_shared_prints(self):
        calls = []
        def search(query, max_pages):
            calls.append(query)
            time.sleep(0.05)
            return []
        with patch.object(app, 'search_prints', search), ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: app.prints_for('same-oracle', 'lang:zhs'), range(16)))
            self.assertEqual(app.prints_for('same-oracle', 'lang:zhs'), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [[]] * 16)

    def test_failed_cache_does_not_poison_retry(self):
        with patch.object(app, 'search_prints', side_effect=[RuntimeError('temporary'), [{'name': 'OK'}]]) as search:
            with self.assertRaisesRegex(RuntimeError, 'temporary'):
                app.prints_for('same', 'lang:en')
            self.assertEqual(app.prints_for('same', 'lang:en'), [{'name': 'OK'}])
            self.assertEqual(search.call_count, 2)

    def test_real_http_requests_overlap_but_stay_rate_limited(self):
        stats = {'active': 0, 'peak': 0, 'starts': []}
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                with lock:
                    stats['active'] += 1
                    stats['peak'] = max(stats['peak'], stats['active'])
                    stats['starts'].append(time.monotonic())
                time.sleep(0.20)
                body = json.dumps({'ok': True}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                with lock: stats['active'] -= 1
        admitted = []
        admission_lock = threading.Lock()
        real_throttle = app._throttle
        def observed_throttle():
            # 服务端线程调度会改变到达间隔；在真实节流器准入处测量策略。
            with admission_lock:
                real_throttle()
                admitted.append(app._last_call[0])
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with patch.object(app, 'API_BASE', 'http://127.0.0.1:%d' % server.server_port), patch.object(app, 'MIN_API_INTERVAL', 0.06), patch.object(app, '_throttle', observed_throttle), patch.object(app, '_query_log'), ThreadPoolExecutor(max_workers=32) as pool:
                results = list(pool.map(lambda n: app.api_get('/query', {'n': n}), range(6)))
            self.assertEqual(results, [{'ok': True}] * 6)
            self.assertGreater(stats['peak'], 1, 'API responses must overlap')
            starts = sorted(admitted)
            self.assertEqual(len(starts), 6)
            self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.059)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

    def test_global_api_cooldown(self):
        start = time.monotonic()
        app._pause_api(0.12)
        def admitted(_):
            app._throttle()
            return time.monotonic()
        with patch.object(app, 'MIN_API_INTERVAL', 0.05), ThreadPoolExecutor(max_workers=8) as pool:
            starts = sorted(pool.map(admitted, range(4)))
        self.assertGreaterEqual(starts[0] - start, 0.11)
        self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.04)

    def test_blocked_api_retries_pause_all_threads(self):
        for code in (429, 503):
            headers = Message()
            headers['Retry-After'] = '7'
            error = urllib.error.HTTPError('https://example.test', code, 'blocked', headers, io.BytesIO())
            response = io.BytesIO(b'{"ok": true}')
            with patch.object(app, 'opener') as opener, patch.object(app, '_throttle'), patch.object(app, '_pause_api') as pause, patch.object(app, '_query_log'):
                opener.return_value.open.side_effect = [error, response]
                self.assertEqual(app.api_get('/test', retries=2), {'ok': True})
                pause.assert_called_once_with(30.0 if code == 429 else 15.0, slow_down=(code == 429))
                self.assertEqual(opener.return_value.open.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
