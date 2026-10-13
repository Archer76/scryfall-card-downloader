"""PTCG exact-print engine. Embedded by build_gui.py; no cross-language matching."""
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PTCG_API_BASE = 'https://api.tcgdex.net/v2'
PTCG_WORKERS = 64
# Verified official set codes; PR-SV is an explicitly supported export alias.
PTCG_EN_SETS = {
    'DRI': 'sv10', 'PR-SV': 'svp', 'SVP': 'svp', 'SVE': 'sve',
    'ASC': 'me02.5', 'TWM': 'sv06', 'PAR': 'sv04', 'RCL': 'swsh2',
    'PAL': 'sv02', 'OBF': 'sv03', 'SUM': 'sm1', '30C': '30th',
    'CIN': 'sm4', 'SSH': 'swsh1', 'PBL': 'me05', 'PRE': 'sv08.5',
    'MEE': 'mee',
}
PTCG_ROW = re.compile(r'^([0-9]+)\s+(.+?)\s+([A-Za-z0-9][A-Za-z0-9.\-]*)\s+([A-Za-z]*[0-9]+[A-Za-z]?(?:/[A-Za-z]*[0-9]+)?)$')


def parse_ptcg_decklist(text):
    rows = []
    errors = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or not line[0].isdigit():
            continue
        match = PTCG_ROW.fullmatch(line)
        if not match or int(match.group(1)) <= 0:
            errors.append('第 %d 行：%s' % (number, line))
            continue
        qty, name, code, local_id = match.groups()
        rows.append((int(qty), name.strip(), code, local_id))
    if errors:
        raise ValueError('宝可梦牌表必须使用“数量 卡名 系列代码 编号”，例如 1 Shaymin DRI 10；不接受无编号牌表。\n' + '\n'.join(errors))
    return rows


def _ptcg_number(value):
    value = str(value)
    # Numerator-only exports such as 10/182 still specify local card number.
    value = value.split('/', 1)[0]
    return str(int(value)) if value.isascii() and value.isdigit() else value


def _ptcg_name_text(value, language=None, basic_energy=False):
    value = str(value or '')
    # Remove both kinds of parenthetical annotations, including nested pairs.
    while True:
        stripped = re.sub(r'\([^()]*\)|（[^（）]*）', ' ', value)
        if stripped == value:
            break
        value = stripped
    value = re.sub(r'\s+', ' ', value).strip().replace('’', "'")
    if language == 'en' and basic_energy:
        value = re.sub(r'^Basic\s+', '', value, flags=re.IGNORECASE)
    if language == 'zh-tw' and basic_energy:
        value = re.sub(r'^基本【([^】]+)】能量$', r'基本\1能量', value)
    return value


def _ptcg_name(value, language=None, basic_energy=False):
    return _ptcg_name_text(value, language, basic_energy).casefold()


def _ptcg_basic_energy(card, language, allow_known_mislabel=False):
    # Names alone cannot distinguish older special Darkness/Metal Energy.
    if card.get('category') != 'Energy':
        return False
    name = _ptcg_name(card.get('name'), language, True)
    if language == 'en':
        basic_names = {kind.casefold() + ' energy' for kind in (
            'Grass', 'Fire', 'Water', 'Lightning', 'Psychic', 'Fighting',
            'Darkness', 'Metal', 'Fairy')}
    else:
        basic_names = {name} if re.fullmatch(r'基本[^\s【】]+能量', name) else set()
    if name not in basic_names:
        return False
    if card.get('energyType') in ('Normal', 'Basic'):
        return True
    # These two official zh-tw records are known mislabels. This exception is
    # for missing-image source records only, never arbitrary Special candidates.
    return (allow_known_mislabel and language == 'zh-tw'
            and card.get('energyType') == 'Special'
            and card.get('id') in ('S8a-DAR', 'SV-P-DAR')
            and name == '基本惡能量')


