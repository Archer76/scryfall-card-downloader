#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Scryfall 卡图获取器 —— 提交中英文牌表，自动取回中文卡图与可打印卡册。

零第三方依赖，只用 Python 标准库。默认查询 4 线程、图片下载 8 线程，遇到 HTTP 403/429/503 自动减半下载并发并退避。

用法:
    python scryfall_cards_threaded.py 牌表.txt
    python scryfall_cards_threaded.py 牌表.txt --paper A3 --out D:\\卡图
    python scryfall_cards_threaded.py - < 牌表.txt            # 从标准输入读
    python scryfall_cards_threaded.py 牌表.txt --no-pdf       # 只要图片
    python scryfall_cards_threaded.py 牌表.txt --dry-run      # 只解析选图，不下载

牌表每行一张，中英文均可:
    Lightning Bolt  /  闪电击
    4 Lightning Bolt  /  4 闪电击
    1 Counterspell (CMM) 81
    井号、双斜杠、分号开头的行按注释忽略。

选图规则:
    1. 语言链 简体(zhs) > 繁体(zht) > 英文(en)，其他语言不考虑；
    2. 同语言内取常规系列中最新的一个，并在其中取 collector_number 最小者，
       即该系列的普通插画；promo / Secret Lair / masterpiece 等特殊类型不参与；
    3. 炼金(Alchemy)版本一律排除；
    4. 基本地（含雪境）与荒野不限语言，以高清优先；
    5. 图片取 png，即 Scryfall 的最高清晰度尺寸（745x1040，约合 300 DPI）。

命名规则:
    * 文件名用 Scryfall 的中文牌名，可从其他打印版本另行获取；
    * 单面牌  -> <中文牌名>.png
    * 多面牌  -> <完整牌名>_正面.png 与 <完整牌名>_背面.png（两面都下载）
    * 历险牌  -> <完整译名>.png（如「碎骨巨人-一脚踩下」，只有一张图）
    * 未取得中文名时回退英文名，并在清单中标注。

数量处理:
    牌表里的数量不影响下载，只影响卡册——「8 Forest」只下载一张 树林.png，
    但卡册中会排入 8 张树林，方便直接打印代牌。

产出:
    卡图输出/mtg-pics/*.png   共享卡图，重复牌名直接复用；--pics 可指定缓存目录
    卡图输出/mtg-pics/index.json  牌名别名与选图索引
    <out>/manifest.txt    人读清单
    <out>/manifest.json   结构化清单（含系列、编号、语言、原始 URL）
    <out>/cards.pdf       卡册：万智牌标准卡尺寸 63x88mm 排版，可直接打印
                          A4 竖向每页 9 张（3x3）；A3 横向每页 18 张（6x3）
"""
from __future__ import annotations

import argparse
import copy
import shutil
from contextlib import contextmanager
import hashlib
import math
import tempfile
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from collections import deque
import threading
import glob
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

API_BASE = "https://api.scryfall.com"
UA = "ScryfallCards/1.0 (personal card image fetcher)"
DEFAULT_WORKERS = 4
MAX_WORKERS = 16
DEFAULT_DOWNLOAD_WORKERS = 8  # 48 张/档的小样实测吞吐最高；非服务端保证
MAX_DOWNLOAD_WORKERS = 32
# /cards/named、/cards/search 当前上限 2 次/秒；留 100ms 余量。
MIN_API_INTERVAL = 0.6
# 图片不设固定启动间隔；线程上限、服务端冷却与 Retry-After 仍有效。
MIN_IMAGE_INTERVAL = 0.0
_image_interval = None  # None 使用默认值；命令行每次任务单独设置。
API_CACHE_TTL = 24 * 60 * 60
LANG_LABEL = {"zhs": "简体中文", "zht": "繁体中文", "en": "英文"}
LANG_RANK = {"zhs": 0, "zht": 1, "en": 2}
FACE_LABELS = ("正面", "背面")
FULL_SEP = "-"                   # 完整牌名的面间隔符
ALCHEMY_FILTER = "-is:alchemy"   # 排除炼金（Alchemy）版本
# 常规系列类型：promo / box(Secret Lair) / masterpiece(神秘档案) / funny 等不参与选图
NORMAL_SET_TYPES = {
    "core", "expansion", "masters", "commander", "draft_innovation",
    "duel_deck", "starter", "premium_deck", "planechase", "archenemy",
    "spellbook", "eternal",
}
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# PDF 相关
MM = 72.0 / 25.4                 # 毫米 -> PDF point
CARD_W_MM, CARD_H_MM = 63.0, 88.0  # 万智牌标准卡尺寸
# 纸张尺寸（毫米，宽 x 高）。A4 取竖向排 3x3=9 张；
# A3 取横向排 6x3=18 张，比竖向的 4x4 多两张。
PAPERS = {"A4": (210.0, 297.0), "A3": (420.0, 297.0)}

_thread_state = threading.local()
_last_call = [0.0]
_api_cooldown_until = [0.0]
_api_lock = threading.Lock()
_api_backoff_interval = [0.0]
_api_abort_reason = [None]
_api_rate_limit_events = [0]
_image_lock = threading.Lock()
_last_image_call = [0.0]
_image_cooldown_until = [0.0]
_image_backoff_interval = [0.0]
_image_abort_reason = [None]
_image_rate_limit_events = [0]
_api_disk_cache_dir = None
_query_log_lock = threading.Lock()
_exact_print_refresh = False
_print_cache = {}
_print_cache_lock = threading.Lock()


def opener():
    """每个线程独立的连接处理器，使用 urllib 的系统/环境代理设置。"""
    if not hasattr(_thread_state, "opener"):
        _thread_state.opener = urllib.request.build_opener()
    return _thread_state.opener


def _reset_network_state():
    global _image_interval
    _image_interval = None
    with _api_lock:
        _last_call[0] = _api_cooldown_until[0] = _api_backoff_interval[0] = 0.0
        _api_abort_reason[0] = None
        _api_rate_limit_events[0] = 0
    with _image_lock:
        _last_image_call[0] = _image_cooldown_until[0] = _image_backoff_interval[0] = 0.0
        _image_abort_reason[0] = None
        _image_rate_limit_events[0] = 0


def _throttle():
    """全体 API 线程共用 0.6 秒启动间隔及限流冷却时间。"""
    while True:
        with _api_lock:
            if _api_abort_reason[0]:
                raise RuntimeError(_api_abort_reason[0])
            now = time.monotonic()
            interval = max(MIN_API_INTERVAL, _api_backoff_interval[0])
            delay = max(_last_call[0] + interval - now, _api_cooldown_until[0] - now, 0.0)
            if delay <= 0:
                _last_call[0] = now
                return
        time.sleep(min(delay, 0.5))


def _pause_api(seconds, slow_down=False):
    with _api_lock:
        now = time.monotonic()
        # 同一冷却窗口只降速一次，避免多个已发出的请求同时返回 429 时重复惩罚。
        if slow_down and now >= _api_cooldown_until[0]:
            _api_backoff_interval[0] = min(4.8, max(MIN_API_INTERVAL, _api_backoff_interval[0]) * 2)
            _api_rate_limit_events[0] += 1
            if _api_rate_limit_events[0] >= 4:
                _api_abort_reason[0] = "API 先后 4 次进入限流，已停止本次后续 API 请求；请等待解除限制再运行。"
        _api_cooldown_until[0] = max(_api_cooldown_until[0], now + seconds)


def _throttle_images():
    """首次下载及普通网络错误重试都必须经过同一个图片请求时钟。"""
    while True:
        with _image_lock:
            if _image_abort_reason[0]:
                raise RuntimeError(_image_abort_reason[0])
            now = time.monotonic()
            interval = max(MIN_IMAGE_INTERVAL if _image_interval is None else _image_interval, _image_backoff_interval[0])
            delay = max(_last_image_call[0] + interval - now, _image_cooldown_until[0] - now, 0.0)
            if delay <= 0:
                _last_image_call[0] = now
                return
        time.sleep(min(delay, 0.5))


def _pause_images(seconds, slow_down=False):
    with _image_lock:
        now = time.monotonic()
        if slow_down and now >= _image_cooldown_until[0]:
            _image_backoff_interval[0] = min(3.2, max(MIN_IMAGE_INTERVAL, _image_backoff_interval[0]) * 2)
            _image_rate_limit_events[0] += 1
            if _image_rate_limit_events[0] >= 4:
                _image_abort_reason[0] = "图片服务先后 4 次进入限流，已停止本次后续图片请求。"
        _image_cooldown_until[0] = max(_image_cooldown_until[0], now + seconds)


def retry_after_seconds(headers, default=0.0):
    """支持秒数与 HTTP 日期；不截断服务器要求的等待时间。"""
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return default
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else default
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default


def blocked_delay(code, attempts, retry_after=0.0):
    # API 429 会至少限制访问 30 秒；图片也采用同样的保守退避。
    base = 30.0 if code == 429 else (60.0 if code == 403 else 15.0)
    return max(retry_after, base * (2 ** max(0, attempts - 1)))


def _query_log(message):
    with _query_log_lock:
        print(message, flush=True)


def _api_cache_path(url):
    if _api_disk_cache_dir is None:
        return None
    return os.path.join(_api_disk_cache_dir, hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")


def _read_api_cache(url):
    path = _api_cache_path(url)
    if path is None:
        return False, None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            cached = json.load(stream)
        age = time.time() - cached["saved_at"]
        if "data" in cached and (cached["data"] is None or isinstance(cached["data"], dict)) and 0 <= age < API_CACHE_TTL:
            return True, cached["data"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return False, None


def _write_api_cache(url, data):
    path = _api_cache_path(url)
    if path is None:
        return
    temporary = None
    try:
        os.makedirs(_api_disk_cache_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_api_disk_cache_dir,
                                         prefix="cache-", suffix=".part", delete=False) as stream:
            temporary = stream.name
            json.dump({"saved_at": time.time(), "data": data}, stream, ensure_ascii=False)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        _query_log("  提示：查询缓存未能保存，任务继续：%s" % exc)
    finally:
        if temporary is not None:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass


def api_get(path, params=None, retries=4, cache_missing=True, refresh=False):
    """API 请求缓存 24h；限流全局退避，持续受限时停止排队网络请求。

    cache_missing=False 用于关联 token 的按 ID 查询：404 不写负缓存，已有负缓存也不采信，
    使失败的关联检查下次真正重试。"""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    hit, data = (False, None) if refresh else _read_api_cache(url)
    if hit and (data is not None or cache_missing):
        _query_log("  API 缓存命中: %s" % url)
        return data
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json;q=0.9,*/*;q=0.8",
        })
        handler = opener()
        _throttle()
        _query_log("  API 请求 [%d/%d]: %s" % (attempt + 1, retries, url))
        try:
            with handler.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _write_api_cache(url, data)
            return data
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 404:
                exc.close()
                if cache_missing:
                    _write_api_cache(url, None)
                return None
            if code == 403:
                retry_after = retry_after_seconds(exc.headers)
                if retry_after:
                    _pause_api(retry_after)
                reason = "API HTTP 403：访问被拒绝，已停止本次后续 API 请求；请检查 IP/代理或等待解除限制。"
                with _api_lock:
                    _api_abort_reason[0] = reason
                exc.close()
                _query_log("  " + reason)
                raise RuntimeError(reason)
            if code == 429 or 500 <= code < 600:
                last_err = exc
                if code in (429, 503):
                    delay = blocked_delay(code, attempt + 1, retry_after_seconds(exc.headers))
                    _pause_api(delay, slow_down=(code == 429))
                    _query_log("  API 受限 HTTP %s：所有查询暂停新请求 %.0f 秒后重试；请求间隔 %.2f 秒。" %
                               (code, delay, max(MIN_API_INTERVAL, _api_backoff_interval[0])))
                    if code == 429 and attempt + 1 >= retries:
                        with _api_lock:
                            _api_abort_reason[0] = "API 持续返回 HTTP 429，已停止本次后续 API 请求；请等待解除限制再运行。"
                elif attempt + 1 < retries:
                    time.sleep(1.0 * (attempt + 1))
                exc.close()
                continue
            detail = exc.read().decode("utf-8", "replace")[:200]
            exc.close()
            raise RuntimeError("HTTP %s @ %s | %s" % (code, url, detail))
        except Exception as exc:
            # 上面 HTTP 分支中的异常不会进入这个同级 except。
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("请求失败 @ %s | %s" % (url, last_err))


class DownloadBlocked(RuntimeError):
    def __init__(self, code, retry_after=5.0):
        super().__init__("HTTP %s，服务器拒绝或限制下载" % code)
        self.code = code
        self.retry_after = retry_after


