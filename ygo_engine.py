"""Yu-Gi-Oh YDK engine: Baige sc/jp/en, then YGOPRODeck English.

WebP/JPEG inputs are fully decoded with Pillow and stored as RGB PNG so the
existing cache validation and PDF engine can be reused. Network budgets are
source-local: a rejected Baige service must not disable the fallback host.
"""
import concurrent.futures
import copy
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

YGO_BAIGE_API = 'https://ygocdb.com/api/v0/card/'
YGO_BAIGE_IMAGES = 'https://cdn.233.momobako.com/ygoimg'
YGO_DECK_API = 'https://db.ygoprodeck.com/api/v7/cardinfo.php'
YGO_LANGUAGES = ('sc', 'jp', 'en')
YGO_LANGUAGE_NAMES = {'sc': '简体中文', 'jp': '日文', 'en': '英文'}


def parse_ygo_decklist(text):
    """YDK passwords only; section markers are ignored, duplicates count copies.

    Passwords may omit leading zeroes. Nine-digit provisional simulator IDs,
    zero, Unicode digits and quantity/name rows are not card passwords.
    """
    counts = {}
    for raw in text.lstrip('\ufeff').splitlines():
        line = raw.strip()
        if re.fullmatch(r'[0-9]{1,8}', line) and int(line) > 0:
            card_id = str(int(line))
            counts[card_id] = counts.get(card_id, 0) + 1
    return [(qty, card_id) for card_id, qty in counts.items()]


class YGOError(RuntimeError):
    pass


class YGOBudget:
    """Shared admission clock, cooldown and stop flag for ONE endpoint group."""
    def __init__(self, interval):
        self.interval = interval
        self.next_call = 0.0
        self.cooldown = 0.0
        self.stopped = None
        self.lock = threading.Lock()

    def admit(self):
        while True:
            with self.lock:
                if self.stopped:
                    raise YGOError(self.stopped)
                now = time.monotonic()
                delay = max(self.next_call, self.cooldown) - now
                if delay <= 0:
                    self.next_call = now + self.interval
                    return
            time.sleep(min(delay, 0.25))

    def reject(self, code, delay):
        with self.lock:
            self.cooldown = max(self.cooldown, time.monotonic() + delay)
            if code in (403, 429):
                # No repeated requests to a rejecting host during this task.
                # Another source is independent and may still be used.
                self.stopped = 'HTTP %d：该来源已停止本次后续请求' % code


