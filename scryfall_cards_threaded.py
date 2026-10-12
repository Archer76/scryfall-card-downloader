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
    卡图输出/pics/*.png   共享卡图，重复牌名直接复用；--pics 可指定缓存目录
    卡图输出/pics/index.json  牌名别名与选图索引
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


def api_get(path, params=None, retries=4, cache_missing=True):
    """API 请求缓存 24h；限流全局退避，持续受限时停止排队网络请求。

    cache_missing=False 用于关联 token 的按 ID 查询：404 不写负缓存，已有负缓存也不采信，
    使失败的关联检查下次真正重试。"""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    hit, data = _read_api_cache(url)
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


def query_card(name, langs):
    """查询线程只返回元数据；文件命名和清单由主线程按牌表顺序处理。"""
    resolved = resolve_card(name)
    result = {"resolved": resolved}
    if not resolved:
        return result
    oracle = resolved["oracle_id"]
    selected, used_lang = select_print(oracle, langs, is_basic_land(resolved))
    result.update(selected=selected, used_lang=used_lang)
    result["token_check"] = inspect_token_parts(resolved, selected)
    if not selected:
        return result
    jobs = image_jobs(selected)
    result["jobs"] = jobs
    if jobs:
        result["display"], result["name_source"] = resolve_display_names(oracle, selected)
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
SET_RE = re.compile(r"\s*[\(\[]([A-Za-z0-9]{2,6})[\)\]](\s+[\w\-\u2605]+)?\s*$")
SKIP_WORDS = {
    "deck", "decks", "sideboard", "mainboard", "commander", "companion",
    "maybeboard", "creatures", "lands", "instants", "sorceries", "enchantments",
    "artifacts", "planeswalkers", "battles", "tokens", "about",
}


def parse_decklist(text):
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        qty = 1
        match = QTY_RE.match(line)
        if match:
            qty = int(match.group(1))
            line = match.group(2).strip()
        if line.lower() in SKIP_WORDS:
            continue
        name = SET_RE.sub("", line).strip()
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
    """永久保存选图与牌名别名；索引内图片路径相对 pics，移动程序仍可复用。"""
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
        alias = cache_name(name)
        key = self.data["aliases"].get(alias)
        if key is None and alias in self.data["aliases"]:
            return None
        entry = self.data["cards"].get(key)
        if entry is None:
            # 用户直接放入 pics 的完整图片，也可按文件名使用。
            token_files = {record["file"] for token in self.data["tokens"].values()
                           for record in token.get("files", [])}
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
            _query_log("共享图片迁移：%d 个旧图片文件归入 pics，导入 %d 条牌名记录。" % (moved, imported))
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
    parser.add_argument("--pics", default=os.path.join(os.path.dirname(program_cache_directory()), "卡图输出", "pics"),
                        help="共享图片缓存目录（默认程序旁的卡图输出/pics）")
    parser.add_argument("--langs", default="zhs,zht,en",
                        help="语言优先级，逗号分隔（默认 zhs,zht,en）")
    parser.add_argument("--paper", default="A4", choices=sorted(PAPERS),
                        help="卡册纸张（默认 A4 竖向每页 9 张；A3 横向每页 18 张）")
    parser.add_argument("--no-pdf", action="store_true", help="不生成卡册 PDF")
    parser.add_argument("--no-cut-marks", action="store_true", help="不在卡册上画裁切框")
    parser.add_argument("--dry-run", action="store_true", help="只解析与选图，不下载")
    parser.add_argument("--force", action="store_true", help="已存在的图片也重新下载")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="查询线程数（默认 %d，1–%d；API 请求间隔 0.6 秒）" % (DEFAULT_WORKERS, MAX_WORKERS))
    parser.add_argument("--download-workers", type=int, default=DEFAULT_DOWNLOAD_WORKERS,
                        help="图片下载线程数（默认 %d，1–%d；受限时自动减半）" % (DEFAULT_DOWNLOAD_WORKERS, MAX_DOWNLOAD_WORKERS))
    parser.add_argument("--image-interval", type=float, default=MIN_IMAGE_INTERVAL,
                        help="图片请求启动间隔秒数（默认 %g；0 取消固定间隔，不取消服务端冷却）" % MIN_IMAGE_INTERVAL)
    args = parser.parse_args(argv)
    if not math.isfinite(args.image_interval) or args.image_interval < 0:
        parser.error("--image-interval 必须是非负有限数")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error("--workers 必须在 1 到 %d 之间" % MAX_WORKERS)
    if not 1 <= args.download_workers <= MAX_DOWNLOAD_WORKERS:
        parser.error("--download-workers 必须在 1 到 %d 之间" % MAX_DOWNLOAD_WORKERS)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True, errors="replace")

    global _api_disk_cache_dir, _image_interval
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
                url = uris.get("png") or uris.get("large") or uris.get("normal")
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n运行已中断。已下载的图片保留，下次运行会跳过。", file=sys.stderr, flush=True)
        sys.exit(130)
    except Exception as exc:
        print("运行失败：%s" % exc, file=sys.stderr, flush=True)
        sys.exit(1)