def download(url, dest, retries=3):
    """普通网络错误在线程内重试；限流交给调度器降并发后重试。"""
    last_err = None
    tmp = dest + ".part"
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})
        handler = opener()
        _throttle_images()
        try:
            with handler.open(req, timeout=120) as resp:
                blob = resp.read()
            if not blob:
                raise RuntimeError("服务器返回空图片")
            if dest.lower().endswith(".png") and not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("服务器返回的内容不是 PNG 图片")
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, dest)
            return len(blob)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 503):
                code = exc.code
                delay = blocked_delay(code, 1, retry_after_seconds(exc.headers))
                _pause_images(delay, slow_down=(code == 429))
                exc.close()
                raise DownloadBlocked(code, delay)
            last_err = exc
            exc.close()
        except Exception as exc:
            last_err = exc
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if attempt + 1 < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("下载失败 %s | %s" % (url, last_err))


def download_all(results, workers):
    """按目标路径去重下载；收集结果后由主线程更新记录。"""
    jobs = {}
    for entry in results:
        for record in entry.get("files", []):
            if record["status"] == "pending":
                dest = record["file"]
                if dest not in jobs:
                    jobs[dest] = {"url": record["url"], "dest": dest,
                                  "records": [], "blocks": 0}
                jobs[dest]["records"].append(record)
    if not jobs:
        print("无需下载新图片。", flush=True)
        return
    queue = deque(jobs.values())
    active = {}
    limit = workers
    completed = 0
    total = len(jobs)
    cooldown_until = 0.0
    last_report = time.monotonic()
    print("开始下载：%d 张图片，初始并发 %d。" % (total, limit), flush=True)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="card-image") as pool:
        while queue or active:
            now = time.monotonic()
            while queue and len(active) < limit and now >= cooldown_until:
                job = queue.popleft()
                active[pool.submit(download, job["url"], job["dest"])] = job
            blocked_this_batch = False
            if active:
                done, _ = wait(active, timeout=1.0, return_when=FIRST_COMPLETED)
            else:
                time.sleep(min(1.0, max(0.0, cooldown_until - now)))
                done = set()
            for future in done:
                job = active.pop(future)
                try:
                    size = future.result()
                except DownloadBlocked as exc:
                    job["blocks"] += 1
                    blocked_this_batch = True
                    delay = blocked_delay(exc.code, job["blocks"], exc.retry_after)
                    _pause_images(delay, slow_down=(exc.code == 429))
                    cooldown_until = max(cooldown_until, time.monotonic() + delay)
                    if job["blocks"] < 5:
                        queue.append(job)
                        print("  受限 HTTP %s：%s，等待后重试 (%d/4)" %
                              (exc.code, os.path.basename(job["dest"]), job["blocks"]), flush=True)
                        continue
                    error = "持续被阻断（5 次）：%s" % exc
                    if exc.code in (403, 429):
                        with _image_lock:
                            _image_abort_reason[0] = "图片服务持续拒绝或限制访问，已停止后续图片请求：%s" % exc
                    for record in job["records"]:
                        record.update(status="error", error=error)
                    print("  下载失败：%s | %s" % (os.path.basename(job["dest"]), error), flush=True)
                except Exception as exc:
                    for record in job["records"]:
                        record.update(status="error", error=str(exc))
                    print("  下载失败：%s | %s" % (os.path.basename(job["dest"]), exc), flush=True)
                else:
                    for record in job["records"]:
                        record.update(status="ok", bytes=size)
                    print("  下载完成 [%d/%d] %s (%.1f KB)" %
                          (completed + 1, total, os.path.basename(job["dest"]), size / 1024.0), flush=True)
                completed += 1
            if blocked_this_batch:
                previous = limit
                limit = max(1, limit // 2)
                print("  自动降并发：%d -> %d；暂停新任务，剩余 %.0f 秒后继续。" %
                      (previous, limit, max(0.0, cooldown_until - time.monotonic())), flush=True)
            now = time.monotonic()
            if now - last_report >= 5.0:
                print("  下载进度 %d/%d | 运行中 %d | 排队 %d | 并发上限 %d | 冷却 %.0f 秒" %
                      (completed, total, len(active), len(queue), limit,
                       max(0.0, cooldown_until - now)), flush=True)
                last_report = now
    print("下载阶段完成：%d/%d。" % (completed, total), flush=True)


# =========================================================================== 查询

def resolve_card(name):
    """把牌名解析成一张 Scryfall 卡（中英文牌名均可）。"""
    card = api_get("/cards/named", {"fuzzy": name})
    if card is None:
        card = api_get("/cards/named", {"exact": name})
    if card is None:
        listing = api_get("/cards/search", {"q": '!"%s"' % name, "unique": "cards"})
        if listing and listing.get("data"):
            card = listing["data"][0]
    if card is None:
        listing = api_get("/cards/search", {"q": name, "unique": "cards"})
        if listing and listing.get("data"):
            card = listing["data"][0]
    return card


def search_prints(query, max_pages=3):
    """按查询串列出打印（按出版日期降序），跨页收集。"""
    found = []
    page = 1
    while page <= max_pages:
        data = api_get("/cards/search", {
            "q": query, "unique": "prints", "order": "released",
            "dir": "desc", "page": page, "include_extras": "true",
        })
        if data is None:
            break
        found.extend(data.get("data") or [])
        if not data.get("has_more"):
            break
        page += 1
    return found


def prints_for(oracle_id, suffix="", max_pages=3):
    """同一个打印查询只有一个线程访问网络，其余线程等待共享结果。"""
    key = (oracle_id, suffix, max_pages)
    with _print_cache_lock:
        future = _print_cache.get(key)
        owner = future is None
        if owner:
            future = Future()
            _print_cache[key] = future
    if owner:
        try:
            query = "oracleid:%s %s" % (oracle_id, ALCHEMY_FILTER)
            if suffix:
                query += " " + suffix
            future.set_result(search_prints(query, max_pages))
        except BaseException as exc:
            future.set_exception(exc)
            with _print_cache_lock:
                if _print_cache.get(key) is future:
                    del _print_cache[key]
    return future.result()


FICTIONAL_LANGS = frozenset({"ph", "qya", "dw", "tlh"})


def printing_identity(spec):
    name, code, collector = spec
    return "mtg-print:%s:%s:%s" % (code, collector, cache_name(name))


def select_exact_print(spec, resolved):
    """Never substitute another set/collector, even for the same oracle."""
    name, code, collector = spec
    oracle = resolved.get("oracle_id")

    def usable(card):
        return bool(card and str(card.get("set", "")).lower() == code
                    and str(card.get("collector_number", "")) == collector
                    and card.get("oracle_id") == oracle and is_usable_image(card)
                    and bool(image_jobs(card))
                    and all(any(uris.get(key) for key in ("png", "large", "normal", "small", "border_crop"))
                            for _, uris in image_jobs(card))
                    and (bool(card.get("image_uris")) or len(image_jobs(card)) == len(card.get("card_faces") or [])))

    def quality(card):
        return (bool(card.get("highres_image")),
                {"highres_scan": 3, "full": 2, "lowres": 1}.get(card.get("image_status"), 0))

    root = "/cards/%s/%s/" % (urllib.parse.quote(code, safe=""), urllib.parse.quote(collector, safe=""))
    for lang in ("zhs", "zht", "en"):
        card = api_get(root + lang, cache_missing=False, refresh=_exact_print_refresh)
        if usable(card) and card.get("lang") == lang:
            return card, lang
    fictional = []
    for lang in sorted(FICTIONAL_LANGS):
        card = api_get(root + lang, cache_missing=False, refresh=_exact_print_refresh)
        if usable(card) and card.get("lang") == lang:
            fictional.append(card)
    if fictional:
        card = max(fictional, key=quality)
        return card, card.get("lang")
    found = []
    page = 1
    while True:
        listing = api_get("/cards/search", {
            "q": 'set:%s cn:"%s" lang:any' % (code, collector), "unique": "prints",
            "include_multilingual": "true", "include_extras": "true", "page": page,
        }, cache_missing=False, refresh=_exact_print_refresh)
        if not listing:
            break
        found.extend(c for c in listing.get("data", []) if usable(c))
        if not listing.get("has_more"):
            break
        page += 1
    if not found:
        return None, None
    rank = lambda c: {"zhs": 0, "zht": 1, "en": 2}.get(
        c.get("lang"), 3 if c.get("lang") in FICTIONAL_LANGS else 4)
    best_rank = min(map(rank, found))
    card = max((c for c in found if rank(c) == best_rank), key=quality)
    return card, card.get("lang")


def query_card(name, langs):
    """查询线程只返回元数据；文件命名和清单由主线程按牌表顺序处理。"""
    spec = parse_print_spec(name)
    resolved = resolve_card(spec[0] if spec else name)
    result = {"resolved": resolved}
    if not resolved:
        return result
    oracle = resolved["oracle_id"]
    selected, used_lang = select_exact_print(spec, resolved) if spec else select_print(oracle, langs, is_basic_land(resolved))
    result.update(selected=selected, used_lang=used_lang)
    result["token_check"] = inspect_token_parts(resolved, selected)
    if not selected:
        return result
    jobs = image_jobs(selected)
    result["jobs"] = jobs
    if jobs:
        result["display"], result["name_source"] = (face_names(selected), selected.get("set")) if spec else resolve_display_names(oracle, selected)
    return result


def query_all(rows, langs, workers):
    """按输入牌名去重，并行查询；下载阶段另有独立的线程设置。"""
    names = list(dict.fromkeys(name for qty, name in rows))
    results = {}
    total = len(names)
    _query_log("开始查询：%d 种牌，查询并发 %d。" % (total, workers))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="card-query") as pool:
        pending = {pool.submit(query_card, name, langs): name for name in names}
        last_report = time.monotonic()
        while pending:
            done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = {"error": str(exc)}
                _query_log("[查询 %d/%d] %s" % (len(results), total, name))
            now = time.monotonic()
            if now - last_report >= 5.0:
                _query_log("  查询进度 %d/%d | 未完成 %d | API 冷却 %.0f 秒" %
                           (len(results), total, len(pending), max(0.0, _api_cooldown_until[0] - now)))
                last_report = now
    _query_log("查询阶段完成：%d/%d。" % (total, total))
    return results


# =========================================================================== 选图

def has_cjk(text):
    return bool(text) and bool(CJK_RE.search(text))


def face_names(card):
    """该打印各面的展示名（面顺序即正面、背面）。"""
    faces = card.get("card_faces") or []
    if faces:
        return [(f.get("printed_name") or f.get("name")) for f in faces]
    return [card.get("printed_name") or card.get("name")]


def name_quality(card):
    """2=各面皆中文名, 1=部分中文, 0=无中文名。"""
    names = [n for n in face_names(card) if n]
    if not names:
        return 0
    good = sum(1 for n in names if has_cjk(n))
    if good == len(names):
        return 2
    return 1 if good else 0


def collector_key(card):
    """收藏编号排序键：纯数字在前按数值升序，其余按字符串。"""
    raw = card.get("collector_number") or ""
    match = re.match(r"^(\d+)", raw)
    if match:
        return (0, int(match.group(1)), raw)
    return (1, 0, raw)


def date_value(card):
    return (card.get("released_at") or "1970-01-01").replace("-", "")


def pick_print_in_normal_set(cards, newest=True):
    """在常规系列中挑选打印。

    newest=True 取最新系列（中文卡图质量随年代提升）；
    newest=False 取最早系列（回退英文时，原版扫描通常比重印列表更清晰）。
    同一系列内一律取 collector_number 最小者，即该系列的普通插画版本。
    """
    if not cards:
        return None
    normal = [c for c in cards if (c.get("set_type") or "") in NORMAL_SET_TYPES]
    pool = normal or cards
    dates = [date_value(c) for c in pool]
    target = max(dates) if newest else min(dates)
    same_set = [c for c in pool if date_value(c) == target]
    return min(same_set, key=collector_key)


def pick_quality_first(cards):
    """基本地/荒野：高清优先，其次语言偏好，再次出版日期新、编号小。"""
    if not cards:
        return None
    pool = [c for c in cards if c.get("highres_image")] or cards
    return sorted(pool, key=lambda c: (
        LANG_RANK.get(c.get("lang"), 3),
        -int(date_value(c)),
        collector_key(c),
    ))[0]


def is_basic_land(card):
    """基本地判断：一并覆盖平原/海岛/沼泽/山脉/树林/荒野与雪境基本地。"""
    type_line = (card.get("type_line") or "").lower()
    return "basic" in type_line and "land" in type_line


def is_usable_image(card):
    """排除占位图与缺失图。

    Scryfall 对尚未扫描的语种会挂一张 placeholder——它不提供中文卡图，
    只是一张低清的英文占位图；取它会得到中文名配低清英文图的结果。
    """
    return (card.get("image_status") or "") not in ("placeholder", "missing")