class YGOClient:
    def __init__(self, cache_dir, core, force=False, image_interval=0):
        self.core = core
        self.force = force
        self.cache_dir = os.path.join(cache_dir, 'yugioh')
        # Conservative client policies, NOT claimed server limits.
        self.budgets = {
            'baige-api': YGOBudget(0.1),
            'deck-api': YGOBudget(0.1),  # <=10 starts/sec, official max20/sec
            'baige-image': YGOBudget(max(0.15, image_interval)),
            'deck-image': YGOBudget(max(0.15, image_interval)),
        }

    def request(self, url, source, method='GET'):
        budget = self.budgets[source]
        last = None
        for attempt in range(3):
            budget.admit()
            req = urllib.request.Request(url, method=method, headers={
                'User-Agent': self.core.UA, 'Accept': '*/*'})
            try:
                with self.core.opener().open(req, timeout=30 if source.endswith('api') or method == 'HEAD' else 120) as response:
                    if method == 'HEAD':
                        return response.headers.get('Content-Type', '').split(';')[0].lower()
                    return response.read()
            except urllib.error.HTTPError as exc:
                code = exc.code
                delay = self.core.retry_after_seconds(exc.headers)
                exc.close()
                if code in (403, 429):
                    budget.reject(code, max(delay, 1))
                    raise YGOError('HTTP %d：%s' % (code, url))
                if code in (404, 400, 410):
                    raise YGOError('HTTP %d：%s' % (code, url))
                if 500 <= code < 600:
                    last = 'HTTP %d' % code
                    budget.reject(code, max(delay, attempt + 1))
                    continue
                raise YGOError('HTTP %d：%s' % (code, url))
            except (OSError, TimeoutError) as exc:
                last = str(exc)
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise YGOError('请求失败 %s | %s' % (url, last))

    def json_get(self, url, source, validator):
        filename = os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + '.json')
        if not self.force:
            try:
                with open(filename, encoding='utf-8') as stream:
                    saved = json.load(stream)
                if 0 <= time.time() - saved['saved_at'] < self.core.API_CACHE_TTL and validator(saved['data']):
                    return saved['data']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            data = json.loads(self.request(url, source).decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise YGOError('API 返回非有效 JSON：%s' % exc) from exc
        if not validator(data):
            raise YGOError('API 返回卡片身份不一致或缺少有效资料：%s' % url)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.core.atomic_json(filename, {'saved_at': time.time(), 'data': data})
        return data

    def baige_card(self, card_id):
        return self.json_get(YGO_BAIGE_API + card_id, 'baige-api',
            lambda d: isinstance(d, dict) and str(d.get('id')) == card_id and isinstance(d.get('text'), dict))

    def deck_card(self, card_id):
        def valid(data):
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                return False
            if len(data['data']) != 1 or not isinstance(data['data'][0], dict) or str(data['data'][0].get('id')) != card_id:
                return False
            images = data['data'][0].get('card_images')
            if not isinstance(images, list):
                return False
            expected = 'https://images.ygoprodeck.com/images/cards/%s.jpg' % card_id
            return any(isinstance(image, dict) and str(image.get('id')) == card_id and image.get('image_url') == expected for image in images)
        data = self.json_get(YGO_DECK_API + '?' + urllib.parse.urlencode({'id': card_id}), 'deck-api', valid)
        card = data['data'][0]
        for image in card.get('card_images', []):
            if isinstance(image, dict) and str(image.get('id')) == card_id:
                url = image.get('image_url')
                if isinstance(url, str):
                    parsed = urllib.parse.urlparse(url)
                    if parsed.scheme == 'https' and parsed.hostname == 'images.ygoprodeck.com' and parsed.path == '/images/cards/%s.jpg' % card_id:
                        return card, url
        raise YGOError('YGOPRODeck 没有与卡片密码一致的完整卡图')

    @staticmethod
    def png_bytes(blob):
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as image:
            if image.format not in ('WEBP', 'JPEG', 'PNG'):
                raise YGOError('不是受支持的卡图格式')
            if image.width <= 0 or image.height <= 0 or image.width * image.height > 40000000:
                raise YGOError('卡图尺寸无效或过大')
            image.load()  # decode the ENTIRE source; reject truncated/corrupt images
            output = io.BytesIO()
            image.convert('RGB').save(output, format='PNG')
            return output.getvalue()

    def fetch_image(self, url, source, path, dry_run=False):
        if dry_run:
            kind = self.request(url, source, method='HEAD')
            if kind not in ('image/webp', 'image/jpeg', 'image/png'):
                raise YGOError('图片端点未返回有效图片类型：%s' % kind)
            return None
        data = self.png_bytes(self.request(url, source))
        directory = os.path.dirname(path)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, suffix='.part', delete=False) as stream:
                temp = stream.name
                stream.write(data)
            if not self.core.complete_image(temp):
                raise YGOError('转换后的 PNG 完整性校验失败')
            os.replace(temp, path)
            temp = None
        finally:
            if temp is not None:
                os.unlink(temp)
        return len(data)

    def select(self, card_id, path, dry_run=False, previous=None):
        attempts = []
        metadata = None
        # A saved image URL is authoritative for repair, not a new language pick.
        # Only permit our exact known card endpoints; never load arbitrary index URLs.
        if previous and previous.get('files'):
            record = previous['files'][0]
            source = previous.get('source')
            language = previous.get('lang_code')
            url = record.get('url')
            expected = '%s/%s/%s.webp' % (YGO_BAIGE_IMAGES, language, card_id)
            valid = source == 'baige' and language in YGO_LANGUAGES and url == expected
            valid |= source == 'ygoprodeck' and language == 'en' and url == 'https://images.ygoprodeck.com/images/cards/%s.jpg' % card_id
            if valid:
                try:
                    size = self.fetch_image(url, 'deck-image' if source == 'ygoprodeck' else 'baige-image', path, dry_run)
                    result = copy.deepcopy(previous)
                    result.update(status='dry-run' if dry_run else 'ok', attempts=attempts)
                    result.pop('error', None)
                    result['files'] = [self.record(path, url, size, dry_run)]
                    return result
                except Exception as exc:
                    attempts.append({'source': source, 'lang_code': language, 'stage': 'cache-repair', 'error': str(exc)})
        try:
            metadata = self.baige_card(card_id)
        except Exception as exc:
            attempts.append({'source': 'baige', 'stage': 'metadata', 'error': str(exc)})
        # Query metadata failure MUST NOT skip otherwise downloadable Baige images.
        for language in YGO_LANGUAGES:
            url = '%s/%s/%s.webp' % (YGO_BAIGE_IMAGES, language, card_id)
            try:
                size = self.fetch_image(url, 'baige-image', path, dry_run)
                text = metadata.get('text', {}) if metadata else {}
                name = text.get({'sc': 'sc_name', 'jp': 'jp_name', 'en': 'en_name'}[language]) or text.get('name') or card_id
                return self.entry(card_id, name, language, 'baige', path, url, size, dry_run, attempts)
            except Exception as exc:
                attempts.append({'source': 'baige', 'lang_code': language, 'stage': 'image', 'error': str(exc)})
        try:
            card, url = self.deck_card(card_id)
            size = self.fetch_image(url, 'deck-image', path, dry_run)
            return self.entry(card_id, card.get('name') or card_id, 'en', 'ygoprodeck', path, url, size, dry_run, attempts)
        except Exception as exc:
            attempts.append({'source': 'ygoprodeck', 'lang_code': 'en', 'error': str(exc)})
        return {'game': 'ygo', 'input': card_id, 'selected_id': card_id, 'display_name': card_id,
                'files': [], 'status': 'error', 'error': '百鸽三语种及 YGOPRODeck 均失败', 'attempts': attempts}

    @staticmethod
    def record(path, url, size, dry_run):
        result = {'file': path, 'url': url, 'format': 'png', 'label': None,
                  'status': 'dry-run' if dry_run else 'ok'}
        if size is not None:
            result['bytes'] = size
        return result

    def entry(self, card_id, name, language, source, path, url, size, dry_run, attempts):
        return {'game': 'ygo', 'input': card_id, 'selected_id': card_id,
                'display_name': name, 'lang_code': language, 'lang': YGO_LANGUAGE_NAMES[language],
                'source': source, 'files': [self.record(path, url, size, dry_run)],
                'status': 'dry-run' if dry_run else 'ok', 'attempts': attempts}


