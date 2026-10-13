"""Bounded TCGdex concurrency benchmark; no retries or local response cache.
Stops increasing concurrency on any failed request. Uses the downloader's urllib
system/environment proxy behavior and verified TLS. Images are CRC-validated,
then discarded. Per-request evidence and summaries are persisted after each tier.
"""
import argparse
import collections
import concurrent.futures
import datetime
import json
from pathlib import Path
import random
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BASE = 'https://api.tcgdex.net/v2/zh-tw'
OUT = Path(__file__).resolve().parent / 'tcgdex-benchmark-results'
LOCAL = threading.local()
TIMEOUT = 25.0


def opener():
    if not hasattr(LOCAL, 'opener'):
        LOCAL.opener = urllib.request.build_opener()
    return LOCAL.opener


def save(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def png_valid(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Not PNG')
    at = 8
    width = height = None
    while at + 12 <= len(data):
        size = struct.unpack('>I', data[at:at+4])[0]
        kind = data[at+4:at+8]
        end = at + 8 + size
        if end + 4 > len(data):
            raise ValueError('Truncated PNG')
        expected = struct.unpack('>I', data[end:end+4])[0]
        if (zlib.crc32(data[at+4:end]) & 0xffffffff) != expected:
            raise ValueError('PNG CRC mismatch')
        if kind == b'IHDR':
            width, height = struct.unpack('>II', data[at+8:at+16])
        if kind == b'IEND':
            if (width, height) != (600, 825):
                raise ValueError('Unexpected image dimensions: %s' % ((width, height),))
            return
        at = end + 4
    raise ValueError('Missing IEND')


def get(url, image=False):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'CardDownloader-TCGdexBenchmark/1.0 (bounded personal integration test)',
        'Accept': 'image/png' if image else 'application/json',
    })
    with opener().open(request, timeout=TIMEOUT) as response:
        body = response.read()
        meta = {key: response.headers.get(key) for key in
                ['Content-Type', 'Age', 'X-Cache', 'CF-Cache-Status', 'Retry-After']
                if response.headers.get(key) is not None}
        code = response.status
    if image:
        png_valid(body)
        payload = None
    else:
        payload = json.loads(body.decode('utf-8'))
        if not isinstance(payload, (dict, list)) or isinstance(payload, dict) and 'error' in payload:
            raise ValueError('Invalid API data')
    return payload, len(body), code, meta


def prepare():
    all_cards = {}
    sets = []
    for set_id in ['SV2a', 'SV4a', 'S12a', 'SV8a', 'SC1a', 'SV6']:
        start = time.monotonic()
        data, size, code, headers = get(BASE + '/sets/' + set_id)
        cards = data.get('cards', [])
        sets.append({'id': set_id, 'cards': len(cards), 'with_image': sum(bool(c.get('image')) for c in cards)})
        all_cards.update({c['id']: c for c in cards})
        print('PREPARE', set_id, sets[-1], 'seconds', round(time.monotonic()-start, 2), flush=True)
    cards = list(all_cards.values())
    random.Random(76).shuffle(cards)
    image_cards = [card for card in cards if card.get('image')]
    save('samples.json', {'date': datetime.datetime.now().astimezone().isoformat(),
                          'sets': sets, 'cards': cards, 'image_cards': image_cards})
    # Verify one real high-quality Traditional Chinese image before load tests.
    if not image_cards:
        raise RuntimeError('No Traditional Chinese images in sampled sets')
    card = image_cards[0]
    _, size, code, _ = get(card['image'] + '/high.png', image=True)
    print('PREPARED', len(cards), 'cards;', len(image_cards), 'image links;',
          'verified image', card['id'], size, 'bytes', flush=True)