def select_print(oracle_id, langs, basic):
    """返回 (选中打印, 语言说明)。

    逐语言挑选时跳过 placeholder / missing 的打印：Scryfall 对尚未扫描的
    语种只挂一张低清英文占位图，取它会让「中文优先」落到一张假中文图上。
    """
    if basic:
        for lang in langs:
            got = [c for c in prints_for(oracle_id, "is:highres lang:%s" % lang) if is_usable_image(c)]
            if got:
                return pick_quality_first(got), LANG_LABEL.get(lang, lang)
        got = [c for c in prints_for(oracle_id, "is:highres") if is_usable_image(c)]
        if got:
            return pick_quality_first(got), "任何语言"
        got = [c for c in prints_for(oracle_id) if is_usable_image(c)]
        return (pick_quality_first(got), "任何语言") if got else (None, None)

    for lang in langs:
        got = [c for c in prints_for(oracle_id, "lang:%s" % lang) if is_usable_image(c)]
        if got:
            # 中文卡图质量随年代提升故取最新；回退英文时取原版，扫描更清晰
            return pick_print_in_normal_set(got, newest=(lang != "en")), lang
    got = [c for c in prints_for(oracle_id) if is_usable_image(c)]
    return (pick_print_in_normal_set(got), "任何语言") if got else (None, None)


def resolve_display_names(oracle_id, selected):
    """取完整中文牌名：优先中文打印中译名齐全者，可从其他打印另行获取。"""
    partial = None
    for lang in ("zhs", "zht"):
        got = prints_for(oracle_id, "lang:%s" % lang)
        if not got:
            continue
        ranked = sorted(got, key=name_quality, reverse=True)
        if name_quality(ranked[0]) == 2:
            return face_names(ranked[0]), ranked[0].get("set")
        if partial is None and name_quality(ranked[0]) == 1:
            partial = ranked[0]
    if partial is not None:
        return face_names(partial), partial.get("set")
    return face_names(selected), selected.get("set")


def full_display_name(names):
    """各面名合成完整牌名；多面用 FULL_SEP 连接。"""
    cleaned = [n for n in names if n]
    return FULL_SEP.join(cleaned) if cleaned else None


def image_jobs(card):
    """列出待下载图片：(面标注, 地址表)。

    顶层有 image_uris 即单张图（普通牌、历险牌、split 等）；
    否则按 card_faces 逐面下载（transform、modal_dfc 等双面牌）。
    """
    top = card.get("image_uris")
    if top:
        return [(None, top)]
    faces = card.get("card_faces") or []
    jobs = []
    multi = len(faces) > 1
    for index, face in enumerate(faces):
        uris = face.get("image_uris")
        if not uris:
            continue
        if not multi:
            label = None
        elif index < len(FACE_LABELS):
            label = FACE_LABELS[index]
        else:
            label = "面%d" % (index + 1)
        jobs.append((label, uris))
    return jobs


def safe_name(text):
    cleaned = ILLEGAL.sub("_", text or "")
    cleaned = re.sub(r"[_\s]+", " ", cleaned).strip(" .")
    return cleaned or "card"


# =========================================================================== 牌表

QTY_RE = re.compile(r"^\s*(\d+)\s*[xX]?\s+(.*\S)\s*$")
PRINT_SPEC_RE = re.compile(r"^(.+?)\s+(?:\(([A-Za-z0-9]{2,6})\)|([A-Z0-9]{2,6}))\s+([A-Za-z0-9★*†-]*\d[A-Za-z0-9★*†-]*)$")


def parse_print_spec(name):
    """Only an explicit set code plus collector number is a printing request."""
    match = PRINT_SPEC_RE.fullmatch(str(name or "").strip())
    if not match:
        return None
    card_name, parenthesized, bare, collector = match.groups()
    code = parenthesized or bare
    if not any(c.isalpha() for c in code):
        return None
    return card_name.strip(), code.lower(), collector


def canonical_print_input(spec):
    name, code, collector = spec
    return "%s (%s) %s" % (name, code.upper(), collector)


SET_RE = re.compile(r"\s*[\(\[]([A-Za-z0-9]{2,6})[\)\]]\s*$")
SKIP_WORDS = {
    "deck", "decks", "sideboard", "mainboard", "commander", "companion",
    "maybeboard", "creatures", "lands", "instants", "sorceries", "enchantments",
    "artifacts", "planeswalkers", "battles", "tokens", "about",
}


def parse_decklist(text):
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line[0].isdigit():
            continue
        match = QTY_RE.match(line)
        if not match:
            raise ValueError("数字开头的牌表行需要数量与卡名，例如 4 Lightning Bolt：%s" % line)
        qty = int(match.group(1))
        if qty <= 0:
            raise ValueError("牌表数量必须大于 0：%s" % line)
        line = match.group(2).strip()
        if line.lower() in SKIP_WORDS:
            continue
        spec = parse_print_spec(line)
        name = canonical_print_input(spec) if spec else SET_RE.sub("", line).strip()
        if name:
            rows.append((qty, name))
    return rows


# =========================================================================== 共享图片缓存


def cache_name(name):
    return re.sub(r"\s+", " ", re.sub(r"\s*//\s*", " // ", str(name or ""))).strip().casefold()


def complete_image(path, expected_bytes=None):
    """检查 PNG 分块 CRC/结束标记，避免把残缺下载当作可用缓存。"""
    try:
        if os.path.islink(path):
            return False
        with open(path, "rb") as stream:
            blob = stream.read()
        if expected_bytes and len(blob) != expected_bytes:
            return False
        if path.lower().endswith((".jpg", ".jpeg")):
            return blob.startswith(b"\xff\xd8") and blob.endswith(b"\xff\xd9")
        if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        pos, header = 8, False
        while pos + 12 <= len(blob):
            length = struct.unpack(">I", blob[pos:pos + 4])[0]
            end = pos + 12 + length
            if end > len(blob):
                return False
            tag = blob[pos + 4:pos + 8]
            crc = struct.unpack(">I", blob[end - 4:end])[0]
            if zlib.crc32(blob[pos + 4:end - 4]) & 0xffffffff != crc:
                return False
            if tag == b"IHDR":
                if length != 13 or pos != 8:
                    return False
                width, height = struct.unpack(">II", blob[pos + 8:pos + 16])
                header = width > 0 and height > 0
            if tag == b"IEND":
                return header and length == 0 and end == len(blob)
            pos = end
    except (OSError, ValueError, struct.error):
        pass
    return False


def atomic_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix="index-", suffix=".part", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)


class PictureCache:
    """永久保存选图与牌名别名；索引内图片路径相对当前缓存目录，移动程序仍可复用。"""
    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        self.root = os.path.dirname(self.directory)
        os.makedirs(self.directory, exist_ok=True)
        self.path = os.path.join(self.directory, "index.json")
        self.data = {"version": 1, "cards": {}, "aliases": {}, "images": {}, "migrated": []}
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if loaded.get("version") == 1 and all(isinstance(loaded.get(key), dict) for key in ("cards", "aliases", "images")):
                self.data = loaded
                self.data.setdefault("migrated", [])
            else:
                _query_log("图片索引格式不正确，将从历史清单重建。")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            _query_log("图片索引无法读取，将从历史清单重建。")
        self.data.setdefault("tokens", {})
        self.data.setdefault("token_ids", {})
        self.preexisting_keys = set(self.data["cards"])
        self.pending_removals = {}

    @staticmethod
    def key(entry):
        spec = parse_print_spec(entry.get("input"))
        if spec:
            return printing_identity(spec)
        return cache_name(entry.get("english_name") or entry.get("display_name") or entry.get("input"))

    @staticmethod
    def image_identity(url):
        return str(url or "").split("?", 1)[0]

    def image_path(self, filename):
        if not isinstance(filename, str) or not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
            raise ValueError("图片索引中的文件名不安全")
        return os.path.join(self.directory, filename)

    def save(self):
        atomic_json(self.path, self.data)

    def remember_image(self, filename, url, size=None):
        self.image_path(filename)
        meta = self.data["images"].setdefault(filename, {})
        if url:
            meta["url"] = url
        if size:
            meta["bytes"] = size

    def allocate(self, base, fmt, url):
        identity = self.image_identity(url)
        for filename, meta in self.data["images"].items():
            if identity and self.image_identity(meta.get("url")) == identity:
                self.image_path(filename)
                return filename
        original = "%s.%s" % (base, fmt)
        filename = original
        seq = 1
        while filename in self.data["images"] or os.path.exists(self.image_path(filename)):
            token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
            suffix = token if seq == 1 else "%s_%d" % (token, seq)
            filename = "%s_%s.%s" % (base, suffix, fmt)
            seq += 1
        self.remember_image(filename, url)
        return filename

    def store(self, entry):
        if not entry.get("files") or not self.key(entry):
            return
        stored = copy.deepcopy(entry)
        stored.pop("qty", None)
        stored.pop("cache_hit", None)
        for record in stored["files"]:
            filename = os.path.basename(record["file"])
            record["file"] = filename
            self.remember_image(filename, record.get("url"), record.get("bytes"))
        if entry.get("kind") == "token":
            key = entry["cache_identity"]
            self.data["tokens"][key] = stored
            for card_id in entry.get("related_ids", []):
                self.data["token_ids"][card_id] = key
            return
        key = self.key(stored)
        self.data["cards"][key] = stored
        if parse_print_spec(stored.get("input")):
            return
        names = [stored.get("input"), stored.get("english_name"), stored.get("display_name")]
        names.extend(stored.get("aliases") or [])
        if "//" in str(stored.get("english_name", "")):
            names.extend(stored["english_name"].split("//"))
        display = str(stored.get("display_name", ""))
        if len(stored["files"]) == 2 and {record.get("label") for record in stored["files"]} == set(FACE_LABELS) and display.count(FULL_SEP) == 1:
            names.extend(display.split(FULL_SEP))
        for name in names:
            alias = cache_name(name)
            if not alias:
                continue
            aliases = self.data["aliases"]
            previous = aliases.get(alias)
            if alias not in aliases or previous == key or self.data["cards"].get(previous, {}).get("filename_only"):
                aliases[alias] = key
            else:
                aliases[alias] = None  # 真正重名的别名需要正常解析，避免取错牌。

    def lookup(self, name):
        spec = parse_print_spec(name)
        if spec:
            entry = self.data["cards"].get(printing_identity(spec))
            return self.restore(entry) if entry else None
        alias = cache_name(name)
        key = self.data["aliases"].get(alias)
        if key is None and alias in self.data["aliases"]:
            return None
        entry = self.data["cards"].get(key)
        if entry is None:
            # 用户直接放入当前缓存目录 的完整图片，也可按文件名使用。
            token_files = {record["file"] for token in self.data["tokens"].values()
                           for record in token.get("files", [])}
            token_files.update(record["file"] for key, card in self.data["cards"].items()
                               if key.startswith("mtg-print:") for record in card.get("files", []))
            names = {cache_name(os.path.splitext(filename)[0]): filename
                     for filename in os.listdir(self.directory)
                     if filename not in token_files and filename.lower().endswith((".png", ".jpg", ".jpeg"))}
            filename = names.get(cache_name(safe_name(name)))
            if not filename or not complete_image(self.image_path(filename)):
                return None
            entry = {"input": name, "english_name": name if not has_cjk(name) else "",
                     "display_name": name, "chinese_name": has_cjk(name), "lang": "本地图片",
                     "filename_only": True, "files": [{"label": None, "file": self.image_path(filename),
                     "url": "", "format": os.path.splitext(filename)[1][1:], "status": "exists"}]}
            self.store(entry)
            key = self.key(entry)
            entry = self.data["cards"][key]
        return self.restore(entry)

    def lookup_token(self, card_id):
        key = self.data["token_ids"].get(card_id)
        entry = self.data["tokens"].get(key)
        return self.restore(entry) if entry else None

    def restore(self, entry):
        entry = copy.deepcopy(entry)
        if not entry.get("files"):
            return None
        for record in entry["files"]:
            try:
                filename = record["file"]
                path = self.image_path(filename)
                meta = self.data["images"].get(filename, {})
                if complete_image(path, meta.get("bytes")):
                    record.update(file=path, status="exists", bytes=os.path.getsize(path))
                    record.pop("error", None)
                elif record.get("url"):
                    record.update(file=path, status="pending")
                    record.pop("bytes", None)
                    record.pop("error", None)
                else:
                    return None
            except (KeyError, ValueError, TypeError):
                return None
        entry.update(status="exists", cache_hit=True)
        return entry

    @staticmethod
    def file_digest(path):
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()

    def _move_legacy_image(self, source):
        source = os.path.abspath(source)
        if self.excluded_legacy_path(source):
            return None
        if os.path.commonpath([os.path.realpath(self.root), os.path.realpath(source)]) != os.path.realpath(self.root):
            raise ValueError("旧图片不在当前输出目录中")
        if not complete_image(source):
            return None
        filename = os.path.basename(source)
        dest = self.image_path(filename)
        if os.path.realpath(source) == os.path.realpath(dest):
            return filename
        digest = self.file_digest(source)
        base, ext = os.path.splitext(filename)
        seq = 1
        while os.path.exists(dest):
            if self.file_digest(dest) == digest:
                self.pending_removals[source] = (dest, digest)
                return filename
            suffix = digest[:10] if seq == 1 else "%s_%d" % (digest[:10], seq)
            filename = "%s_%s%s" % (base, suffix, ext)
            dest = self.image_path(filename)
            seq += 1
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix="pic-", suffix=".part", delete=False) as stream:
                temporary = stream.name
                with open(source, "rb") as original:
                    shutil.copyfileobj(original, stream)
            if self.file_digest(temporary) != digest:
                raise OSError("迁移期间图片发生变化，保留原文件")
            os.replace(temporary, dest)
            temporary = None
            self.pending_removals[source] = (dest, digest)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.remove(temporary)
        return filename

    def excluded_legacy_path(self, path):
        """不从已弃用 pics 或其他游戏目录导入文件、别名或下载 URL。"""
        current = os.path.basename(self.directory).lower()
        if current not in {"mtg-pics", "ptcg-pics"}:
            return False
        excluded = {"pics", "ptcg-pics", "mtg-pics"} - {current}
        # 同时支持其他机器记录的 Windows 路径与当前机器路径。
        parts = str(path or "").replace("\\", "/").lower().split("/")
        return any(part in excluded for part in parts)

    def migrate_legacy(self):
        """先复制旧图片，再提交清单和索引，最后验证并清理源图片。"""
        manifests = []
        direct = os.path.join(self.root, "manifest.json")
        if os.path.isfile(direct):
            manifests.append(direct)
        for dirname in sorted(os.listdir(self.root)):
            if re.fullmatch(r"\d{6}-\d{6}(?:_\d+)?", dirname):
                path = os.path.join(self.root, dirname, "manifest.json")
                if os.path.isfile(path):
                    manifests.append(path)
        moved, imported = 0, 0
        migrated = set(self.data["migrated"])
        referenced, committed, blocked_directories = set(), set(), set()
        for path in manifests:
            identity = os.path.relpath(path, self.root)
            if identity in migrated:
                continue
            directory = os.path.join(os.path.dirname(path), "cards")
            sources, renames = set(), {}
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    manifest = json.load(stream)
                if self.excluded_legacy_path(manifest.get("out_dir")) or any(
                        self.excluded_legacy_path(record.get("file"))
                        for entry in manifest.get("cards", []) for record in entry.get("files", [])):
                    blocked_directories.add(directory)
                    _query_log("跳过旧 pics 或另一游戏的历史清单（保留整个任务）：%s" % path)
                    continue
                changed = manifest.get("out_dir") != self.directory
                for entry in manifest.get("cards", []):
                    if not entry.get("files"):
                        continue
                    valid = True
                    for record in entry["files"]:
                        original_name = os.path.basename(record.get("file", ""))
                        filename = original_name
                        source = os.path.join(directory, filename)
                        referenced.add(source)
                        if os.path.isfile(source):
                            target = self._move_legacy_image(source)
                            if target is None:
                                valid = False
                                continue
                            filename = target
                            sources.add(source)
                            moved += 1
                            changed = True
                            if filename != original_name:
                                renames[original_name] = filename
                        dest = self.image_path(filename)
                        if not complete_image(dest) and not record.get("url"):
                            valid = False
                            continue
                        if record.get("file") != dest:
                            changed = True
                        record["file"] = dest
                        if complete_image(dest):
                            record.update(status="exists", bytes=os.path.getsize(dest))
                        else:
                            record.update(status="pending")
                            record.pop("bytes", None)
                    if valid:
                        for record in entry["files"]:
                            self.remember_image(os.path.basename(record["file"]), record.get("url"), record.get("bytes"))
                        if self.key(entry) not in self.preexisting_keys:
                            self.store(entry)
                            imported += 1
                manifest["out_dir"] = self.directory
                if changed:
                    atomic_json(path, manifest)
                committed.update(sources)
                text_path = os.path.join(os.path.dirname(path), "manifest.txt")
                if renames and os.path.isfile(text_path):
                    with open(text_path, "r", encoding="utf-8") as stream:
                        lines = stream.read().splitlines()
                    lines = [line[:len(line) - len(line.lstrip())] + renames[line.strip()]
                             if line.strip() in renames else line for line in lines]
                    with open(text_path, "w", encoding="utf-8") as stream:
                        stream.write("\n".join(lines) + "\n")
                migrated.add(identity)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                blocked_directories.add(directory)
                _query_log("旧任务图片迁移未完成，将在下次重试：%s | %s" % (path, exc))
        self.data["migrated"] = sorted(migrated)
        directories = [os.path.join(self.root, "cards")]
        directories.extend(os.path.join(self.root, name, "cards") for name in os.listdir(self.root)
                           if re.fullmatch(r"\d{6}-\d{6}(?:_\d+)?", name))
        for directory in directories:
            if not os.path.isdir(directory) or directory in blocked_directories:
                continue
            for filename in os.listdir(directory):
                source = os.path.join(directory, filename)
                if source in referenced:
                    continue
                if os.path.isfile(source) and filename.lower().endswith((".png", ".jpg", ".jpeg")):
                    target = self._move_legacy_image(source)
                    if target:
                        self.remember_image(target, "", os.path.getsize(self.image_path(target)))
                        committed.add(source)
                        moved += 1
        self.save()
        for source in committed:
            pending = self.pending_removals.get(source)
            if not pending or not os.path.isfile(source):
                continue
            dest, digest = pending
            # 删除前重新核对绝对路径和两端内容，源图片变化时保留它。
            if os.path.commonpath([os.path.realpath(self.root), os.path.realpath(source)]) != os.path.realpath(self.root):
                continue
            if self.file_digest(source) == digest and self.file_digest(dest) == digest:
                os.remove(source)
        for directory in directories:
            if os.path.isdir(directory) and not os.path.islink(directory) and not os.listdir(directory):
                os.rmdir(directory)
        if moved or imported:
            _query_log("共享图片迁移：%d 个旧图片文件归入当前缓存目录，导入 %d 条牌名记录。" % (moved, imported))
        return {"moved": moved, "imported": imported, "images": len(self.data["images"]), "cards": len(self.data["cards"])}


