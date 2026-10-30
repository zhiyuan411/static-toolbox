#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 高星仓库搜索结果抓取脚本（仅依赖 Python3 标准库）

功能：
  1. 循环抓取 GitHub 仓库搜索结果页（按星数倒序），解析出原始搜索结果并写入 jsonl 文件；
  2. 抓取失败的页不重试，继续下一页，并把失败信息记录到独立文件；
  3. 支持通过 --retry 传入失败清单文件，重新抓取这些页，并把结果按「页数/排名」插回
     jsonl 文件的正确行位置；重试仍失败的页会写入另一个失败文件，可反复重试；
  4. 把英文描述翻译为中文写入新增字段 _desc_zh（优先 bilibili 接口，其次 MyMemory）。
     描述已含中文则原样保留；两个接口都失败则 _desc_zh 为空，不阻塞、不计入失败页。

用法示例（详见 ./fetch_github_stars.py -h）：
  chmod +x fetch_github_stars.py
  ./fetch_github_stars.py                       # 默认抓取 50 页（约 500 条）
  ./fetch_github_stars.py --pages 10 --interval 2 --timeout 60
  ./fetch_github_stars.py --no-translate        # 不做翻译
  ./fetch_github_stars.py -d --pages 1 --limit 2 --dry-run   # 调试/小数据量演练
  ./fetch_github_stars.py --retry github_search_failed.jsonl
  ./fetch_github_stars.py --retry github_search_failed_r1.jsonl  # 反复重试直至清零
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ============================================================
#                        常量配置区
# ============================================================

# ---- 抓取相关（下面 3 个均可用命令行参数覆盖，参数优先级高于此处配置）----
DEFAULT_PAGES = 50          # 默认抓取页数（50 页 × 10 条 = 500 条最高星结果）  -> --pages
PER_PAGE = 10               # GitHub 搜索每页结果条数（用于计算全局排名）
START_PAGE = 1              # 起始页码                                      -> --start-page
REQUEST_INTERVAL = 1.0      # 每页抓取之间的间隔（秒）                        -> --interval
REQUEST_TIMEOUT = 30        # 单次页面请求超时（秒）                          -> --timeout

# ---- 调试相关 ----
DEBUG = False               # 调试模式：打印详细过程                          -> -d/--debug
DEFAULT_LIMIT = 0           # 每页最多处理的结果条数（0 表示全部）            -> --limit

# ---- 搜索条件（SEARCH_QUERY 可写已编码形式，也可写 stars:>1 这类原始形式）----
BASE_URL = "https://github.com/search"
SEARCH_QUERY = "stars%3A%3E50000"   # 即 stars:>50000，按星数倒序取全局前 500 名
SEARCH_TYPE = "Repositories"
SEARCH_SORT = "stars"
SEARCH_ORDER = "desc"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- 输出文件（相对路径默认存放在脚本所在目录）----
OUTPUT_FILE = "github_stars.jsonl"          # 结果 jsonl
FAILED_FILE = "github_search_failed.jsonl"  # 抓取阶段的失败清单
# 重试阶段仍失败的清单：None 表示按输入文件名自动递增轮次，永不覆盖输入文件，
# 形如 github_search_failed.jsonl -> github_search_failed_r1.jsonl -> _r2 ...
# 因此可反复执行「重试 -> 重试新文件」直到没有失败，无需手工复制/改名。
RETRY_FAILED_FILE = None                    # 也可直接写死路径，或用 --retry-failed 指定

# ---- 翻译相关 ----
TRANSLATE_ENABLED = True        # 是否翻译描述
TRANSLATE_INTERVAL = 0.2        # 两次翻译请求之间的最小间隔（秒）
TRANSLATE_TIMEOUT = 20          # 单次翻译请求超时（秒）
TRANSLATE_MAX_CHARS = 500       # 单次翻译的最大字符数（超出部分截断）