def run(kind, tiers, count, tag='', same_sample=False):
    sample = json.loads((OUT / 'samples.json').read_text(encoding='utf-8'))
    cards = sample['image_cards'] if kind == 'image' else sample['cards']
    if kind == 'search':
        cards = list({card['name']: card for card in cards}.values())
    records = []
    abort = threading.Event()
    active_lock = threading.Lock()
    active = 0
    peak = 0

    def request_one(card):
        nonlocal active, peak
        if abort.is_set():
            return {'id': card['id'], 'skipped': True}
        if kind == 'image':
            url = card['image'] + '/high.png'
        elif kind == 'search':
            url = BASE + '/cards?' + urllib.parse.urlencode({'name': 'eq:' + card['name']})
        else:
            url = BASE + '/cards/' + urllib.parse.quote(card['id'])
        with active_lock:
            active += 1
            peak = max(active, peak)
        start = time.monotonic()
        item = {'id': card['id'], 'url': url}
        try:
            data, size, code, headers = get(url, image=kind == 'image')
            if kind == 'detail' and data.get('id') != card['id']:
                raise ValueError('Wrong card ID')
            if kind == 'search' and not any(c.get('id') == card['id'] for c in data):
                raise ValueError('Search did not return expected card')
            item.update(ok=True, status=code, bytes=size, headers=headers)
        except urllib.error.HTTPError as exc:
            item.update(ok=False, status=exc.code, retry_after=exc.headers.get('Retry-After'),
                        error=str(exc))
            abort.set()
            exc.close()
        except Exception as exc:
            item.update(ok=False, error=type(exc).__name__ + ': ' + str(exc))
            abort.set()
        finally:
            with active_lock:
                active -= 1
            item['seconds'] = round(time.monotonic()-start, 4)
        return item

    for index, concurrency in enumerate(tiers):
        abort.clear()
        active = peak = 0
        # A fixed 64 requests per normal tier; >=64 gets two waves.
        n = max(count, concurrency * 2)
        if len(cards) < n:
            if kind == 'image':
                n = len(cards)  # never invent URLs for absent images
            else:
                raise RuntimeError('Not enough distinct query samples')
        offset = 0 if same_sample else (index * count) % len(cards)
        selected = [cards[(offset+i) % len(cards)] for i in range(n)]
        print('BEGIN', kind, 'concurrency', concurrency, 'requests', n, flush=True)
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(request_one, selected))
        elapsed = time.monotonic()-start
        attempted = [row for row in results if not row.get('skipped')]
        successful = [row for row in attempted if row['ok']]
        failed = [row for row in attempted if not row['ok']]
        durations = sorted(row['seconds'] for row in attempted)
        record = {'kind': kind, 'concurrency': concurrency, 'socket_timeout_seconds': TIMEOUT,
                  'same_sample_across_tiers': same_sample, 'planned': n,
                  'attempted': len(attempted), 'successful': len(successful),
                  'failed': len(failed), 'skipped': n-len(attempted),
                  'peak_active': peak, 'seconds': round(elapsed, 3),
                  'requests_per_second': round(len(successful)/elapsed, 3),
                  'megabytes': round(sum(row.get('bytes', 0) for row in successful)/1e6, 3),
                  'p50_seconds': durations[len(durations)//2] if durations else None,
                  'p95_seconds': durations[min(len(durations)-1, int(len(durations)*.95))] if durations else None,
                  'status_counts': dict(collections.Counter(str(row.get('status', 'non_http_error')) for row in attempted)),
                  'errors': failed, 'requests': results}
        records.append(record)
        save(kind + tag + '-results.json', {'date': datetime.datetime.now().astimezone().isoformat(),
                                    'method': 'urllib system/environment proxy, TLS verification on, no retries, no application cache, no fixed request interval',
                                    'records': records})
        print('RESULT', json.dumps({k:v for k,v in record.items() if k not in ('requests',)}, ensure_ascii=False), flush=True)
        if failed:
            print('STOP: failed request; no higher tier or automatic retry for this endpoint.', flush=True)
            break
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'detail', 'search', 'image'])
    parser.add_argument('--tiers', default='2,4,8,16,32,64')
    parser.add_argument('--count', type=int, default=64)
    parser.add_argument('--tag', default='')
    parser.add_argument('--timeout', type=float, default=25.0)
    parser.add_argument('--same-sample', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    TIMEOUT = args.timeout
    if args.mode == 'prepare':
        prepare()
    else:
        run(args.mode, [int(n) for n in args.tiers.split(',')], args.count, args.tag, args.same_sample)