class PTCGError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class PTCGClient:
    """Own API cooldown/cache, never alters Scryfall API timing or route."""
    def __init__(self, language, cache_dir, core):
        if language not in ('en', 'zh-tw'):
            raise ValueError('不支持的宝可梦语言')
        self.language = language
        self.cache_dir = os.path.join(cache_dir, 'tcgdex', language)
        self.core = core
        self.lock = threading.Lock()
        self.cooldown = 0.0
        self.abort = None
        self.limited = 0
        self.set_lock = threading.Lock()
        self.sets = {}

    def admit(self):
        while True:
            with self.lock:
                if self.abort:
                    raise PTCGError('request_error', self.abort)
                delay = self.cooldown - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 0.5))

    def get(self, path):
        url = PTCG_API_BASE + '/' + self.language + path
        filename = os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + '.json')
        try:
            with open(filename, encoding='utf-8') as fh:
                saved = json.load(fh)
            data = saved['data']
            if 0 <= time.time() - saved['saved_at'] < self.core.API_CACHE_TTL and isinstance(data, (dict, list)) and not (isinstance(data, dict) and 'error' in data):
                return data
        except (OSError, ValueError, TypeError, KeyError):
            pass
        last = None
        for attempt in range(4):
            self.admit()
            req = urllib.request.Request(url, headers={'User-Agent': self.core.UA, 'Accept': 'application/json'})
            try:
                with self.core.opener().open(req, timeout=30) as response:
                    data = json.loads(response.read().decode('utf-8'))
                if not isinstance(data, (dict, list)) or isinstance(data, dict) and 'error' in data:
                    raise ValueError('TCGdex 响应格式不正确')
                os.makedirs(self.cache_dir, exist_ok=True)
                self.core.atomic_json(filename, {'saved_at': time.time(), 'data': data})
                return data
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry = self.core.retry_after_seconds(exc.headers)
                exc.close()
                if code == 404:
                    return None  # no failure/404 cache; retry next task
                if code == 403:
                    with self.lock:
                        self.abort = 'TCGdex HTTP 403：已停止后续查询'
                    raise PTCGError('request_error', self.abort)
                if code == 429 or 500 <= code < 600:
                    last = 'HTTP %d' % code
                    delay = self.core.blocked_delay(code, attempt + 1, retry) if code in (429, 503) else max(retry, attempt + 1)
                    with self.lock:
                        now = time.monotonic()
                        if code == 429 and now >= self.cooldown:
                            self.limited += 1
                            if self.limited >= 4:
                                self.abort = 'TCGdex 多次限流：已停止后续查询'
                        self.cooldown = max(self.cooldown, now + delay)
                    continue
                raise PTCGError('request_error', 'TCGdex HTTP %d' % code)
            except PTCGError:
                raise
            except (OSError, ValueError) as exc:
                last = str(exc)
                if attempt < 3:
                    time.sleep(0.8 * (attempt + 1))
        raise PTCGError('request_error', 'TCGdex 查询失败：%s' % last)

    def set_for(self, code):
        # Singleflight by supplied set code; no per-card bulk directory traversal.
        with self.set_lock:
            if code in self.sets:
                return self.sets[code]
            set_id = PTCG_EN_SETS.get(code.upper(), code) if self.language == 'en' else code
            data = self.get('/sets/' + urllib.parse.quote(set_id, safe=''))
            if not data and self.language == 'en' and code.upper() not in PTCG_EN_SETS:
                ids = set()
                for field in ('abbreviation.official', 'tcgOnline'):
                    found = self.get('/sets?' + urllib.parse.urlencode({field: 'eq:' + code.upper()}))
                    if isinstance(found, list):
                        ids.update(s['id'] for s in found if isinstance(s, dict) and isinstance(s.get('id'), str))
                if len(ids) > 1:
                    raise PTCGError('ambiguous_set', '系列代码 %s 对应多个系列，需提供具体 set ID' % code)
                if len(ids) == 1:
                    set_id = next(iter(ids))
                    data = self.get('/sets/' + urllib.parse.quote(set_id, safe=''))
                    aliases = (data or {}).get('abbreviation', {})
                    official = aliases.get('official') if isinstance(aliases, dict) else None
                    if code.upper() not in (str(official).upper(), str((data or {}).get('tcgOnline')).upper()):
                        raise PTCGError('identity_mismatch', '返回系列不具有指定官方缩写：%s' % code)
            if not data:
                raise PTCGError('missing_set', '未找到 %s 系列 %s；请使用该语言卡库的系列代码或 TCGdex set ID' % (self.language, code))
            if not isinstance(data, dict) or data.get('id') != set_id or not isinstance(data.get('cards'), list):
                raise PTCGError('identity_mismatch', '系列身份不匹配：请求 %s，返回 %s' % (set_id, data.get('id') if isinstance(data, dict) else '非对象'))
            self.sets[code] = data
            return data

    def image_url(self, card):
        image = card.get('image')
        if not isinstance(image, str) or not image:
            return None
        parsed = urllib.parse.urlparse(image)
        if parsed.scheme != 'https' or parsed.hostname != 'assets.tcgdex.net' or parsed.path.split('/')[1:2] != [self.language]:
            raise PTCGError('invalid_image', 'TCGdex 图片地址无效或语言不一致')
        return image.rstrip('/') + '/high.png'

    def basic_energy_fallback(self, original):
        # TCGdex's default name filter is case-insensitive containment. This
        # includes Basic prefixes and parenthetical annotations; details below
        # must still match the full normalized name and Energy/Normal category.
        search_name = _ptcg_name_text(original['name'], self.language, True)
        search_names = [search_name]
        if self.language == 'zh-tw':
            decorated = re.sub(r'^基本(.+)能量$', r'基本【\1】能量', search_name)
            if decorated != search_name:
                search_names.append(decorated)
        briefs = []
        for search in search_names:
            found = self.get('/cards?' + urllib.parse.urlencode({'name': search}))
            if isinstance(found, list):
                briefs.extend(found)
        expected = _ptcg_name(original['name'], self.language, True)
        seen = {original['id']}
        invalid_image = None
        for brief in briefs:
            if not isinstance(brief, dict):
                continue
            card_id = brief.get('id')
            if not isinstance(card_id, str) or not card_id or card_id in seen:
                continue
            seen.add(card_id)
            if _ptcg_name(brief.get('name'), self.language, True) != expected:
                continue
            candidate = self.get('/cards/' + urllib.parse.quote(card_id, safe=''))
            if (not isinstance(candidate, dict) or candidate.get('id') != card_id
                    or not isinstance(candidate.get('set'), dict)
                    or not isinstance(candidate['set'].get('id'), str) or not candidate['set']['id']
                    or not isinstance(candidate.get('localId'), str) or not candidate['localId']
                    or ('localId' in brief and _ptcg_number(candidate['localId']) != _ptcg_number(brief['localId']))
                    or not _ptcg_basic_energy(candidate, self.language)
                    or _ptcg_name(candidate.get('name'), self.language, True) != expected):
                continue
            try:
                url = self.image_url(candidate)
            except PTCGError as exc:
                invalid_image = exc
                continue
            if url:
                candidate = copy.deepcopy(candidate)
                candidate['_ptcg_fallback'] = {
                    'reason': 'basic_energy_missing_image',
                    'requested_id': original['id'],
                    'requested_tcgdex_set_id': original['set']['id'],
                    'requested_tcgdex_collector_number': original['localId'],
                }
                return candidate, url
        if invalid_image:
            raise invalid_image
        raise PTCGError('missing_image', '基础能量 %s 缺图，未找到同语言同名基础能量图片' % original['id'])

    def resolve(self, name, code, number):
        data = self.set_for(code)
        matches = [c for c in data['cards'] if _ptcg_number(c.get('localId', '')) == _ptcg_number(number)]
        if not matches:
            raise PTCGError('missing_card', '系列 %s 中没有编号 %s' % (code, number))
        if len(matches) != 1:
            raise PTCGError('ambiguous_card', '系列 %s 编号 %s 不唯一' % (code, number))
        brief = matches[0]
        card_id = brief.get('id')
        if not isinstance(card_id, str):
            raise PTCGError('identity_mismatch', '卡牌缺少真实 ID')
        card = self.get('/cards/' + urllib.parse.quote(card_id, safe=''))
        if not card:
            raise PTCGError('missing_card', '卡牌详情不存在：%s' % card_id)
        if not isinstance(card, dict) or card.get('id') != card_id or card.get('set', {}).get('id') != data['id'] or _ptcg_number(card.get('localId', '')) != _ptcg_number(number):
            raise PTCGError('identity_mismatch', '卡牌详情身份与指定系列、编号不一致：%s' % card_id)
        actual_name = str(card.get('name') or '')
        basic_energy = _ptcg_basic_energy(card, self.language, allow_known_mislabel=True)
        if _ptcg_name(name, self.language, basic_energy) != _ptcg_name(actual_name, self.language, basic_energy):
            raise PTCGError('name_mismatch', '指定 %s %s 的名称为“%s”，不是“%s”' % (code, number, actual_name, name))
        url = self.image_url(card)
        if url:
            return card, url
        if basic_energy:
            return self.basic_energy_fallback(card)
        raise PTCGError('missing_image', '卡牌 %s 有资料但尚无图片；不会替换成其他版本' % card_id)