# =========================================================================== 关联 token


def inspect_token_parts(*cards):
    """只检查主卡及选中打印，不沿 token 的反向关系递归。"""
    ids, sources, texts = [], [], []
    for card in cards:
        if not card:
            continue
        if card.get("id") and card["id"] not in sources:
            sources.append(card["id"])
        texts.append(card.get("oracle_text") or "")
        texts.extend(face.get("oracle_text") or "" for face in card.get("card_faces") or [])
        for part in card.get("all_parts") or []:
            if part.get("component") == "token" and part.get("id"):
                if part["id"] not in ids:
                    ids.append(part["id"])
    possible = any(re.search(r"\b(?:create|creates|put)\b[^.]*\btokens?\b", text, re.I)
                   for text in texts)
    status = "related" if ids else ("rules_only" if possible else "none")
    return {"checked": True, "status": status, "source_ids": sources,
            "related_ids": ids, "possible_creation": possible}


def cached_print_id(entry):
    if entry.get("selected_id"):
        return entry["selected_id"]
    # 只信任 Scryfall 图片域名和 UUID 路径，不猜测自制图的关系。
    for record in entry.get("files", []):
        url = urllib.parse.urlparse(record.get("url") or "")
        if url.hostname not in ("cards.scryfall.io", "c1.scryfall.com", "c2.scryfall.com"):
            continue
        match = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?:png|jpg)$", url.path, re.I)
        if match:
            return match.group(1)
    return None


def ensure_token_check(entry):
    check = entry.get("token_check") or {}
    if check.get("checked") or check.get("status") == "unavailable":
        return
    card_id = cached_print_id(entry)
    if not card_id:
        entry["token_check"] = {"checked": False, "status": "unavailable", "related_ids": [],
                                "error": "无可靠打印 ID，无法自动检查关联 token"}
        return
    try:
        card = api_get("/cards/" + urllib.parse.quote(card_id, safe=""), cache_missing=False)
        if not card or card.get("id") != card_id:
            raise RuntimeError("关联检查未取得指定打印对象")
        entry["token_check"] = inspect_token_parts(card)
    except Exception as exc:
        entry["token_check"] = {"checked": False, "status": "error", "related_ids": [],
                                "source_ids": [card_id], "error": str(exc)}


def token_identity(card):
    oracle = card.get("oracle_id")
    return "token:oracle:" + oracle if oracle else "token:card:" + card["id"]


def query_token(card_id, langs):
    card = api_get("/cards/" + urllib.parse.quote(card_id, safe=""), cache_missing=False)
    if not card or card.get("id") != card_id:
        raise RuntimeError("未取得指定关联 token 对象：" + card_id)
    identity = token_identity(card)
    oracle = card.get("oracle_id")
    selected = None
    if oracle:
        # 不使用名称搜索；所有候选再次核对 oracle 身份与实际图片。
        for lang in langs:
            candidates = [c for c in prints_for(oracle, "lang:%s" % lang)
                          if c.get("oracle_id") == oracle and c.get("lang") == lang
                          and is_usable_image(c) and image_jobs(c)]
            if card.get("lang") == lang and is_usable_image(card) and image_jobs(card):
                candidates.append(card)
            if candidates:
                selected = pick_print_in_normal_set(candidates, newest=(lang != "en"))
                break
    if selected is None and is_usable_image(card) and image_jobs(card):
        selected = card
    if selected is None:
        raise RuntimeError("关联 token 没有可用图片：" + card_id)
    return card, selected, identity


def token_entry(card, selected, identity, cache, dry_run=False, force=False):
    names = face_names(selected)
    full_name = safe_name(full_display_name(names) or card.get("name"))
    entry = {"kind": "token", "cache_identity": identity, "related_ids": [card["id"]],
             "input": card.get("name") or card["id"], "qty": 1,
             "english_name": card.get("name"), "display_name": full_name,
             "oracle_id": card.get("oracle_id"), "selected_id": selected.get("id"),
             "lang": LANG_LABEL.get(selected.get("lang"), selected.get("lang")),
             "lang_code": selected.get("lang"), "set_code": selected.get("set"),
             "set_name": selected.get("set_name"), "collector_number": selected.get("collector_number"),
             "layout": selected.get("layout"), "chinese_name": all(has_cjk(n) for n in names if n),
             "token_type": "emblem" if "emblem" in (card.get("type_line") or "").lower() else "token",
             "status": "dry-run" if dry_run else "exists", "files": []}
    for label, uris in image_jobs(selected):
        url = uris.get("png") or uris.get("large") or uris.get("normal")
        if not url:
            raise RuntimeError("关联 token 牌面没有可用图片 URL")
        fmt = "png" if uris.get("png") else "jpg"
        base = full_name if not label else full_name + "_" + label
        path = cache.image_path(cache.allocate(base, fmt, url))
        record = {"label": label, "url": url, "format": fmt, "file": path}
        if dry_run:
            record["status"] = "dry-run"
        elif not force and complete_image(path):
            record.update(status="exists", bytes=os.path.getsize(path))
        else:
            record["status"] = "pending"
        entry["files"].append(record)
    return entry


def collect_tokens(results, queried, cache, langs, dry_run=False, force=False):
    tokens, by_id, by_identity, checks = [], {}, {}, {}
    for entry in results:
        query = queried.get(entry["input"], {})
        if not entry.get("files") and "token_check" not in query:
            continue
        if "token_check" in query:
            entry["token_check"] = copy.deepcopy(query["token_check"])
        else:
            key = cached_print_id(entry) or cache.key(entry)
            if key in checks:
                entry["token_check"] = copy.deepcopy(checks[key])
            else:
                ensure_token_check(entry)
                checks[key] = copy.deepcopy(entry.get("token_check"))
        check = entry.get("token_check") or {}
        entry["token_refs"] = []
        for card_id in check.get("related_ids", []):
            if card_id not in by_id:
                try:
                    token = None if force else cache.lookup_token(card_id)
                    if token is None:
                        card, selected, identity = query_token(card_id, langs)
                        token = by_identity.get(identity)
                        if token is None:
                            stored = cache.data["tokens"].get(identity)
                            token = cache.restore(stored) if stored and not force else None
                            if token is None:
                                token = token_entry(card, selected, identity, cache, dry_run, force)
                        if card_id not in token["related_ids"]:
                            token["related_ids"].append(card_id)
                    token["qty"] = 1
                    identity = token["cache_identity"]
                    if identity in by_identity:
                        existing = by_identity[identity]
                        existing["related_ids"] = list(dict.fromkeys(existing["related_ids"] + token["related_ids"]))
                        token = existing
                    else:
                        if dry_run:
                            token["status"] = "dry-run"
                            for record in token["files"]:
                                record["status"] = "dry-run"
                        by_identity[identity] = token
                        tokens.append(token)
                    by_id[card_id] = token
                except Exception as exc:
                    token = {"kind": "token", "cache_identity": "token:card:" + card_id,
                             "related_ids": [card_id], "input": card_id, "qty": 1,
                             "status": "error", "error": str(exc), "files": []}
                    by_id[card_id] = token
                    tokens.append(token)
            entry["token_refs"].append(by_id[card_id]["cache_identity"])
        entry["token_refs"] = list(dict.fromkeys(entry["token_refs"]))
        message = {"none": "已检查，无关联 token 或创建迹象", "rules_only": "规则可能创建 token，但没有可下载关联对象",
                   "unavailable": "无可靠打印 ID，无法自动检查", "error": "关联查询失败"}.get(check.get("status"))
        print("  [Token] %s：%s" % (entry["input"], message or ", ".join(entry["token_refs"])), flush=True)
        if check.get("error"):
            print("    " + check["error"], flush=True)
    for token in tokens:
        print("  [Token] %s | %s%s" % (token.get("display_name", token["input"]), token["status"],
              " | 命中缓存" if token.get("cache_hit") else ""), flush=True)
    return tokens