class YGOPictureCache:
    """Password-keyed independent game cache; no name or foreign-game lookup."""
    def __init__(self, directory, core):
        self.core = core
        self.directory = os.path.abspath(directory)
        self.paths = core.PictureCache(self.directory)
        self.filename = os.path.join(self.directory, 'ygo-index.json')
        self.cards = {}
        try:
            with open(self.filename, encoding='utf-8') as stream:
                stored = json.load(stream)
            if stored.get('version') == 1 and stored.get('game') == 'ygo' and isinstance(stored.get('cards'), dict):
                self.cards = stored['cards']
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def lookup(self, card_id):
        entry = self.cards.get(card_id)
        if not entry or entry.get('game') != 'ygo' or str(entry.get('selected_id')) != card_id:
            return None
        return self.paths.restore(entry)

    def store(self, entry):
        if entry.get('status') not in ('ok', 'exists') or not entry.get('files'):
            return  # dry-run and failed choices never become success cache
        stored = copy.deepcopy(entry)
        stored.pop('qty', None)
        stored.pop('cache_hit', None)
        for record in stored['files']:
            filename = os.path.basename(record['file'])
            self.paths.remember_image(filename, record.get('url'), record.get('bytes'))
            record['file'] = filename
        self.cards[str(entry['selected_id'])] = stored

    def save(self):
        self.paths.save()
        self.core.atomic_json(self.filename, {'version': 1, 'game': 'ygo', 'cards': self.cards})