class PTCGPictureCache:
    """Independent exact-print index: names never serve as lookup keys."""
    def __init__(self, directory, language, core):
        self.directory = os.path.abspath(directory)
        self.language = language
        self.core = core
        self.paths = core.PictureCache(self.directory)  # reuse safe allocation/CRC only
        self.file = os.path.join(self.directory, 'ptcg-index.json')
        self.entries = {}
        try:
            with open(self.file, encoding='utf-8') as fh:
                stored = json.load(fh)
            if stored.get('version') == 1 and stored.get('language') == language and isinstance(stored.get('cards'), dict):
                self.entries = stored['cards']
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def key(self, code, number):
        # No cross-language lowercase identity folding.
        return self.language + ':' + code + ':' + _ptcg_number(number)

    def lookup(self, name, code, number):
        stored = self.entries.get(self.key(code, number))
        if not stored or _ptcg_name(stored.get('input_name')) != _ptcg_name(name):
            return None
        return self.paths.restore(stored)

    def store(self, entry):
        if not entry.get('files'):
            return
        stored = copy.deepcopy(entry)
        stored.pop('qty', None)
        stored.pop('cache_hit', None)
        for record in stored['files']:
            filename = os.path.basename(record['file'])
            record['file'] = filename
            self.paths.remember_image(filename, record.get('url'), record.get('bytes'))
        self.entries[self.key(entry['set_code'], entry['collector_number'])] = stored

    def save(self):
        self.paths.save()
        self.core.atomic_json(self.file, {'version': 1, 'language': self.language, 'cards': self.entries})