BILIBILI_TRANSLATE_URL = "https://index-translate.bilibili.com/v1/chat/completions"
BILIBILI_TRANSLATE_MODEL = "Index-Translate-35B-A3B"
MYMEMORY_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
TRANSLATE_PROMPT = "翻译为简体中文，只输出翻译结果，不要输出其他任何内容：\n\n{}"

# ============================================================
#                        基础工具
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 运行期实际生效的值（由命令行参数覆盖配置区默认值）
runtime_timeout = REQUEST_TIMEOUT   # <- --timeout
runtime_debug = DEBUG               # <- -d/--debug

_EMBEDDED_DATA_RE = re.compile(
    r'<script[^>]*data-target="react-app\.embeddedData"[^>]*>', re.I)
_CJK_RE = re.compile(u"[\u4e00-\u9fff\u3400-\u4dbf]")
_ROUND_RE = re.compile(r"_r(\d+)$")


def log(message):
    """输出带时间戳的过程日志。"""
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message))
    sys.stdout.flush()


def debug(message):
    """仅在调试模式（-d/--debug）下输出详细信息。"""
    if runtime_debug:
        print("[%s]   · %s" % (datetime.now().strftime("%H:%M:%S"), message))
        sys.stdout.flush()


def humanize_seconds(seconds):
    """把秒数转换为人性化的「x小时x分x秒」。"""
    total = int(max(0, round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d小时%d分%d秒" % (hours, minutes, secs)
    if minutes:
        return "%d分%d秒" % (minutes, secs)
    return "%d秒" % secs


def eta_text(done, total, start_time):
    """按已执行页数与已耗时间等比例估算剩余时间和完成时刻。"""
    if done <= 0:
        return "预计剩余 计算中"
    elapsed = time.time() - start_time
    remaining = elapsed / done * (total - done)
    finish = datetime.fromtimestamp(time.time() + remaining)
    return "预计剩余 %s | 预计完成 %s" % (
        humanize_seconds(remaining), finish.strftime("%H:%M:%S"))


def resolve_path(path):
    """相对路径统一解析到脚本所在目录，便于任意目录下 ./xx 执行。"""
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


def http_request(url, data=None, timeout=None):
    """发起 HTTP 请求并返回响应体（bytes）。"""
    if timeout is None:
        timeout = runtime_timeout
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    debug("请求 -> %s | 超时 %ss | %s" % (url, timeout, "POST" if data else "GET"))
    request = urllib.request.Request(url, data=data, headers=headers)
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        status = getattr(response, "status", None) or response.getcode()
    if "gzip" in encoding:
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    debug("响应 <- status=%s, %d 字节, 耗时 %.2fs" % (status, len(body), time.time() - started))
    return body


def build_url(page, query):
    """拼接搜索页 URL，query 视为已编码或可自动编码的字符串。"""
    encoded = query
    if "%" not in query:
        encoded = urllib.parse.quote(query, safe="")
    return "%s?q=%s&type=%s&s=%s&o=%s&p=%d" % (
        BASE_URL, encoded, SEARCH_TYPE, SEARCH_SORT, SEARCH_ORDER, page)


def extract_results(html):
    """从搜索页 HTML 中解析出 results 列表与分页元信息。"""
    match = _EMBEDDED_DATA_RE.search(html)
    if not match:
        raise ValueError("未找到 embeddedData 数据块（可能被拦截或页面结构有变）")
    end = html.find("</script>", match.end())
    if end < 0:
        raise ValueError("embeddedData 数据块未闭合")
    raw = html[match.end():end].strip()
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError("embeddedData JSON 解析失败: %s" % exc)

    payload = data.get("payload") or {}
    route = payload.get("blackbirdSearchRoute")
    if not isinstance(route, dict):
        raise ValueError("payload.blackbirdSearchRoute 缺失")
    results = route.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("当前页无结果（可能被限流或需要登录）")

    meta = {
        "page": route.get("page"),
        "page_count": route.get("page_count"),
        "result_count": route.get("result_count"),
    }
    return results, meta


def fetch_page(page, query):
    """抓取指定页并返回 (url, results, meta)。失败时抛出异常。"""
    url = build_url(page, query)
    html = http_request(url).decode("utf-8", "replace")
    debug("页面 HTML 长度 %d 字符" % len(html))
    results, meta = extract_results(html)
    debug("解析结果 %d 条 | 命中总数 %s | 总页数 %s"
          % (len(results), meta.get("result_count"), meta.get("page_count")))
    return url, results, meta


# ============================================================
#                        描述翻译
# ============================================================

def new_translate_stats():
    return {"bilibili": 0, "mymemory": 0, "skip": 0, "empty": 0, "failed": 0}


def has_chinese(text):
    return bool(_CJK_RE.search(text or ""))


def translate_by_bilibili(text):
    """调用 bilibili 翻译接口，无结果时返回空字符串。"""
    payload = {
        "model": BILIBILI_TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": TRANSLATE_PROMPT.format(text)}],
        "temperature": 0.3,
    }
    body = http_request(
        BILIBILI_TRANSLATE_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=TRANSLATE_TIMEOUT,
    )
    data = json.loads(body.decode("utf-8", "replace"))
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def translate_by_mymemory(text):
    """调用 MyMemory 翻译接口，无结果或配额耗尽时返回空字符串。"""
    query_string = urllib.parse.urlencode({"q": text, "langpair": "en|zh"})
    body = http_request(MYMEMORY_TRANSLATE_URL + "?" + query_string,
                        timeout=TRANSLATE_TIMEOUT)
    data = json.loads(body.decode("utf-8", "replace"))
    if str(data.get("responseStatus") or "") != "200":
        return ""
    translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not translated or "MYMEMORY WARNING" in translated.upper():
        return ""
    return translated


def translate_description(text, stats):
    """翻译仓库描述，返回 (译文, 来源)。失败时返回 ("", "failed")。"""
    text = (text or "").strip()
    if not text:
        stats["empty"] += 1
        debug("描述为空，跳过翻译")
        return "", "empty"
    if has_chinese(text):       # 已含中文，不翻译
        stats["skip"] += 1
        debug("描述已含中文，跳过翻译：%s" % text[:60])
        return text, "skip"

    source = text[:TRANSLATE_MAX_CHARS]
    debug("待翻译(%d 字)：%s" % (len(source), source[:60]))
    for name, func in (("bilibili", translate_by_bilibili),
                       ("mymemory", translate_by_mymemory)):
        translated = ""
        try:
            started = time.time()
            translated = func(source)
            debug("翻译接口[%s] 耗时 %.2fs -> %s"
                  % (name, time.time() - started, (translated or "<空>")[:80]))
        except Exception as exc:
            translated = ""
            debug("翻译接口[%s] 异常：%s" % (name, exc))
        if translated:
            stats[name] += 1
            return translated, name
        if TRANSLATE_INTERVAL > 0:
            time.sleep(TRANSLATE_INTERVAL)
    stats["failed"] += 1
    debug("两个翻译接口均无结果，译文置空")
    return "", "failed"


# ============================================================
#                        jsonl 读写
# ============================================================

def build_row(item, page, index, stats, do_translate):
    """在原始搜索结果字段基础上补充 _page/_rank/_desc_zh/_desc_display 等字段。"""
    row = dict(item)
    row["_page"] = page
    row["_idx"] = index
    row["_rank"] = (page - 1) * PER_PAGE + index + 1
    row["_fetched_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    description = item.get("hl_trunc_description") or ""
    if do_translate:
        zh, by = translate_description(description, stats)
    else:
        zh, by = "", "disabled"
    row["_desc_zh"] = zh                        # 译文（失败时为空）
    row["_desc_zh_by"] = by                     # 译文来源
    row["_desc_display"] = zh if zh else description   # 综合描述：译文优先，否则用原文
    debug("#%s %s | 星 %s | 语言 %s | 描述来源 %s"
          % (row["_rank"], row.get("hl_name"), row.get("followers"),
             row.get("language"), by))
    return row


def dump_line(row):
    return json.dumps(row, ensure_ascii=False)


def append_rows(path, rows):
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dump_line(row) + "\n")


def write_rows(path, rows):
    """原子化整体重写 jsonl 文件（用于重试插入后重新排序）。"""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dump_line(row) + "\n")
    os.replace(tmp_path, path)


def load_rows(path):
    """读取 jsonl 文件为 dict 列表，跳过空行与坏行。"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def sort_rows(rows):
    """按排名升序排序，无排名的行保持原有相对顺序并排在最后。"""
    ranked, unranked = [], []
    for row in rows:
        rank = row.get("_rank")
        if isinstance(rank, int):
            ranked.append(row)
        else:
            unranked.append(row)
    ranked.sort(key=lambda r: r["_rank"])
    return ranked + unranked


# ============================================================
#                        失败清单
# ============================================================

def write_failed(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_failed(path):
    """读取失败清单，按页码去重升序返回。"""
    if not os.path.exists(path):
        raise SystemExit("失败清单文件不存在：%s" % path)
    pages = {}
    broken = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                broken += 1
                continue
            if not isinstance(record, dict):
                broken += 1
                continue
            raw_page = record.get("page")
            if raw_page is None:
                broken += 1
                continue
            try:
                page = int(raw_page)
            except (TypeError, ValueError):
                broken += 1
                continue
            pages[page] = {
                "page": page,
                "url": record.get("url") or "",
                "reason": record.get("reason") or "",
            }
    if broken:
        log("失败清单中有 %d 行无法识别，已忽略" % broken)
    return [pages[page] for page in sorted(pages)]


def next_retry_failed_path(input_path, explicit=None):
    """
    计算本轮重试的失败清单输出路径。

    显式指定（--retry-failed 或配置区 RETRY_FAILED_FILE）时直接使用；
    否则按输入文件名自动递增轮次：xxx.jsonl -> xxx_r1.jsonl -> xxx_r2.jsonl ...
    这样「重试 -> 再重试新文件」可以无限轮次进行，既不会覆盖输入文件，
    也不需要手工复制/改名。
    """
    if explicit:
        return resolve_path(explicit)
    directory = os.path.dirname(os.path.abspath(input_path)) or BASE_DIR
    stem = os.path.splitext(os.path.basename(input_path))[0]
    base = _ROUND_RE.sub("", stem)
    round_no = 1
    match = _ROUND_RE.search(stem)
    if match:
        round_no = int(match.group(1)) + 1
    while os.path.exists(os.path.join(directory, "%s_r%d.jsonl" % (base, round_no))):
        round_no += 1
    return os.path.join(directory, "%s_r%d.jsonl" % (base, round_no))


def make_failure(page, url, reason):
    return {
        "page": page,
        "url": url,
        "reason": reason[:200],
        "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ============================================================
#                        抓取主流程
# ============================================================

def crawl(pages, start_page, out_path, failed_path, query,
          interval=REQUEST_INTERVAL, do_translate=TRANSLATE_ENABLED,
          append=False, limit=DEFAULT_LIMIT, dry_run=False):
    page_list = list(range(start_page, start_page + pages))
    total = len(page_list)

    if append:
        existed = len(load_rows(out_path))
        log("追加模式：已有 %d 条，本次继续写入 %s" % (existed, out_path))
    elif not dry_run:
        if os.path.exists(out_path):
            log("输出文件已存在，将被覆盖：%s" % out_path)
        open(out_path, "w", encoding="utf-8").close()
    else:
        log("[dry-run] 演练模式，不会写入任何文件")

    log("开始抓取：第 %d ~ %d 页，共 %d 页，间隔 %.1fs，输出 %s"
        % (start_page, start_page + pages - 1, total, interval, out_path))
    log("生效配置：timeout=%ss | translate=%s | limit=%s"
        % (runtime_timeout, do_translate, limit if limit > 0 else "全部"))

    stats = new_translate_stats()
    failures = []
    ok_pages = 0
    total_rows = 0
    meta_logged = False
    start_time = time.time()

    for seq, page in enumerate(page_list, 1):
        log("[%d/%d] 第 %d 页抓取中 ..." % (seq, total, page))
        url = build_url(page, query)
        results = None
        meta = {}
        reason = ""
        try:
            url, results, meta = fetch_page(page, query)
        except urllib.error.HTTPError as exc:
            reason = "HTTP %s %s" % (exc.code, getattr(exc, "reason", ""))
        except urllib.error.URLError as exc:
            reason = "网络错误 %s" % getattr(exc, "reason", exc)
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)

        if results is None:
            failures.append(make_failure(page, url, reason))
            log("[%d/%d] 第 %d 页失败：%s（已记录，跳过） | %s"
                % (seq, total, page, reason, eta_text(seq, total, start_time)))
            if interval > 0 and seq < total:
                time.sleep(interval)
            continue

        if limit and limit > 0 and len(results) > limit:
            debug("调试限流：本页 %d 条仅处理前 %d 条" % (len(results), limit))
            results = results[:limit]

        if not meta_logged:
            meta_logged = True
            log("搜索命中共 %s 条 / %s 页" % (meta.get("result_count"), meta.get("page_count")))
            page_count = meta.get("page_count")
            if isinstance(page_count, int) and page_count < total:
                log("提示：实际可翻页数 %d 少于计划抓取页数 %d，超出部分将无结果"
                    % (page_count, total))

        rows = []
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            rows.append(build_row(item, page, index, stats, do_translate))
        if not dry_run:
            append_rows(out_path, rows)

        ok_pages += 1
        total_rows += len(rows)
        log("[%d/%d] 第 %d 页成功：%d 条 | 累计 %d 条 | %s"
            % (seq, total, page, len(rows), total_rows, eta_text(seq, total, start_time)))

        if interval > 0 and seq < total:
            time.sleep(interval)

    elapsed = time.time() - start_time
    log("-" * 56)
    log("抓取完成：成功 %d 页 / %d 条，失败 %d 页，耗时 %s"
        % (ok_pages, total_rows, len(failures), humanize_seconds(elapsed)))
    if do_translate:
        log("翻译统计：bilibili %d | MyMemory %d | 含中文跳过 %d | 空描述 %d | 翻译失败 %d"
            % (stats["bilibili"], stats["mymemory"], stats["skip"],
               stats["empty"], stats["failed"]))
    if dry_run:
        log("[dry-run] 未写入文件，本应输出：%s" % out_path)
    else:
        log("结果文件：%s" % out_path)
        if failures:
            write_failed(failed_path, failures)
            log("失败清单：%s" % failed_path)
            log("可执行重试：./%s --retry %s"
                % (os.path.basename(__file__), os.path.basename(failed_path)))
        else:
            if os.path.exists(failed_path):
                os.remove(failed_path)
            log("全部页面抓取成功，无失败记录")
    return len(failures)


def retry(retry_path, out_path, retry_failed_path, query,
          interval=REQUEST_INTERVAL, do_translate=TRANSLATE_ENABLED,
          limit=DEFAULT_LIMIT, dry_run=False):
    records = read_failed(retry_path)
    if not records:
        log("失败清单为空，无需重试：%s" % retry_path)
        return 0

    page_names = ", ".join(str(r["page"]) for r in records[:20])
    if len(records) > 20:
        page_names += " ..."
    log("待重试 %d 页：%s" % (len(records), page_names))
    log("生效配置：timeout=%ss | translate=%s | limit=%s"
        % (runtime_timeout, do_translate, limit if limit > 0 else "全部"))

    rows = load_rows(out_path)
    if not rows:
        log("警告：结果文件为空或不存在，重试结果将直接写入 %s" % out_path)

    rank_map = {}
    for row in rows:
        rank = row.get("_rank")
        if isinstance(rank, int):
            rank_map[rank] = row

    stats = new_translate_stats()
    failures = []
    ok_pages = 0
    inserted = 0
    total = len(records)
    start_time = time.time()

    for seq, record in enumerate(records, 1):
        page = record["page"]
        url = build_url(page, query)
        results = None
        meta = {}
        reason = ""
        log("[%d/%d] 重试第 %d 页 ..." % (seq, total, page))
        try:
            url, results, meta = fetch_page(page, query)
        except urllib.error.HTTPError as exc:
            reason = "HTTP %s %s" % (exc.code, getattr(exc, "reason", ""))
        except urllib.error.URLError as exc:
            reason = "网络错误 %s" % getattr(exc, "reason", exc)
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)

        if results is None:
            failures.append(make_failure(page, url, reason))
            log("[%d/%d] 第 %d 页重试失败：%s | %s"
                % (seq, total, page, reason, eta_text(seq, total, start_time)))
            if interval > 0 and seq < total:
                time.sleep(interval)
            continue

        if limit and limit > 0 and len(results) > limit:
            debug("调试限流：本页 %d 条仅处理前 %d 条" % (len(results), limit))
            results = results[:limit]

        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            rank = (page - 1) * PER_PAGE + index + 1
            rank_map[rank] = build_row(item, page, index, stats, do_translate)
            inserted += 1
        ok_pages += 1
        log("[%d/%d] 第 %d 页重试成功：%d 条 | %s"
            % (seq, total, page, len(results), eta_text(seq, total, start_time)))

        if interval > 0 and seq < total:
            time.sleep(interval)

    ordered = sort_rows(list(rank_map.values()))
    elapsed = time.time() - start_time

    log("-" * 56)
    log("重试完成：成功 %d 页，仍失败 %d 页，写入/更新 %d 条，耗时 %s"
        % (ok_pages, len(failures), inserted, humanize_seconds(elapsed)))
    if do_translate:
        log("翻译统计：bilibili %d | MyMemory %d | 含中文跳过 %d | 空描述 %d | 翻译失败 %d"
            % (stats["bilibili"], stats["mymemory"], stats["skip"],
               stats["empty"], stats["failed"]))

    if dry_run:
        log("[dry-run] 未写入文件，本应重排 %d 行到 %s" % (len(ordered), out_path))
    else:
        write_rows(out_path, ordered)
        log("结果文件已按排名重排：%s（共 %d 行）" % (out_path, len(ordered)))

    if failures:
        if dry_run:
            log("重试失败清单应写入：%s" % retry_failed_path)
        else:
            write_failed(retry_failed_path, failures)
            log("重试失败清单：%s（%d 页）" % (retry_failed_path, len(failures)))
        log("可再次重试：./%s --retry %s"
            % (os.path.basename(__file__), os.path.basename(retry_failed_path)))
    else:
        # 全部补回后清掉本轮输入与输出清单，保证「清单文件存在 == 还有页要重试」
        if not dry_run:
            for path in {retry_failed_path, retry_path}:
                if os.path.exists(path):
                    os.remove(path)
                    log("已清理失败清单：%s" % path)
        log("全部失败页已补回，无遗留失败记录，无需再次重试")
    return len(failures)


# ============================================================
#                        命令行入口
# ============================================================

EPILOG = """\
常用示例：
  ./fetch_github_stars.py                       默认抓取 50 页（约 500 条）
  ./fetch_github_stars.py --pages 10            只抓 10 页
  ./fetch_github_stars.py --pages 50 --interval 2 --timeout 60
                                                覆盖配置区的页数/间隔/超时
  ./fetch_github_stars.py -d --pages 1 --limit 2 --dry-run
                                                调试模式：抓 1 页、每页仅处理 2 条、不写文件
  ./fetch_github_stars.py --retry github_search_failed.jsonl
                                                重试失败页，结果按排名插回 jsonl
  ./fetch_github_stars.py --retry github_search_failed_r1.jsonl
                                                再重试上一轮仍失败的页（轮次文件自动递增，
                                                不会覆盖输入文件，可反复重试直到清零）

调试模式说明（-d/--debug）：
  额外打印 请求URL/响应大小/耗时、HTML 长度、解析条数、每条结果的排名与描述来源、
  每个翻译接口的返回与耗时；配合 --limit N（每页只处理前 N 条）、
  --pages 1、--dry-run（不写任何文件）即可做小数据量测试。

配置文件常量（脚本头部「常量配置区」）可用参数覆盖，参数优先级更高：
  DEFAULT_PAGES      <- --pages
  REQUEST_INTERVAL   <- --interval
  REQUEST_TIMEOUT    <- --timeout
"""


def _esc(text):
    """argparse 会对 help 文本做 % 格式化，这里转义其中的百分号。"""
    return str(text).replace("%", "%%")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch_github_stars.py",
        description="抓取 GitHub 高星仓库搜索结果并按排名输出 jsonl",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    group_crawl = parser.add_argument_group("抓取参数（覆盖配置区常量）")
    group_crawl.add_argument("--pages", type=int, default=DEFAULT_PAGES,
                             help="抓取页数，覆盖 DEFAULT_PAGES（默认 %d）" % DEFAULT_PAGES)
    group_crawl.add_argument("--interval", type=float, default=REQUEST_INTERVAL,
                             help="每页抓取间隔秒数，覆盖 REQUEST_INTERVAL（默认 %s）"
                                  % REQUEST_INTERVAL)
    group_crawl.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT,
                             help="单次请求超时秒数，覆盖 REQUEST_TIMEOUT（默认 %s）"
                                  % REQUEST_TIMEOUT)
    group_crawl.add_argument("--start-page", type=int, default=START_PAGE,
                             help="起始页码（默认 %d）" % START_PAGE)
    group_crawl.add_argument("--query", default=SEARCH_QUERY,
                             help=_esc("搜索条件（默认 %s）" % SEARCH_QUERY))

    group_debug = parser.add_argument_group("调试参数")
    group_debug.add_argument("-d", "--debug", action="store_true", default=DEBUG,
                             help="调试模式，打印详细过程（默认 %s）" % DEBUG)
    group_debug.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                             help="每页最多处理的结果条数，0 表示全部（默认 %d）"
                                  % DEFAULT_LIMIT)
    group_debug.add_argument("--dry-run", action="store_true",
                             help="演练模式：只抓取和打印，不写入任何文件")

    group_file = parser.add_argument_group("文件参数")
    group_file.add_argument("--output", default=OUTPUT_FILE,
                            help="结果 jsonl 路径（默认 %s）" % OUTPUT_FILE)
    group_file.add_argument("--failed", default=FAILED_FILE,
                            help="抓取失败清单路径（默认 %s）" % FAILED_FILE)
    group_file.add_argument("--retry", metavar="FILE", default=None,
                            help="重试模式：传入失败清单文件")
    group_file.add_argument("--retry-failed", default=None, metavar="FILE",
                            help="重试失败清单输出路径（默认按输入名自动递增 _r1/_r2 ...）")
    group_file.add_argument("--append", action="store_true",
                            help="追加写入已有结果文件，而不是覆盖")

    parser.add_argument("--no-translate", action="store_true",
                        help="关闭描述翻译")
    return parser.parse_args(argv)


def main(argv=None):
    global runtime_debug, runtime_timeout
    args = parse_args(argv)
    runtime_debug = args.debug
    runtime_timeout = args.timeout
    do_translate = TRANSLATE_ENABLED and not args.no_translate
    output_path = resolve_path(args.output)

    if args.retry:
        retry(resolve_path(args.retry), output_path,
              next_retry_failed_path(resolve_path(args.retry),
                                     args.retry_failed or RETRY_FAILED_FILE),
              args.query, interval=args.interval, do_translate=do_translate,
              limit=args.limit, dry_run=args.dry_run)
        return 0

    if args.pages <= 0:
        raise SystemExit("--pages 必须为正整数")
    if args.start_page <= 0:
        raise SystemExit("--start-page 必须为正整数")

    failures = crawl(args.pages, args.start_page, output_path,
                     resolve_path(args.failed), args.query,
                     interval=args.interval, do_translate=do_translate,
                     append=args.append, limit=args.limit, dry_run=args.dry_run)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