# =========================================================================== PDF

def decode_png_rgb(path):
    """把 PNG 解码为 RGB 字节流（丢弃 alpha），返回 (宽, 高, bytes)。"""
    with open(path, "rb") as fh:
        blob = fh.read()
    pos = 8
    idat = bytearray()
    width = height = depth = ctype = None
    while pos + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if depth != 8 or ctype not in (2, 6):
        raise ValueError("暂只支持 8 位 RGB/RGBA 的 PNG：depth=%s ctype=%s" % (depth, ctype))

    channels = 3 if ctype == 2 else 4
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    rgb = bytearray(width * height * 3)
    prev = bytearray(stride)
    src = 0
    dst = 0

    for _y in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ftype == 1:            # Sub
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif ftype == 2:          # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:          # Average
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:          # Paeth
            for x in range(stride):
                if x >= channels:
                    a = line[x - channels]
                    c = prev[x - channels]
                else:
                    a = c = 0
                b = prev[x]
                p = a + b - c
                pa = p - a
                if pa < 0:
                    pa = -pa
                pb = p - b
                if pb < 0:
                    pb = -pb
                pc = p - c
                if pc < 0:
                    pc = -pc
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[x] = (line[x] + pr) & 0xFF
        if channels == 4:
            row = bytearray(width * 3)
            row[0::3] = line[0::4]
            row[1::3] = line[1::4]
            row[2::3] = line[2::4]
            rgb[dst:dst + width * 3] = row
        else:
            rgb[dst:dst + width * 3] = line
        dst += width * 3
        prev = line
    return width, height, bytes(rgb)