def run_ptcg(args, text, core):
    language = 'zh-tw' if args.game == 'ptcg-zh' else 'en'
    rows = parse_ptcg_decklist(text)  # reject entire malformed task before creating dirs/API
    if not rows:
        print('牌表为空：需要数字开头的“数量 卡名 系列代码 编号”行。')
        return 2
    if not args.pics:
        args.pics = os.path.join(os.path.dirname(core.program_cache_directory()), '卡图输出', 'ptcg-pics', language)
    os.makedirs(args.out, exist_ok=True)
    core._reset_network_state()
    core._image_interval = args.image_interval
    client = PTCGClient(language, core.program_cache_directory(), core)
    cache = PTCGPictureCache(args.pics, language, core)
    unique = list(dict.fromkeys((name, code, number) for qty, name, code, number in rows))
    hits = {} if args.force else {row: cache.lookup(*row) for row in unique}
    pending = [row for row in unique if not hits.get(row)]
    resolved = {}
    print('宝可梦TCG %s | 查询 %d 线程 | 下载 %d 线程 | 图片间隔 %g 秒' % (language, args.workers, args.download_workers, args.image_interval), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='ptcg-query') as pool:
        futures = {pool.submit(client.resolve, *row): row for row in pending}
        finished = 0
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            finished += 1
            try:
                resolved[row] = future.result()
            except Exception as exc:
                resolved[row] = exc
            print('[查询 %d/%d] %s %s %s' % (finished, len(pending), *row), flush=True)
    results = []
    for index, (qty, name, code, number) in enumerate(rows, 1):
        row = (name, code, number)
        entry = {'input': '%s %s %s' % row, 'input_name': name, 'qty': qty,
                 'game': args.game, 'lang_code': language, 'set_code': code, 'collector_number': number}
        if hits.get(row):
            entry = copy.deepcopy(hits[row])
            entry.update(qty=qty, cache_hit=True, input='%s %s %s' % row,
                         input_name=name, set_code=code, collector_number=number,
                         requested_set_code=code, requested_collector_number=number)
            print('[缓存 %d/%d] %s -> %s' % (index, len(rows), entry['input'], entry['display_name']), flush=True)
        else:
            value = resolved[row]
            if isinstance(value, Exception):
                entry.update(status=getattr(value, 'status', 'request_error'), error=str(value), files=[])
                print('  查询失败：%s | %s' % (entry['input'], value), flush=True)
                results.append(entry)
                continue
            card, url = value
            entry.update(selected_id=card['id'], tcgdex_set_id=card['set']['id'],
                         display_name=card['name'], english_name=card['name'] if language == 'en' else '',
                         lang='繁体中文' if language == 'zh-tw' else '英文', set_name=card['set'].get('name', ''),
                         chinese_name=language == 'zh-tw', status='pending',
                         selected_collector_number=card['localId'],
                         requested_set_code=code, requested_collector_number=number)
            fallback = card.get('_ptcg_fallback')
            if fallback:
                entry.update(fallback_reason=fallback['reason'],
                             requested_id=fallback['requested_id'],
                             requested_tcgdex_set_id=fallback['requested_tcgdex_set_id'],
                             requested_tcgdex_collector_number=fallback['requested_tcgdex_collector_number'])
            base = core.safe_name('%s_%s_%s' % (card['name'], card['set']['id'], card['localId']))
            filename = cache.paths.allocate(base, 'png', url)
            path = cache.paths.image_path(filename)
            record = {'file': path, 'format': 'png', 'label': None, 'url': url, 'status': 'pending'}
            if not args.force and core.complete_image(path):
                record.update(status='exists', bytes=os.path.getsize(path))
            entry['files'] = [record]
        if args.dry_run:
            entry['status'] = 'dry-run'
            for record in entry['files']:
                record['status'] = 'dry-run'
        cache.store(entry)
        results.append(entry)
    cache.save()  # persist URLs before downloads so interrupted tasks are recoverable
    if not args.dry_run:
        core.download_all(results, args.download_workers)
    pdf_paths = []
    for entry in results:
        if not entry.get('files'):
            continue
        if args.dry_run:
            entry.pop('error', None)
            entry['status'] = 'dry-run'
        elif all(r['status'] in ('ok', 'exists') and core.complete_image(r['file'], r.get('bytes')) for r in entry['files']):
            entry.pop('error', None)
            entry['status'] = 'exists' if all(r['status'] == 'exists' for r in entry['files']) else 'ok'
            pdf_paths.extend([r['file'] for r in entry['files']] * entry['qty'])
        else:
            entry.update(status='error', error='图片下载失败或校验未通过')
            for record in entry['files']:
                if record['status'] in ('ok', 'exists') and not core.complete_image(record['file'], record.get('bytes')):
                    record.update(status='error', error='图片完整性校验失败')
        cache.store(entry)
    cache.save()
    ok = sum(e['status'] in ('ok', 'exists', 'dry-run') for e in results)
    failed = len(results) - ok
    manifest = {'game': args.game, 'language': language, 'out_dir': cache.directory,
                'cards': results, 'tokens': [], 'summary': {'processed': len(results), 'ok': ok, 'failed': failed}}
    core.atomic_json(os.path.join(args.out, 'manifest.json'), manifest)
    lines = ['宝可梦TCG %s | 成功 %d / 失败 %d' % (language, ok, failed)]
    for entry in results:
        lines.append('%d %s | %s | %s%s' % (entry['qty'], entry['input'], entry.get('lang', language), entry['status'], ' | ' + entry['error'] if entry.get('error') else ''))
        if entry.get('selected_id'):
            lines.append('    实际图片：%s | %s %s' % (entry['selected_id'], entry['tcgdex_set_id'], entry.get('selected_collector_number', '')))
        if entry.get('fallback_reason'):
            lines.append('    回退：%s | 原请求 %s %s（%s）' % (entry['fallback_reason'], entry['set_code'], entry['collector_number'], entry['requested_id']))
        lines.extend('    %s [%s]' % (os.path.basename(r['file']), r['status']) for r in entry.get('files', []))
    with open(os.path.join(args.out, 'manifest.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    print('完成：成功 %d / 失败 %d' % (ok, failed), flush=True)
    if pdf_paths and not args.no_pdf and not args.dry_run:
        info = core.make_pdf(pdf_paths, args.paper, os.path.join(args.out, 'cards.pdf'), cut_marks=not args.no_cut_marks)
        if not info or info['cards'] != len(pdf_paths):
            print('部分图片未能进入 PDF', flush=True)
            return 1
    return 1 if failed else 0