def run_ygo(args, text, core):
    rows = parse_ygo_decklist(text)
    if not rows:
        print('游戏王牌表为空：每行填写一个卡片密码，其他行自动跳过。')
        return 2
    from PIL import features
    if not features.check('webp'):
        raise YGOError('Pillow 缺少 WebP 解码支持，请重新安装官方 Pillow 或使用完整 EXE。')
    if not args.pics:
        args.pics = os.path.join(os.path.dirname(core.program_cache_directory()), '卡图输出', 'yugioh-pics')
    os.makedirs(args.out, exist_ok=True)
    cache = YGOPictureCache(args.pics, core)
    client = YGOClient(core.program_cache_directory(), core, force=args.force, image_interval=args.image_interval)
    entries = {}
    pending = []
    for qty, card_id in rows:
        saved = None if args.force else cache.lookup(card_id)
        if saved and all(record['status'] == 'exists' for record in saved['files']):
            saved.update(status='dry-run' if args.dry_run else 'exists', cache_hit=True)
            saved.pop('error', None)
            if args.dry_run:
                for record in saved['files']:
                    record['status'] = 'dry-run'
            entries[card_id] = saved
        else:
            pending.append((card_id, saved))
    print('游戏王 | 百鸽 简中→日文→英文 → YGOPRODeck英文 | 缓存 %d/%d' % (len(entries), len(rows)), flush=True)
    # Each worker selects AND downloads one unique card. Never start the fallback
    # before all higher-priority Baige image candidates have failed validation.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, args.download_workers), thread_name_prefix='ygo-card') as pool:
        jobs = {pool.submit(client.select, card_id, cache.paths.image_path(card_id + '.png'), args.dry_run, saved): card_id
                for card_id, saved in pending}
        for done, future in enumerate(concurrent.futures.as_completed(jobs), 1):
            card_id = jobs[future]
            try:
                entry = future.result()
            except Exception as exc:
                entry = {'game': 'ygo', 'input': card_id, 'selected_id': card_id, 'status': 'error', 'error': str(exc), 'files': []}
            entries[card_id] = entry
            cache.store(entry)
            cache.save()  # completed cards survive interrupted tasks
            print('[游戏王 %d/%d] %s | %s | %s | %s' % (done, len(pending), card_id, entry.get('lang', ''), entry.get('source', ''), entry['status']), flush=True)
    results = []
    pdf_paths = []
    for qty, card_id in rows:
        entry = entries[card_id]
        entry['qty'] = qty
        if not args.dry_run and entry['status'] in ('ok', 'exists'):
            if all(core.complete_image(r['file'], r.get('bytes')) for r in entry['files']):
                pdf_paths.extend([r['file'] for r in entry['files']] * qty)
            else:
                entry.update(status='error', error='PNG 完整性校验失败')
        results.append(entry)
    cache.save()
    ok = sum(e['status'] in ('ok', 'exists', 'dry-run') for e in results)
    failed = len(results) - ok
    manifest = {'game': 'ygo', 'language_order': list(YGO_LANGUAGES), 'source_order': ['baige', 'ygoprodeck'],
                'out_dir': cache.directory, 'cards': results, 'tokens': [],
                'summary': {'processed': len(results), 'ok': ok, 'failed': failed}}
    core.atomic_json(os.path.join(args.out, 'manifest.json'), manifest)
    with open(os.path.join(args.out, 'manifest.txt'), 'w', encoding='utf-8') as stream:
        for entry in results:
            stream.write('%d %s | %s | %s | %s%s\n' % (entry['qty'], entry['input'], entry.get('lang', ''), entry.get('source', ''), entry['status'], ' | ' + entry['error'] if entry.get('error') else ''))
            for attempt in entry.get('attempts', []):
                stream.write('    %s\n' % json.dumps(attempt, ensure_ascii=False))
    print('完成：成功 %d / 失败 %d' % (ok, failed), flush=True)
    if pdf_paths and not args.no_pdf and not args.dry_run:
        info = core.make_pdf(pdf_paths, args.paper, os.path.join(args.out, 'cards.pdf'),
                             cut_marks=not args.no_cut_marks, card_size_mm=(59, 86))
        if not info or info['cards'] != len(pdf_paths):
            print('部分图片未能进入 PDF', flush=True)
            return 1
    return 1 if failed else 0