def jpeg_info(path):
    """读取 JPEG 的 SOF 段，返回 (宽, 高, 原始字节, 颜色分量数)；不解码像素。"""
    with open(path, "rb") as fh:
        blob = fh.read()
    if not blob.startswith(b"\xff\xd8"):
        raise ValueError("不是 JPEG 文件")
    pos = 2
    while pos + 4 <= len(blob):
        if blob[pos] != 0xFF:
            raise ValueError("JPEG 标记损坏")
        marker = blob[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack(">H", blob[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            precision, height, width, components = struct.unpack(">BHHB", blob[pos + 4:pos + 10])
            if precision != 8 or components not in (1, 3, 4) or not width or not height:
                raise ValueError("暂只支持 8 位灰度/RGB/CMYK JPEG")
            return width, height, blob, components
        pos += 2 + length
    raise ValueError("JPEG 缺少尺寸信息")


def pack_rgb(width, height, rgb):
    """按 Up 差分重新打包并压缩，配合 PDF 的 Predictor 15。"""
    packed = bytearray()
    prev = bytes(width * 3)
    for y in range(height):
        row = rgb[y * width * 3:(y + 1) * width * 3]
        packed.append(2)
        packed += bytes((a - b) & 0xFF for a, b in zip(row, prev))
        prev = row
    return zlib.compress(bytes(packed), 6)


def build_pdf(images, paper, out_path, cut_marks=True):
    """images: [(宽, 高, flate)]，返回 (页数, 每页张数, 列, 行)。"""
    pw_mm, ph_mm = PAPERS[paper]
    pw, ph = pw_mm * MM, ph_mm * MM
    cw, ch = CARD_W_MM * MM, CARD_H_MM * MM
    cols = max(1, int(pw_mm // CARD_W_MM))
    rows = max(1, int(ph_mm // CARD_H_MM))
    per_page = cols * rows
    margin_x = (pw - cols * cw) / 2.0
    margin_y = (ph - rows * ch) / 2.0

    objects = [None, None]        # 1 = Catalog, 2 = Pages
    page_ids = []
    pages = [images[i:i + per_page] for i in range(0, len(images), per_page)]

    for page_cards in pages:
        img_ids = []
        for image in page_cards:
            w, h, data = image[:3]
            components = image[3] if len(image) > 3 else None
            if components:
                # JPEG 原样嵌入，由 PDF 阅读器按 DCTDecode 解码。
                space = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[components]
                decode = " /Decode [1 0 1 0 1 0 1 0]" if components == 4 else ""
                header = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                          "/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode%s "
                          "/Length %d >>\nstream\n" % (w, h, space, decode, len(data)))
            else:
                header = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                          "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                          "/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns %d >> "
                          "/Length %d >>\nstream\n" % (w, h, w, len(data)))
            objects.append(header.encode("ascii") + data + b"\nendstream")
            img_ids.append(len(objects))

        ops = []
        if cut_marks:
            ops.append("0.5 w 0.75 G")
            for idx in range(len(page_cards)):
                col = idx % cols
                row = idx // cols
                x = margin_x + col * cw
                y = ph - margin_y - (row + 1) * ch
                ops.append("%.2f %.2f %.2f %.2f re S" % (x, y, cw, ch))
            ops.append("0 G")
        for idx in range(len(page_cards)):
            col = idx % cols
            row = idx // cols
            x = margin_x + col * cw
            y = ph - margin_y - (row + 1) * ch
            ops.append("q %.2f 0 0 %.2f %.2f %.2f cm /Im%d Do Q" % (cw, ch, x, y, idx))
        content = ("\n".join(ops) + "\n").encode("ascii")
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        content_id = len(objects)

        xobj = " ".join("/Im%d %d 0 R" % (i, img_ids[i]) for i in range(len(img_ids)))
        objects.append(
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
             "/Resources << /XObject << %s >> >> /Contents %d 0 R >>"
             % (pw, ph, xobj, content_id)).encode("ascii"))
        page_ids.append(len(objects))

    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join("%d 0 R" % pid for pid in page_ids)
    objects[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))).encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % (index + 1) + body + b"\nendobj\n"
    xref_pos = len(out)
    total = len(objects) + 1
    out += b"xref\n0 %d\n" % total
    out += b"0000000000 65535 f \n"
    for index in range(1, total):
        out += b"%010d 00000 n \n" % offsets[index]
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (total, xref_pos))
    with open(out_path, "wb") as fh:
        fh.write(bytes(out))
    return len(pages), per_page, cols, rows


def make_pdf(paths, paper, out_path, cut_marks=True):
    """把卡图按万智牌标准尺寸排成卡册。"""
    entries = []
    total = len(paths)
    started = time.time()
    for index, path in enumerate(paths, 1):
        sys.stdout.write("\r  排版卡册: %d/%d 已处理" % (index - 1, total))
        sys.stdout.flush()
        try:
            if path.lower().endswith((".jpg", ".jpeg")):
                entries.append(jpeg_info(path))
            else:
                w, h, rgb = decode_png_rgb(path)
                entries.append((w, h, pack_rgb(w, h, rgb)))
        except Exception as exc:
            print("\n  跳过 %s：%s" % (os.path.basename(path), exc))
    sys.stdout.write("\r  排版卡册: %d/%d 完成\n" % (len(entries), total))
    sys.stdout.flush()
    if not entries:
        return None
    pages, per_page, cols, rows = build_pdf(entries, paper, out_path, cut_marks=cut_marks)
    return {
        "pages": pages, "per_page": per_page, "cols": cols, "rows": rows,
        "cards": len(entries), "paper": paper,
        "size_mb": os.path.getsize(out_path) / 1048576.0,
        "seconds": time.time() - started,
    }


# =========================================================================== 主流程

def program_cache_directory():
    executable = sys.executable if getattr(sys, "frozen", False) else __file__
    return os.path.join(os.path.dirname(os.path.abspath(executable)), ".scryfall-api-cache")


@contextmanager
def network_task_lock():
    """同一程序目录的多个窗口不能叠加任务，把每个窗口的频率相加。"""
    directory = program_cache_directory()
    os.makedirs(directory, exist_ok=True)
    stream = open(os.path.join(directory, "network-task.lock"), "a+b")
    locked = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError("另一个窗口正在执行任务，请等它结束后再开始，避免叠加 API 请求频率。")
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if "--help" in arguments or "-h" in arguments:
        return _main(arguments)
    with network_task_lock():
        return _main(arguments)


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="scryfall_cards_threaded",
        description="提交中英文牌表，自动取回 Scryfall 中文卡图并排出可打印的卡册 PDF。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="牌表每行一张，中英文均可；井号、双斜杠开头的行按注释忽略。")
    parser.add_argument("decklist", nargs="?", default="-",
                        help="牌表路径；- 表示从标准输入读取")
    parser.add_argument("--out", default="scryfall-cards",
                        help="输出目录（默认 ./scryfall-cards）")
    parser.add_argument("--game", choices=("mtg", "ptcg-zh", "ptcg-en"), default="mtg",
                        help="游戏：万智牌/宝可梦繁体中文/宝可梦英文")
    parser.add_argument("--pics", default=None,
                        help="图片目录；默认万智牌 mtg-pics，宝可梦 ptcg-pics/zh-tw 或 en")
    parser.add_argument("--langs", default="zhs,zht,en",
                        help="语言优先级，逗号分隔（默认 zhs,zht,en）")
    parser.add_argument("--paper", default="A4", choices=sorted(PAPERS),
                        help="卡册纸张（默认 A4 竖向每页 9 张；A3 横向每页 18 张）")
    parser.add_argument("--no-pdf", action="store_true", help="不生成卡册 PDF")
    parser.add_argument("--no-cut-marks", action="store_true", help="不在卡册上画裁切框")
    parser.add_argument("--dry-run", action="store_true", help="只解析与选图，不下载")
    parser.add_argument("--force", action="store_true", help="已存在的图片也重新下载")
    parser.add_argument("--workers", type=int, default=None,
                        help="查询线程数（万智牌默认4，范围1–16；宝可梦默认64，范围1–64）")
    parser.add_argument("--download-workers", type=int, default=None,
                        help="下载线程数（万智牌默认8，范围1–32；宝可梦默认64，范围1–64）")
    parser.add_argument("--image-interval", type=float, default=MIN_IMAGE_INTERVAL,
                        help="图片请求启动间隔秒数（默认 %g；0 取消固定间隔，不取消服务端冷却）" % MIN_IMAGE_INTERVAL)
    args = parser.parse_args(argv)
    if not math.isfinite(args.image_interval) or args.image_interval < 0:
        parser.error("--image-interval 必须是非负有限数")
    ptcg = args.game != "mtg"
    query_max = 64 if ptcg else MAX_WORKERS
    image_max = 64 if ptcg else MAX_DOWNLOAD_WORKERS
    if args.workers is None:
        args.workers = 64 if ptcg else DEFAULT_WORKERS
    if args.download_workers is None:
        args.download_workers = 64 if ptcg else DEFAULT_DOWNLOAD_WORKERS
    if not 1 <= args.workers <= query_max:
        parser.error("--workers 必须在 1 到 %d 之间" % query_max)
    if not 1 <= args.download_workers <= image_max:
        parser.error("--download-workers 必须在 1 到 %d 之间" % image_max)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True, errors="replace")

    global _api_disk_cache_dir, _image_interval, _exact_print_refresh
    _exact_print_refresh = args.force
    _reset_network_state()
    _image_interval = args.image_interval
    with _print_cache_lock:
        _print_cache.clear()
    _api_disk_cache_dir = program_cache_directory()
    langs = [item.strip() for item in args.langs.split(",") if item.strip()]
    if args.decklist == "-":
        if sys.stdin.isatty():
            parser.error("请指定牌表文件，例如：python scryfall_cards_threaded.py 牌表.txt")
        text = sys.stdin.read()
    else:
        with open(args.decklist, "r", encoding="utf-8-sig") as fh:
            text = fh.read()

    if ptcg:
        if "run_ptcg" in globals():
            return run_ptcg(args, text, sys.modules[__name__])
        from ptcg_engine import run_ptcg as ptcg_runner
        return ptcg_runner(args, text, sys.modules[__name__])
    if args.pics is None:
        args.pics = os.path.join(os.path.dirname(program_cache_directory()), "卡图输出", "mtg-pics")
    rows = parse_decklist(text)
    if not rows:
        print("牌表为空，未解析到任何卡名。")
        return 2

    os.makedirs(args.out, exist_ok=True)
    cards_dir = os.path.abspath(args.pics)
    picture_cache = PictureCache(cards_dir)
    picture_cache.migrate_legacy()

    print("牌表 %d 行 | 语言链 %s | 输出 %s" % (len(rows), " > ".join(langs), os.path.abspath(args.out)))
    print("网络策略：查询 %d 线程 | 下载 %d 线程 | API 请求间隔 %.2f 秒 | 图片启动间隔 %.2f 秒 | 查询缓存 24 小时" %
          (args.workers, args.download_workers, MIN_API_INTERVAL, args.image_interval), flush=True)
    cached = {}
    for name in dict.fromkeys(name for qty, name in rows):
        hit = None if args.force else picture_cache.lookup(name)
        if hit is not None:
            cached[name] = hit
    print("本地图片缓存：%d/%d 种牌命中 | 共享目录 %s" % (len(cached), len(set(name for qty, name in rows)), cards_dir), flush=True)
    query_rows = [(qty, name) for qty, name in rows if name not in cached]
    results = []
    queried = query_all(query_rows, langs, args.workers) if query_rows else {}
    if not query_rows:
        print("所有牌名已在本地图片缓存中，无需 API 查询。", flush=True)
    pdf_paths = []
    ok = failed = 0

    for index, (qty, name) in enumerate(rows, 1):
        entry = {"input": name, "qty": qty}
        spec = parse_print_spec(name)
        if spec:
            entry.update(printing_spec={"name": spec[0], "set": spec[1], "collector_number": spec[2]},
                         cache_identity=printing_identity(spec), set_code=spec[1], collector_number=spec[2])
        try:
            if name in cached:
                entry = copy.deepcopy(cached[name])
                entry.update(input=name, qty=qty, cache_hit=True)
                entry.pop("error", None)
                available = sum(record["status"] == "exists" for record in entry["files"])
                if args.dry_run:
                    for record in entry["files"]:
                        record["status"] = "dry-run"
                    entry["status"] = "dry-run"
                print("[缓存 %d/%d] %s -> %s（%d 张已有，%d 张待补下载）" %
                      (index, len(rows), name, entry.get("display_name"), available, len(entry["files"]) - available), flush=True)
                results.append(entry)
                continue
            query = queried[name]
            if "error" in query:
                raise RuntimeError(query["error"])
            resolved = query["resolved"]
            if not resolved:
                entry.update(status="not_found")
                failed += 1
                print("[%d/%d] 未找到：%s" % (index, len(rows), name))
                results.append(entry)
                continue

            oracle = resolved["oracle_id"]
            entry["oracle_id"] = oracle
            entry["english_name"] = (resolved.get("name") or "").strip()
            basic = is_basic_land(resolved)
            entry["basic_land"] = basic

            selected, used_lang = query["selected"], query["used_lang"]
            if not selected:
                entry.update(status="no_image")
                failed += 1
                print("[%d/%d] 无可用图：%s" % (index, len(rows), name))
                results.append(entry)
                continue

            jobs = query["jobs"]
            if not jobs:
                entry.update(status="no_image")
                failed += 1
                print("[%d/%d] 该打印无图片地址：%s" % (index, len(rows), name))
                results.append(entry)
                continue

            display, name_source = query["display"], query["name_source"]
            full_name = safe_name(full_display_name(display)) or safe_name(entry["english_name"])
            chinese_ok = all(has_cjk(n) for n in display if n)

            entry["aliases"] = list(dict.fromkeys([name, entry["english_name"], full_name] + display + face_names(resolved) + face_names(selected)))
            entry.update(
                selected_id=selected.get("id"),
                lang=used_lang,
                lang_code=selected.get("lang"),
                set_code=selected.get("set"),
                set_name=selected.get("set_name"),
                set_type=selected.get("set_type"),
                collector_number=selected.get("collector_number"),
                image_status=selected.get("image_status"),
                highres=bool(selected.get("highres_image")),
                layout=selected.get("layout"),
                display_name=full_name,
                name_source_set=name_source,
                chinese_name=chinese_ok,
                files=[],
            )

            for label, uris in jobs:
                url = uris.get("png") or uris.get("large") or uris.get("normal") or (uris.get("small") or uris.get("border_crop") if spec else None)
                if not url:
                    raise RuntimeError("该面没有可用的图片 URL")
                fmt = "png" if uris.get("png") else "jpg"
                base = full_name if not label else "%s_%s" % (full_name, label)
                filename = picture_cache.allocate(base, fmt, url)
                dest = os.path.join(cards_dir, filename)
                record = {"label": label, "file": dest, "format": fmt, "url": url}

                if args.dry_run:
                    record["status"] = "dry-run"
                elif complete_image(dest) and not args.force:
                    record["status"] = "exists"
                    record["bytes"] = os.path.getsize(dest)
                else:
                    record["status"] = "pending"
                entry["files"].append(record)

            entry["status"] = "dry-run" if args.dry_run else (
                "ok" if any(f["status"] == "ok" for f in entry["files"]) else "exists")

            print("[%d/%d] %s -> %s [%s %s #%s]" % (
                index, len(rows), name, full_name, entry["lang_code"],
                entry["set_code"], entry["collector_number"]))
            for record in entry["files"]:
                size = ("%.1f KB" % (record["bytes"] / 1024.0)) if "bytes" in record else "-"
                print("        %-42s %s [%s]" % (os.path.basename(record["file"]), size, record["status"]))
            if not chinese_ok:
                entry["note"] = "Scryfall 未提供完整中文名，文件名回退英文"
                print("        （未取得完整中文名）")
            ok += 1
        except Exception as exc:
            entry.update(status="error", error=str(exc))
            failed += 1
            print("[%d/%d] 出错：%s | %s" % (index, len(rows), name, exc))
        results.append(entry)

    tokens = collect_tokens(results, queried, picture_cache, langs, args.dry_run, args.force)
    all_results = results + tokens
    for entry in all_results:
        picture_cache.store(entry)
    picture_cache.save()
    if not args.dry_run:
        download_all(all_results, args.download_workers)
        for entry in all_results:
            for record in entry.get("files", []):
                if record["status"] in ("ok", "exists") and not complete_image(record["file"], record.get("bytes")):
                    record.update(status="error", error="图片不完整，保留选图记录以便下次补下载")
            picture_cache.store(entry)
        picture_cache.save()
    # 按牌表顺序排版，不受线程完成顺序影响。
    pdf_paths = []
    for entry in all_results:
        files = entry.get("files", [])
        if entry.get("status") != "error" and files:
            errors = [f.get("error", "下载失败") for f in files if f["status"] == "error"]
            if errors:
                entry.update(status="error", error="; ".join(errors))
            elif not args.dry_run:
                entry["status"] = "ok" if any(f["status"] == "ok" for f in files) else "exists"
        for record in files:
            if record["status"] in ("ok", "exists"):
                pdf_paths.extend([record["file"]] * entry["qty"])
    ok = sum(entry.get("status") in ("ok", "exists", "dry-run") for entry in results)
    failed = len(results) - ok
    token_ok = sum(entry.get("status") in ("ok", "exists", "dry-run") for entry in tokens)
    token_failed = len(tokens) - token_ok
    check_failed = sum(entry.get("token_check", {}).get("status") == "error" for entry in results)

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "decklist_rows": len(rows),
        "lang_chain": langs,
        "paper": args.paper,
        "out_dir": os.path.abspath(cards_dir),
        "summary": {"processed": len(results), "ok": ok, "failed": failed},
        "cards": results,
        "tokens": tokens,
        "token_summary": {"processed": len(tokens), "ok": token_ok, "failed": token_failed,
                          "check_failed": check_failed},
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    lines = ["# Scryfall 卡图清单", "",
             "生成时间: %s" % manifest["generated_at"],
             "牌表行数: %d  成功: %d  失败: %d" % (len(rows), ok, failed), ""]
    for entry in results:
        if entry.get("status") in ("ok", "exists", "dry-run"):
            lines.append("- %d x %s | 文件名: %s | %s %s #%s | %s%s" % (
                entry.get("qty", 1), entry["input"], entry.get("display_name"),
                entry.get("set_code"), entry.get("set_name"), entry.get("collector_number"),
                entry.get("lang"), "" if entry.get("chinese_name") else " | 无中文名"))
            for record in entry.get("files") or []:
                lines.append("    %s" % os.path.basename(record["file"]))
        else:
            lines.append("- %s | !! %s | %s" % (entry["input"], entry.get("status"), entry.get("error", "")))
    lines.extend(["", "# 关联 token / 徽记", "成功: %d  失败: %d  关联检查失败: %d" % (token_ok, token_failed, check_failed)])
    for entry in results:
        check = entry.get("token_check") or {}
        lines.append("- %s -> %s | %s%s" % (entry["input"], ", ".join(entry.get("token_refs", [])) or "无可下载关联对象",
                     check.get("status", "未检查"), " | " + check["error"] if check.get("error") else ""))
        if check.get("status") == "rules_only":
            lines.append("    规则可能创建 token，但没有可下载关联对象；不推测名称或数量。")
    for token in tokens:
        lines.append("- %s | %s | %s | %s%s" % (token.get("display_name", token["input"]),
                     token.get("token_type", "token"), token.get("lang", "未知语种"), token["status"],
                     " | " + token["error"] if token.get("error") else ""))
        lines.extend("    %s [%s]" % (os.path.basename(record["file"]), record["status"]) for record in token.get("files", []))
    with open(os.path.join(args.out, "manifest.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n完成：成功 %d / 失败 %d" % (ok, failed))
    print("卡图目录: %s（每个牌名只存一张）" % os.path.abspath(cards_dir))
    print("清单    : %s" % os.path.abspath(os.path.join(args.out, "manifest.txt")))
    if not args.dry_run and pdf_paths:
        print("卡册张数: %d 张（已按牌表数量复制）" % len(pdf_paths))

    if not args.no_pdf and not args.dry_run and pdf_paths:
        pdf_path = os.path.join(args.out, "cards.pdf")
        info = make_pdf(pdf_paths, args.paper, pdf_path, cut_marks=not args.no_cut_marks)
        if info:
            print("卡册    : %s" % os.path.abspath(pdf_path))
            print("          %s 纸，每页 %d 张（%d 列 x %d 行），共 %d 页，%.1f MB，用时 %.0f 秒" % (
                info["paper"], info["per_page"], info["cols"], info["rows"],
                info["pages"], info["size_mb"], info["seconds"]))
    print("Token 完成：成功 %d / 失败 %d / 关联检查失败 %d" % (token_ok, token_failed, check_failed))
    return 0 if failed + token_failed + check_failed == 0 else 1



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
        norm = lambda s: re.sub(r'\s+', ' ', s).strip().replace('’', "'").casefold()
        allowed = {norm(actual_name)}
        if self.language == 'en' and card.get('category') == 'Energy' and card.get('set', {}).get('id') == 'sve':
            allowed.add(norm('Basic ' + actual_name))
        if norm(name) not in allowed:
            raise PTCGError('name_mismatch', '指定 %s %s 的名称为“%s”，不是“%s”' % (code, number, actual_name, name))
        image = card.get('image')
        if not isinstance(image, str) or not image:
            raise PTCGError('missing_image', '卡牌 %s 有资料但尚无图片；不会替换成其他版本' % card_id)
        parsed = urllib.parse.urlparse(image)
        if parsed.scheme != 'https' or parsed.hostname != 'assets.tcgdex.net' or parsed.path.split('/')[1:2] != [self.language]:
            raise PTCGError('invalid_image', 'TCGdex 图片地址无效或语言不一致')
        return card, image.rstrip('/') + '/high.png'


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
        if not stored or stored.get('input_name') != name:
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
            entry.update(qty=qty, cache_hit=True)
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
                         chinese_name=language == 'zh-tw', status='pending')
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


# GUI layer appended to the existing standalone download engine.
import queue
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# TCGdex 未公布硬性请求或并发限额；PTCG 值为本机实测择优预设。
# 2026-10-13：同样样本比较16/32/64，查询64、下载64吞吐最高。
# 详见 tcgdex-benchmark-results/report.md；图片实测使用120秒socket超时。
# https://tcgdex.dev/faq#are-there-rate-limits
GAME_PRESETS = {
    "万智牌": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "宝可梦TCG（中文）": (64, 64),
    "宝可梦TCG（英文）": (64, 64),
    "游戏王（简中）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "游戏王（日文）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "游戏王（TCG）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
}
GAME_CODES = dict(zip(GAME_PRESETS, ("mtg", "ptcg-zh", "ptcg-en", "ygo-zh", "ygo-ja", "ygo-tcg")))
GAME_PICTURE_DIRECTORIES = {
    "万智牌": "mtg-pics",
    "宝可梦TCG（中文）": "ptcg-pics/zh-tw",
    "宝可梦TCG（英文）": "ptcg-pics/en",
}
PTCG_LANGUAGES = {"ptcg-zh": "zh-tw", "ptcg-en": "en"}
FIXED_LANGUAGE_LABELS = {
    "ptcg-zh": "繁体中文（zh-tw，固定，不回退）",
    "ptcg-en": "英文（en，固定，不回退）",
    "ygo-zh": "简体中文（占位）",
    "ygo-ja": "日文（占位）",
    "ygo-tcg": "TCG（占位）",
}

LANG_PRESETS = {
    "简体 → 繁体 → 英文（默认）": "zhs,zht,en",
    "繁体 → 简体 → 英文": "zht,zhs,en",
    "英文 → 简体 → 繁体": "en,zhs,zht",
    "简体 → 英文": "zhs,en",
    "繁体 → 英文": "zht,en",
}


