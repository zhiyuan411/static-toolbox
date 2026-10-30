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

用法示例：
  chmod +x fetch_github_stars.py
  ./fetch_github_stars.py                       # 默认抓取 50 页
  ./fetch_github_stars.py --pages 10            # 只抓 10 页
  ./fetch_github_stars.py --no-translate        # 不做翻译
  ./fetch_github_stars.py --retry github_search_failed.jsonl
  ./fetch_github_stars.py --retry github_search_failed_retry.jsonl
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

# ---- 抓取相关 ----
DEFAULT_PAGES = 50          # 默认抓取页数（50 页 × 10 条 = 500 条最高星结果）
PER_PAGE = 10               # GitHub 搜索每页结果条数（用于计算全局排名）
START_PAGE = 1              # 起始页码
REQUEST_INTERVAL = 1.0      # 每页抓取之间的间隔（秒）
REQUEST_TIMEOUT = 30        # 单次页面请求超时（秒）

# ---- 搜索条件（SEARCH_QUERY 可写已编码形式，也可写 stars:>1 这类原始形式）----
BASE_URL = "https://github.com/search"
SEARCH_QUERY = "stars%3A%3E50000"   # 即 stars:>50000，按星数倒序取全局前 500 名
SEARCH_TYPE = "Repositories"
SEARCH_SORT = "stars"
SEARCH_ORDER = "desc"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- 输出文件（相对路径默认存放在脚本所在目录）----
OUTPUT_FILE = "github_stars.jsonl"                  # 结果 jsonl
FAILED_FILE = "github_search_failed.jsonl"          # 抓取阶段的失败清单
RETRY_FAILED_FILE = "github_search_failed_retry.jsonl"  # 重试阶段仍失败的清单

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

_EMBEDDED_DATA_RE = re.compile(
    r'<script[^>]*data-target="react-app\.embeddedData"[^>]*>', re.I)
_CJK_RE = re.compile(u"[\u4e00-\u9fff\u3400-\u4dbf]")


def log(message):
    """输出带时间戳的过程日志。"""
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message))
    sys.stdout.flush()


def resolve_path(path):
    """相对路径统一解析到脚本所在目录，便于任意目录下 ./xx 执行。"""
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