def program_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def create_task_directory(output_root):
    """按任务启动的本地时间创建目录，原子创建防止多窗口同秒冲突。"""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%y%m%d-%H%M%S")
    sequence = 0
    while True:
        name = stamp if sequence == 0 else "%s_%02d" % (stamp, sequence)
        candidate = output_root / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            sequence += 1


def engine_command(deck, output, workers, paper, langs, pdf, marks, dry_run, force,
                   download_workers=DEFAULT_DOWNLOAD_WORKERS, game='mtg'):
    if game not in ('mtg', 'ptcg-zh', 'ptcg-en'):
        raise ValueError("该游戏尚未接入下载接口。")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--engine"]
    else:
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
            executable = executable.with_name("python.exe")
        command = [str(executable), "-u", str(Path(__file__).resolve()), "--engine"]
    pics = Path(output).parent / ("mtg-pics" if game == 'mtg' else "ptcg-pics")
    if game in PTCG_LANGUAGES:
        pics /= PTCG_LANGUAGES[game]
    command += [str(deck), "--out", str(output), "--pics", str(pics),
                "--game", game, "--workers", str(workers),
                "--download-workers", str(download_workers), "--paper", paper]
    if game == 'mtg':
        command += ["--langs", langs]
    if not pdf:
        command.append("--no-pdf")
    if not marks:
        command.append("--no-cut-marks")
    if dry_run:
        command.append("--dry-run")
    if force:
        command.append("--force")
    return command


class CardDownloaderGUI:
    def __init__(self, root, base_dir=None):
        self.root = root
        self.base_dir = Path(base_dir) if base_dir else program_directory()
        self.output_root = self.base_dir / "卡图输出"
        self.output_dir = self.output_root
        self.events = queue.Queue()
        self.process = None
        self.process_lock = threading.Lock()
        self.running = False
        self.closing = False
        self.cancel_requested = threading.Event()
        self.summary = None
        self.token_summary = None
        self.download_total = 0
        self.download_finished = 0
        self.last_pdf_allowed = False
        self.previous_pdf_stamp = None
        self.poll_id = None
        self.edit_controls = []

        root.title("Scryfall 卡图下载器")
        root.geometry("1080x820")
        root.minsize(920, 740)
        root.configure(bg="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self.game = tk.StringVar(value="万智牌")
        self.network_policy = tk.StringVar()
        self.game.trace_add("write", self._on_game_changed)
        self._on_game_changed()
        self.paper = tk.StringVar(value="A4 · 竖向 · 每页 9 张")
        self.language = tk.StringVar(value=next(iter(LANG_PRESETS)))
        self.pdf = tk.BooleanVar(value=True)
        self.cut_marks = tk.BooleanVar(value=True)
        self.dry_run = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪：粘贴牌表，或导入 TXT 文件。")
        self.count = tk.StringVar(value="0 种牌 · 0 张")
        self.detail = tk.StringVar(value="自动处理网络请求，受限时等待并重试。")
        self.output_label = tk.StringVar(value=str(self.output_root / "yymmdd-hhmmss"))
        self.pics_label = tk.StringVar(value=str(self.picture_directory()))
        self._build()
        self.poll_id = root.after(80, self.poll_events)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10), background="#f3f5f8", foreground="#243248")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground="#65748a")
        style.configure("Card.TLabel", background="white")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"), background="white")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Primary.TButton", background="#2563eb", foreground="white", font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#b9c7df")], foreground=[("disabled", "#f3f5f8")])
        style.configure("TCheckbutton", font=("Microsoft YaHei UI", 10), background="white")
        style.configure("TCombobox", padding=5, font=("Microsoft YaHei UI", 10))
        style.configure("Horizontal.TProgressbar", background="#2563eb", troughcolor="#e3e9f2", borderwidth=0)

    def _editable(self, widget, normal="normal"):
        self.edit_controls.append((widget, normal))
        return widget

    def _build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Scryfall 卡图下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="粘贴中英文牌表，下载高清卡图，生成可打印卡册。", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 14))
        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2, minsize=340)
        content.rowconfigure(0, weight=1)
        left = ttk.Frame(content, style="Card.TFrame", padding=16)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right_shell = ttk.Frame(content, style="Card.TFrame")
        right_shell.grid(row=0, column=1, sticky="nsew")
        actions = ttk.Frame(right_shell, style="Card.TFrame", padding=(16, 8))
        actions.pack(side="bottom", fill="x")
        self.options_canvas = tk.Canvas(right_shell, bg="white", highlightthickness=0, width=340)
        options_scroll = ttk.Scrollbar(right_shell, command=self.options_canvas.yview)
        self.options_canvas.configure(yscrollcommand=options_scroll.set)
        options_scroll.pack(side="right", fill="y")
        self.options_canvas.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(self.options_canvas, style="Card.TFrame", padding=16)
        options_window = self.options_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda event: self.options_canvas.configure(scrollregion=self.options_canvas.bbox("all")))
        self.options_canvas.bind("<Configure>", lambda event: self.options_canvas.itemconfigure(options_window, width=event.width))
        def scroll_options(event):
            widget = event.widget
            if isinstance(widget, (ttk.Combobox, ttk.Spinbox)):
                return
            while widget is not None:
                if widget is right or widget is self.options_canvas:
                    self.options_canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind_all("<MouseWheel>", scroll_options, add="+")
        ttk.Label(left, text="牌表粘贴框", style="Section.TLabel").pack(anchor="w")
        tools = ttk.Frame(left, style="Card.TFrame")
        tools.pack(fill="x", pady=(10, 10))
        for label, action in [("导入 TXT", self.import_deck), ("粘贴", self.paste_deck), ("示例", self.example), ("清空", self.clear_deck)]:
            self._editable(ttk.Button(tools, text=label, command=action)).pack(side="left", padx=(0, 5))
        text_frame = ttk.Frame(left, style="Card.TFrame")
        text_frame.pack(fill="both", expand=True)
        self.deck_text = tk.Text(text_frame, wrap="word", undo=True, font=("Microsoft YaHei UI", 11),
                                 bg="#f7f9fc", fg="#243248", insertbackground="#2563eb", relief="flat", padx=12, pady=10, width=45, height=13)
        self._editable(self.deck_text)
        scrollbar = ttk.Scrollbar(text_frame, command=self.deck_text.yview)
        self.deck_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.deck_text.pack(side="left", fill="both", expand=True)
        self.deck_text.bind("<<Modified>>", self._on_modified)
        ttk.Label(left, textvariable=self.count, style="Card.TLabel").pack(anchor="w", pady=(8, 3))
        ttk.Label(left, text="MTG：数量 卡名，可加系列代码 编号锁定版本。\nPTCG：数量 卡名 系列代码 编号（必填）。\n游戏王占位支持纯名称行；数量用于 PDF 排版。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        ttk.Label(right, text="卡牌游戏", style="Section.TLabel").pack(anchor="w")
        self.game_box = self._editable(ttk.Combobox(
            right, textvariable=self.game, values=tuple(GAME_PRESETS), state="readonly"), "readonly")
        self.game_box.pack(fill="x", pady=(4, 6))
        ttk.Label(right, textvariable=self.network_policy, style="Card.TLabel",
                  wraplength=300, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 14))
        ttk.Label(right, text="下载与排版", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(right, text="PDF 纸张", style="Card.TLabel").pack(anchor="w")
        self.paper_box = self._editable(ttk.Combobox(right, textvariable=self.paper, state="readonly",
                              values=("A4 · 竖向 · 每页 9 张", "A3 · 横向 · 每页 18 张")), "readonly")
        self.paper_box.pack(fill="x", pady=(4, 10))
        ttk.Label(right, text="语言 / MTG 优先顺序", style="Card.TLabel").pack(anchor="w")
        self.language_box = self._editable(ttk.Combobox(right, textvariable=self.language, values=tuple(LANG_PRESETS)))
        self.language_box.pack(fill="x", pady=(4, 3))
        ttk.Label(right, text="MTG 按语言顺序选图；PTCG 语种固定，不回退。\n已有缓存直接复用；换版本时勾选“重新选图”。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))
        ttk.Label(right, text="自动处理网络请求，受限时等待并重试。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))
        self.pdf_check = self._editable(ttk.Checkbutton(right, text="生成可打印 PDF 卡册", variable=self.pdf, command=self.sync_options))
        self.pdf_check.pack(anchor="w", pady=2)
        self.mark_check = self._editable(ttk.Checkbutton(right, text="显示裁切框", variable=self.cut_marks))
        self.mark_check.pack(anchor="w", pady=2)
        self.dry_check = self._editable(ttk.Checkbutton(right, text="仅查询选图（不下载、不生成 PDF）", variable=self.dry_run, command=self.sync_options))
        self.dry_check.pack(anchor="w", pady=2)
        self.force_check = self._editable(ttk.Checkbutton(right, text="重新选图并下载（忽略图片缓存）", variable=self.force))
        self.force_check.pack(anchor="w", pady=2)
        ttk.Separator(right).pack(fill="x", pady=10)
        ttk.Label(right, text="输出位置 · 按任务时间戳隔离", style="Card.TLabel", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        self.path_display = ttk.Label(right, textvariable=self.output_label, style="Card.TLabel", wraplength=320, font=("Microsoft YaHei UI", 9), foreground="#65748a")
        self.path_display.pack(fill="x", pady=(4, 8))
        ttk.Label(right, text="图片缓存 · 重复牌名跳过查询和下载", style="Card.TLabel", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.pics_label, style="Card.TLabel", wraplength=320, font=("Microsoft YaHei UI", 9), foreground="#65748a").pack(fill="x", pady=(4, 8))
        self.start_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self.start)
        self.start_button.pack(fill="x", pady=(0, 5))
        action_row = ttk.Frame(actions, style="Card.TFrame")
        action_row.pack(fill="x")
        self.cancel_button = ttk.Button(action_row, text="停止任务", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(action_row, text="打开输出文件夹", command=self.open_output).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="打开当前游戏卡图", command=self.open_pics).pack(fill="x", pady=(5, 0))

        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(14, 8))
        ttk.Label(progress_frame, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=6)
        ttk.Label(progress_frame, textvariable=self.detail, style="Subtitle.TLabel").pack(anchor="w")
        log_bar = ttk.Frame(outer)
        log_bar.pack(fill="x")
        ttk.Label(log_bar, text="运行日志").pack(side="left")
        self.pdf_button = ttk.Button(log_bar, text="打开 PDF", command=self.open_pdf, state="disabled")
        self.pdf_button.pack(side="right", padx=(5, 0))
        ttk.Button(log_bar, text="复制日志", command=self.copy_log).pack(side="right")
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", pady=(5, 0))
        self.log_text = tk.Text(log_frame, state="disabled", height=8, wrap="word", font=("Consolas", 9),
                                bg="#172338", fg="#d7e3f5", relief="flat", padx=10, pady=8)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.sync_options()

    def game_code(self):
        try:
            return GAME_CODES[self.game.get()]
        except KeyError:
            raise ValueError("请选择列表中的卡牌游戏。") from None

    def deck_rows(self, text):
        game = self.game_code()
        if game in PTCG_LANGUAGES:
            return parse_ptcg_decklist(text)
        if game == 'mtg':
            return parse_decklist(text)
        # 游戏王支持纯名称行；不能套用 MTG/PTCG 的数字行过滤。
        rows = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "//", ";")):
                continue
            match = re.fullmatch(r"(\d+)\s+(.+)", line)
            rows.append((int(match.group(1)), match.group(2)) if match else (1, line))
        return rows

    def update_count(self):
        try:
            rows = self.deck_rows(self.deck_text.get("1.0", "end-1c"))
            self.count.set("%d 种牌 · %d 张" % (len({tuple(row[1:]) for row in rows}), sum(row[0] for row in rows)))
        except ValueError as exc:
            self.count.set("牌表格式错误：%s" % exc)

    def _on_modified(self, event=None):
        if self.deck_text.edit_modified():
            self.update_count()
            self.deck_text.edit_modified(False)

    def set_deck(self, text):
        self.deck_text.delete("1.0", "end")
        self.deck_text.insert("1.0", text)
        self._on_modified()

    def import_deck(self):
        path = filedialog.askopenfilename(parent=self.root, title="导入牌表", filetypes=[("牌表文本", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
            self.set_deck(text)
        except Exception as exc:
            messagebox.showerror("无法导入牌表", str(exc), parent=self.root)

    def paste_deck(self):
        try:
            text = self.root.clipboard_get()
            self.deck_text.insert("insert", text)
            self._on_modified()
        except tk.TclError:
            self.status.set("剪贴板中没有可粘贴的文字。")

    def example(self):
        examples = {
            'mtg': "# 自动行与指定版可混用\n4 Lightning Bolt\n1 Lightning Bolt M11 146\n1 Lightning Bolt (M11) 146\n4 Counterspell\n8 Forest\n1 碎骨巨人\n",
            'ptcg-zh': "寶可夢\n2 爆炸頭水牛 SV8a 133\n1 傑尼龜 SV2a 007\n訓練家\n1 招式學習器 演進 SV8a 160\n",
            'ptcg-en': "Pokémon\n2 Shaymin DRI 10\n1 Froslass PR-SV 117\n1 Tatsugiri PR-SV 118\n",
            'ygo-zh': "青眼白龙\n黑魔术师\n死者苏生\n",
            'ygo-ja': "青眼の白龍\nブラック・マジシャン\n死者蘇生\n",
            'ygo-tcg': "Blue-Eyes White Dragon\nDark Magician\nMonster Reborn\n",
        }
        self.set_deck(examples[self.game_code()])

    def clear_deck(self):
        self.set_deck("")

    def sync_options(self):
        if self.running:
            return
        self.sync_language()
        self.pdf_check.configure(state="disabled" if self.dry_run.get() else "normal")
        enabled = self.pdf.get() and not self.dry_run.get()
        self.paper_box.configure(state="readonly" if enabled else "disabled")
        self.mark_check.configure(state="normal" if enabled else "disabled")
        self.force_check.configure(state="disabled" if self.dry_run.get() else "normal")
        self.start_button.configure(text="开始查询" if self.dry_run.get() else "开始下载")

    def set_running(self, running):
        self.running = running
        for widget, normal in self.edit_controls:
            widget.configure(state="disabled" if running else normal)
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        if not running:
            self.sync_options()

    def picture_directory(self):
        game = self.game_code()
        if game.startswith('ygo-'):
            raise ValueError("游戏王尚未接入，暂无图片目录。")
        return self.output_root / GAME_PICTURE_DIRECTORIES[self.game.get()]

    def concurrency(self):
        self.game_code()
        return GAME_PRESETS[self.game.get()]

    def sync_language(self):
        game = self.game_code()
        if game == 'mtg':
            if self.language.get() in FIXED_LANGUAGE_LABELS.values():
                self.language.set(getattr(self, '_mtg_language', next(iter(LANG_PRESETS))))
            self.language_box.configure(state="disabled" if self.running else "normal")
        else:
            if self.language.get() not in FIXED_LANGUAGE_LABELS.values():
                self._mtg_language = self.language.get()
            self.language.set(FIXED_LANGUAGE_LABELS[game])
            self.language_box.configure(state="disabled")

    def _on_game_changed(self, *args):
        if self.game.get() not in GAME_PRESETS:
            self.network_policy.set("请选择列表中的卡牌游戏。")
            return
        game = self.game_code()
        if hasattr(self, "pics_label"):
            self.pics_label.set("尚未接入，暂无图片目录" if game.startswith('ygo-') else str(self.picture_directory()))
        query_workers, download_workers = self.concurrency()
        text = "查询并发 %d · 下载并发 %d" % (query_workers, download_workers)
        if game in PTCG_LANGUAGES:
            text += "\n本机实测择优，TCGdex 未公布硬性限额。"
        elif game.startswith('ygo-'):
            text = "游戏王占位：下载接口尚未接入；可识别纯名称牌表。"
        self.network_policy.set(text)
        if hasattr(self, 'language_box'):
            self.sync_language()
        if hasattr(self, 'deck_text'):
            self.update_count()

    def configuration(self):
        text = self.deck_text.get("1.0", "end-1c").strip()
        game = self.game_code()
        if game.startswith('ygo-'):
            raise ValueError("游戏王下载接口尚未接入。")
        rows = self.deck_rows(text)
        if not rows:
            raise ValueError("请先粘贴或导入牌表，每行填写一种牌。")
        if any(row[0] <= 0 for row in rows):
            raise ValueError("牌表里的数量必须大于 0。")
        workers, _ = self.concurrency()
        if game in PTCG_LANGUAGES:
            codes = [PTCG_LANGUAGES[game]]
        else:
            lang_text = self.language.get().strip()
            langs = LANG_PRESETS.get(lang_text, lang_text).replace("，", ",")
            codes = [c.strip().lower() for c in langs.split(",") if c.strip()]
            if not codes or any(not re.fullmatch(r"[a-z]{2,3}", c) for c in codes):
                raise ValueError("请选择语言顺序，或输入语言代码，例如 zhs,zht,en。")
        if not self.paper.get().startswith(("A3", "A4")):
            raise ValueError("请选择 A3 或 A4 纸张。")
        return text, workers, self.paper.get()[:2], ",".join(dict.fromkeys(codes))

    def start(self):
        if self.running:
            return
        try:
            text, workers, paper, langs = self.configuration()
            _, download_workers = self.concurrency()
            task_dir = create_task_directory(self.output_root)
            deck_path = task_dir / "本次牌表.txt"
            deck_path.write_text(text + "\n", encoding="utf-8")
            command = engine_command(deck_path, task_dir, workers, paper, langs,
                                     self.pdf.get(), self.cut_marks.get(), self.dry_run.get(), self.force.get(),
                                     download_workers=download_workers, game=self.game_code())
            self.output_dir = task_dir
            self.output_label.set(str(task_dir))
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc), parent=self.root)
            return
        self.cancel_requested.clear()
        self.summary = None
        self.token_summary = None
        self.download_total = self.download_finished = 0
        self.last_pdf_allowed = self.pdf.get() and not self.dry_run.get()
        pdf_path = self.output_dir / "cards.pdf"
        self.previous_pdf_stamp = (pdf_path.stat().st_mtime_ns, pdf_path.stat().st_size) if pdf_path.exists() else None
        self.pdf_button.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("正在读取本地图片缓存并处理新牌名…")
        self.detail.set("自动处理网络请求，受限时等待并重试。")
        self.set_running(True)
        self.append_log("程序目录：%s\n输出目录：%s\n" % (self.base_dir, self.output_dir))
        threading.Thread(target=self.run_process, args=(command,), daemon=True).start()

    def run_process(self, command):
        """只负责进程和队列，所有 Tk 操作都在主线程进行。"""
        process = None
        code = 1
        try:
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUTF8"] = "1"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with self.process_lock:
                if self.cancel_requested.is_set() or self.closing:
                    self.events.put(("finished", -1))
                    return
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=str(self.base_dir), env=environment,
                                           creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
                self.process = process
            for line in process.stdout:
                self.events.put(("log", line))
            code = process.wait()
        except Exception as exc:
            self.events.put(("log", "启动或运行失败：%s\n" % exc))
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            with self.process_lock:
                self.process = None
        self.events.put(("finished", code))

    def append_log(self, text):
        # PDF 进度使用回车刷新；GUI 显示为普通日志行。
        text = text.replace("\r", "\n")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def interpret_log(self, text):
        token_summary = re.search(r"^Token 完成：成功 (\d+) / 失败 (\d+) / 关联检查失败 (\d+)\s*$", text.strip())
        if token_summary:
            self.token_summary = tuple(map(int, token_summary.groups()))
            self.status.set("Token 处理完成。")
            self.detail.set("Token 成功 %d · 失败 %d · 关联检查失败 %d" % self.token_summary)
            return
        if text.lstrip().startswith("[Token]"):
            self.status.set(text.strip())
            self.detail.set("正在自动处理关联 Token，网络请求受限时等待并重试。")
            return
        cached = re.search(r"\[缓存 (\d+)/(\d+)\] (.*)", text)
        if cached:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(cached.group(2))), value=int(cached.group(1)))
            self.status.set("本地缓存 %s/%s：%s" % cached.groups())
        if "所有牌名已在本地图片缓存中" in text:
            self.status.set("全部命中本地缓存，跳过 API 查询。")
        query = re.search(r"\[查询 (\d+)/(\d+)\] (.*)", text)
        query_begin = re.search(r"开始查询：(\d+) 种牌，查询并发 (\d+)", text)
        query_progress = re.search(r"查询进度 (\d+)/(\d+) (.*)", text)
        begin = re.search(r"开始下载：(\d+) 张图片，初始并发 (\d+)", text)
        download = re.search(r"下载完成 \[(\d+)/(\d+)\]", text)
        progress = re.search(r"下载进度 (\d+)/(\d+) (.*)", text)
        reduced = re.search(r"自动降并发：(\d+) -> (\d+)", text)
        summary = re.search(r"^完成：成功 (\d+) / 失败 (\d+)\s*$", text.strip())
        if query_begin:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(query_begin.group(1))), value=0)
            self.status.set("正在并行查询牌表并选择卡图…")
            self.detail.set("%s 种牌 · 自动处理网络请求，受限时等待并重试。" % query_begin.group(1))
        if query:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(query.group(2))), value=int(query.group(1)))
            self.status.set("查询 %s/%s：%s" % query.groups())
        if query_progress:
            self.progress.configure(value=int(query_progress.group(1)))
            self.detail.set(text.strip())
        if begin:
            self.download_total = int(begin.group(1))
            self.download_finished = 0
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, self.download_total), value=0)
            self.status.set("正在并行下载卡图…")
            self.detail.set("图片 %s 张 · 自动处理网络请求，受限时等待并重试。" % begin.group(1))
        if download:
            self.download_finished = int(download.group(1))
            self.progress.configure(value=self.download_finished)
            self.status.set("下载 %s/%s" % download.groups())
        elif "下载失败：" in text:
            self.download_finished += 1
            self.progress.configure(value=self.download_finished)
        if progress:
            self.download_finished = int(progress.group(1))
            self.progress.configure(value=self.download_finished)
            self.detail.set(text.strip())
        if reduced:
            self.detail.set("下载请求受限，等待后自动重试。")
        if "API 受限 HTTP" in text:
            self.status.set("查询请求受限，所有查询等待后自动重试…")
            self.detail.set(text.strip())
        elif "受限 HTTP" in text:
            self.status.set("服务器限制下载，正在等待后重试…")
        if "排版卡册:" in text:
            self.status.set("图片下载完成，正在生成 PDF 卡册…")
            self.progress.stop()
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        if summary:
            self.summary = tuple(map(int, summary.groups()))

    def poll_events(self):
        if self.closing:
            return
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
                self.interpret_log(value)
            elif kind == "finished":
                self.finish(value)
        self.poll_id = self.root.after(80, self.poll_events)

    def finish(self, code):
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        if self.cancel_requested.is_set():
            self.status.set("任务已停止，已下载的图片保留。")
            self.detail.set("图片保留在 %s；记录保留在 %s；下次优先复用缓存。" % (self.picture_directory().name, self.output_dir.name))
        elif code == 0 or self.summary is not None or self.token_summary is not None:
            self.progress.configure(value=100)
            partial = code != 0 or (self.summary is not None and self.summary[1] > 0) or (self.token_summary is not None and any(self.token_summary[1:]))
            self.status.set("任务完成，部分卡牌失败。" if partial else ("查询完成。" if self.dry_run.get() else "任务完成。"))
            statistics = []
            if self.summary is not None:
                statistics.append("主卡成功 %d · 失败 %d" % self.summary)
            if self.token_summary is not None:
                statistics.append("Token 成功 %d · 失败 %d · 关联检查失败 %d" % self.token_summary)
            statistics.append("请查看日志和清单。" if partial else "输出已保存到程序同目录。")
            self.detail.set("；".join(statistics))
        else:
            self.status.set("运行失败，请查看下方日志。")
            self.detail.set("输出目录：%s" % self.output_dir)
        self.set_running(False)
        pdf_path = self.output_dir / "cards.pdf"
        pdf_stamp = (pdf_path.stat().st_mtime_ns, pdf_path.stat().st_size) if pdf_path.exists() else None
        pdf_ready = self.last_pdf_allowed and not self.cancel_requested.is_set() and pdf_stamp is not None and pdf_stamp != self.previous_pdf_stamp and code in (0, 1) and self.summary is not None
        self.pdf_button.configure(state="normal" if pdf_ready else "disabled")
        try:
            (self.output_dir / "运行日志.txt").write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            self.append_log("无法保存日志：%s\n" % exc)

    def cancel(self):
        if not self.running:
            return
        self.cancel_requested.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在停止任务…")
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()

    def open_output(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._open_path(self.output_dir)
        except Exception as exc:
            messagebox.showerror("无法打开输出目录", str(exc), parent=self.root)

    def open_pics(self):
        try:
            path = self.picture_directory()
            path.mkdir(parents=True, exist_ok=True)
            self._open_path(path)
        except Exception as exc:
            messagebox.showerror("无法打开图片缓存", str(exc), parent=self.root)

    def open_pdf(self):
        path = self.output_dir / "cards.pdf"
        if path.exists():
            try:
                self._open_path(path)
            except Exception as exc:
                messagebox.showerror("无法打开 PDF", str(exc), parent=self.root)

    def _open_path(self, path):
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.status.set("日志已复制到剪贴板。")

    def close(self):
        self.closing = True
        self.cancel_requested.set()
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
        self.root.destroy()


def launch_gui():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    CardDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--engine":
        try:
            sys.exit(main(sys.argv[2:]))
        except KeyboardInterrupt:
            print("\n任务已停止。", file=sys.stderr, flush=True)
            sys.exit(130)
        except Exception as exc:
            print("运行失败：%s" % exc, file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        launch_gui()