def http_request(url, data=None, timeout=REQUEST_TIMEOUT):
    """发起 HTTP 请求并返回响应体（bytes）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
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
    results, meta = extract_results(html)
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
        return "", "empty"
    if has_chinese(text):       # 已含中文，不翻译
        stats["skip"] += 1
        return text, "skip"

    source = text[:TRANSLATE_MAX_CHARS]
    for name, func in (("bilibili", translate_by_bilibili),
                       ("mymemory", translate_by_mymemory)):
        translated = ""
        try:
            translated = func(source)
        except Exception:
            translated = ""
        if translated:
            stats[name] += 1
            return translated, name
        if TRANSLATE_INTERVAL > 0:
            time.sleep(TRANSLATE_INTERVAL)
    stats["failed"] += 1
    return "", "failed"


# ============================================================
#                        jsonl 读写
# ============================================================

def build_row(item, page, index, stats, do_translate):
    """在原始搜索结果字段基础上补充 _page/_rank/_desc_zh 等字段。"""
    row = dict(item)
    row["_page"] = page
    row["_idx"] = index
    row["_rank"] = (page - 1) * PER_PAGE + index + 1
    row["_fetched_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if do_translate:
        zh, by = translate_description(item.get("hl_trunc_description") or "", stats)
    else:
        zh, by = "", "disabled"
    row["_desc_zh"] = zh
    row["_desc_zh_by"] = by
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
            try:
                page = int(record.get("page"))
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
          interval=REQUEST_INTERVAL, do_translate=TRANSLATE_ENABLED, append=False):
    page_list = list(range(start_page, start_page + pages))
    total = len(page_list)

    if append:
        existed = len(load_rows(out_path))
        log("追加模式：已有 %d 条，本次继续写入 %s" % (existed, out_path))
    else:
        if os.path.exists(out_path):
            log("输出文件已存在，将被覆盖：%s" % out_path)
        open(out_path, "w", encoding="utf-8").close()

    log("开始抓取：第 %d ~ %d 页，共 %d 页，间隔 %.1fs，输出 %s"
        % (start_page, start_page + pages - 1, total, interval, out_path))

    stats = new_translate_stats()
    failures = []
    ok_pages = 0
    total_rows = 0
    meta_logged = False

    for seq, page in enumerate(page_list, 1):
        log("[%d/%d] 第 %d 页抓取中 ..." % (seq, total, page))
        url = build_url(page, query)
        try:
            url, results, meta = fetch_page(page, query)
        except urllib.error.HTTPError as exc:
            reason = "HTTP %s %s" % (exc.code, getattr(exc, "reason", ""))
            results = None
        except urllib.error.URLError as exc:
            reason = "网络错误 %s" % getattr(exc, "reason", exc)
            results = None
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
            results = None

        if results is None:
            failures.append(make_failure(page, url, reason))
            log("[%d/%d] 第 %d 页失败：%s（已记录，跳过）" % (seq, total, page, reason))
            if interval > 0 and seq < total:
                time.sleep(interval)
            continue

        if not meta_logged:
            meta_logged = True
            log("搜索命中共 %s 条 / %s 页" % (meta.get("result_count"), meta.get("page_count")))
            if isinstance(meta.get("page_count"), int) and meta["page_count"] < total:
                log("提示：实际可翻页数 %d 少于计划抓取页数 %d，超出部分将无结果"
                    % (meta["page_count"], total))

        rows = []
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            rows.append(build_row(item, page, index, stats, do_translate))
        append_rows(out_path, rows)

        ok_pages += 1
        total_rows += len(rows)
        log("[%d/%d] 第 %d 页成功：%d 条 | 累计 %d 条" % (seq, total, page, len(rows), total_rows))

        if interval > 0 and seq < total:
            time.sleep(interval)

    write_failed(failed_path, failures)
    log("-" * 56)
    log("抓取完成：成功 %d 页 / %d 条，失败 %d 页" % (ok_pages, total_rows, len(failures)))
    if do_translate:
        log("翻译统计：bilibili %d | MyMemory %d | 含中文跳过 %d | 空描述 %d | 翻译失败 %d"
            % (stats["bilibili"], stats["mymemory"], stats["skip"],
               stats["empty"], stats["failed"]))
    log("结果文件：%s" % out_path)
    if failures:
        log("失败清单：%s" % failed_path)
        log("可执行重试：./%s --retry %s"
            % (os.path.basename(__file__), os.path.basename(failed_path)))
    else:
        log("全部页面抓取成功，无失败记录")
    return len(failures)


def retry(retry_path, out_path, retry_failed_path, query,
          interval=REQUEST_INTERVAL, do_translate=TRANSLATE_ENABLED):
    records = read_failed(retry_path)
    if not records:
        log("失败清单为空，无需重试：%s" % retry_path)
        return 0

    page_names = ", ".join(str(r["page"]) for r in records[:20])
    if len(records) > 20:
        page_names += " ..."
    log("待重试 %d 页：%s" % (len(records), page_names))

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

    for seq, record in enumerate(records, 1):
        page = record["page"]
        url = build_url(page, query)
        log("[%d/%d] 重试第 %d 页 ..." % (seq, total, page))
        try:
            url, results, meta = fetch_page(page, query)
        except urllib.error.HTTPError as exc:
            reason = "HTTP %s %s" % (exc.code, getattr(exc, "reason", ""))
            results = None
        except urllib.error.URLError as exc:
            reason = "网络错误 %s" % getattr(exc, "reason", exc)
            results = None
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
            results = None

        if results is None:
            failures.append(make_failure(page, url, reason))
            log("[%d/%d] 第 %d 页重试失败：%s" % (seq, total, page, reason))
            if interval > 0 and seq < total:
                time.sleep(interval)
            continue

        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            rank = (page - 1) * PER_PAGE + index + 1
            rank_map[rank] = build_row(item, page, index, stats, do_translate)
            inserted += 1
        ok_pages += 1
        log("[%d/%d] 第 %d 页重试成功：%d 条" % (seq, total, page, len(results)))

        if interval > 0 and seq < total:
            time.sleep(interval)

    ordered = sort_rows(list(rank_map.values()))
    write_rows(out_path, ordered)
    write_failed(retry_failed_path, failures)

    log("-" * 56)
    log("重试完成：成功 %d 页，仍失败 %d 页，写入/更新 %d 条" % (ok_pages, len(failures), inserted))
    if do_translate:
        log("翻译统计：bilibili %d | MyMemory %d | 含中文跳过 %d | 空描述 %d | 翻译失败 %d"
            % (stats["bilibili"], stats["mymemory"], stats["skip"],
               stats["empty"], stats["failed"]))
    log("结果文件已按排名重排：%s（共 %d 行）" % (out_path, len(ordered)))
    if failures:
        log("重试失败清单：%s" % retry_failed_path)
        log("可再次重试：./%s --retry %s"
            % (os.path.basename(__file__), os.path.basename(retry_failed_path)))
    else:
        log("全部失败页已补回，无遗留失败记录")
    return len(failures)


# ============================================================
#                        命令行入口
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="抓取 GitHub 高星仓库搜索结果并输出 jsonl")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES,
                        help="抓取页数，默认 %d" % DEFAULT_PAGES)
    parser.add_argument("--start-page", type=int, default=START_PAGE,
                        help="起始页码，默认 %d" % START_PAGE)
    parser.add_argument("--interval", type=float, default=REQUEST_INTERVAL,
                        help="每页抓取间隔秒数，默认 %s" % REQUEST_INTERVAL)
    parser.add_argument("--query", default=SEARCH_QUERY,
                        help="搜索条件，默认 %s" % SEARCH_QUERY)
    parser.add_argument("--output", default=OUTPUT_FILE, help="结果 jsonl 文件路径")
    parser.add_argument("--failed", default=FAILED_FILE, help="抓取失败清单路径")
    parser.add_argument("--retry", metavar="FILE", default=None,
                        help="重试模式：传入失败清单文件")
    parser.add_argument("--retry-failed", default=RETRY_FAILED_FILE,
                        help="重试失败清单输出路径")
    parser.add_argument("--append", action="store_true",
                        help="追加写入已有结果文件，而不是覆盖")
    parser.add_argument("--no-translate", action="store_true",
                        help="关闭描述翻译")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    do_translate = TRANSLATE_ENABLED and not args.no_translate
    output_path = resolve_path(args.output)

    if args.retry:
        retry(resolve_path(args.retry), output_path,
              resolve_path(args.retry_failed), args.query,
              interval=args.interval, do_translate=do_translate)
        return 0

    if args.pages <= 0:
        raise SystemExit("--pages 必须为正整数")
    if args.start_page <= 0:
        raise SystemExit("--start-page 必须为正整数")

    failures = crawl(args.pages, args.start_page, output_path,
                     resolve_path(args.failed), args.query,
                     interval=args.interval, do_translate=do_translate,
                     append=args.append)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
