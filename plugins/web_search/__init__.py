"""網路搜尋與網頁擷取。

搜尋順序由設定 order 決定（預設 firecrawl_keyless → tavily_keyless → firecrawl → tavily）：先用兩家官方免 key 端點，
限流或失敗才換要扣點數的 key。同一家有多把 key 時一把用到完再換下一把；全部用完就換下一家。key 不刪除，到期自動恢復。
firecrawl_keyless 是 Firecrawl 官方免 key 端點（/v2/scrape、/v2/search，不帶 Authorization，有每 IP 限流）。
tavily_keyless 是 Tavily 官方免 key 端點（/search、/extract，帶 X-Tavily-Access-Mode: keyless，有每日上限）。
免 key 撞到限流時照 Retry-After 暫停（沒給就 60 秒），搜尋和擷取分開算，暫停期間直接跳過不再白打一次。

錯誤處理（官方文件）：
  Firecrawl  402 Payment Required → 額度用完，標記到下個重置日
  Tavily     432 Plan limit / 433 PayGo limit → 額度用完，標記到下個重置日
  兩家       429 → 限流，標記 60 秒後再試
  兩家       401 → key 無效，標記後換下一把
"""
from __future__ import annotations

import calendar
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# 同資料夾的 page.py（插件不是一般套件，用檔案路徑載入）
_spec = importlib.util.spec_from_file_location(__name__ + "_page", Path(__file__).with_name("page.py"))
page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page)

_TIMEOUT = httpx.Timeout(30.0, read=90.0)
CACHE_TTL = 3600   # 同一網址一小時內再擷取直接讀存檔
# Firecrawl 抓取時先排除的頁面外框：導覽、側欄、最外層頁首頁尾、維基導覽框與語言清單、Sphinx 側欄
_EXCLUDE = ["nav", "aside", "body > header", "body > footer", "[role=navigation]", "[role=banner]",
            "[role=contentinfo]", ".navbox", ".vertical-navbox", ".sphinxsidebar", ".related",
            ".mw-jump-link", "#p-lang", "#siteNotice", ".toc", "#toc", ".ambox", ".mbox-small"]
_COOL: dict[tuple[str, str], float] = {}   # (免 key 服務, search/extract) → 暫停到幾點


def _retry_after(r: httpx.Response) -> float:
    try:
        sec = float(r.headers.get("retry-after") or 0)
    except ValueError:
        sec = 0
    if sec <= 0:
        sec = 60 if r.status_code == 429 or r.status_code >= 500 else 600
    return min(sec, 86400)


def _next_reset(now: float, day: int = 1) -> float:
    """下一次帳單重置時間（本機時間 00:00）。day 為每月重置日（1–31，超過當月天數取月底）。"""
    import calendar
    t = time.localtime(now)
    day = max(1, min(int(day or 1), 31))
    for add in (0, 1, 2):
        m0 = t.tm_mon + add
        y, m = t.tm_year + (m0 - 1) // 12, (m0 - 1) % 12 + 1
        d = min(day, calendar.monthrange(y, m)[1])
        ts = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
        if ts > now:
            return ts
    return ts


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"\n…（已截斷，原文 {len(text)} 字）"


def register(woof):
    def providers() -> list[str]:
        raw = woof.settings.get("order") or "firecrawl_keyless,tavily_keyless,firecrawl,tavily"
        return [p.strip() for p in str(raw).split(",") if p.strip()]

    def limit() -> int:
        return max(1, min(int(woof.settings.get("results") or 5), 10))

    def num(key: str, dflt: int, lo: int) -> int:
        try:
            return max(lo, int(woof.settings.get(key) or dflt))
        except (TypeError, ValueError):
            return dflt

    def cache_dir():
        from woof.config import DATA
        d = DATA / "web"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cache_path(url: str):
        host = (urlparse(url).hostname or "page").replace(":", "_")
        return cache_dir() / f"{host}-{hashlib.sha1(url.encode()).hexdigest()[:10]}.md"

    def cache_get(url: str) -> dict | None:
        p = cache_path(url)
        meta = p.with_suffix(".json")
        if not p.exists() or not meta.exists() or time.time() - p.stat().st_mtime > CACHE_TTL:
            return None
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {"url": url, "text": p.read_text(encoding="utf-8"), "title": m.get("title", ""),
                "desc": m.get("desc", ""), "path": str(p), "raw": m.get("raw", 0)}

    def cache_put(url: str, md: str, title: str, desc: str) -> dict:
        text = page.clean(md)
        p = cache_path(url)
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".json").write_text(json.dumps({"url": url, "title": title, "desc": desc, "raw": len(md or "")},
                                                     ensure_ascii=False), encoding="utf-8")
        old = time.time() - 7 * 86400   # 存檔留 7 天
        for f in cache_dir().iterdir():
            try:
                if f.stat().st_mtime < old:
                    f.unlink()
            except OSError:
                pass
        return {"url": url, "text": text, "title": title, "desc": desc, "path": str(p), "raw": len(md or "")}

    def render(d: dict, keys: list[str], full: bool) -> str:
        return page.preview(d, keys, full, {
            "preview": num("preview_chars", 4000, 1000), "hit": num("hit_chars", 3000, 500),
            "lead": num("lead_chars", 500, 100), "outline": num("outline_chars", 600, 200)})

    async def _post(client: httpx.AsyncClient, url: str, key: str, body: dict) -> dict:
        if key:
            headers = {"Authorization": f"Bearer {key}"}
        else:
            headers = {"X-Tavily-Access-Mode": "keyless"} if "api.tavily.com" in url else {}
        r = await client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(r.text[:300], request=r.request, response=r)
        return r.json()

    async def _search_firecrawl(client, key: str, query: str) -> str:
        data = await _post(client, "https://api.firecrawl.dev/v2/search", key,
                           {"query": query, "limit": limit()})
        rows = (data.get("data") or {}).get("web") or []
        if not rows:
            return "沒有找到結果。"
        out = []
        for i, it in enumerate(rows, 1):
            out.append(f"{i}. {it.get('title') or '（無標題）'}\n   {it.get('url', '')}\n   {_clip(it.get('description') or '', 400)}")
        return "\n\n".join(out)

    async def _search_tavily(client, key: str, query: str) -> str:
        data = await _post(client, "https://api.tavily.com/search", key,
                           {"query": query, "max_results": limit(), "search_depth": "basic"})
        rows = data.get("results") or []
        if not rows:
            return "沒有找到結果。"
        out = []
        for i, it in enumerate(rows, 1):
            out.append(f"{i}. {it.get('title') or '（無標題）'}\n   {it.get('url', '')}\n   {_clip(it.get('content') or '', 400)}")
        return "\n\n".join(out)

    async def _extract_firecrawl(client, key: str, urls: list[str]) -> list[tuple[str, str]]:
        # batch/scrape 是非同步工作，結果要另外輪詢；逐頁 scrape 一次就拿得到全文。
        out = []
        for url in urls:
            data = await _post(client, "https://api.firecrawl.dev/v2/scrape", key,
                               {"url": url, "formats": ["markdown"], "onlyMainContent": True,
                                "removeBase64Images": True, "excludeTags": _EXCLUDE})
            d = data.get("data") or {}
            m = d.get("metadata") or {}
            out.append((url, d.get("markdown") or d.get("content") or "", m.get("title") or "", m.get("description") or ""))
        return out

    async def _extract_tavily(client, key: str, urls: list[str]) -> list[tuple[str, str]]:
        data = await _post(client, "https://api.tavily.com/extract", key, {"urls": urls, "format": "markdown"})
        return [(it.get("url", ""), it.get("raw_content") or "", it.get("title") or "", "")
                for it in (data.get("results") or [])]

    SEARCH = {"firecrawl": _search_firecrawl, "tavily": _search_tavily,
              "firecrawl_keyless": lambda c, _k, q: _search_firecrawl(c, None, q),
              "tavily_keyless": lambda c, _k, q: _search_tavily(c, None, q)}
    EXTRACT = {"firecrawl": _extract_firecrawl, "tavily": _extract_tavily,
               "firecrawl_keyless": lambda c, _k, u: _extract_firecrawl(c, None, u),
               "tavily_keyless": lambda c, _k, u: _extract_tavily(c, None, u)}

    def billing_day(name: str) -> int:
        return int(woof.settings.get(f"{name}_billing_day") or 1)

    def _status_for(code: int, name: str) -> tuple[str, float] | None:
        now = time.time()
        if code in (402, 432, 433):
            return "exhausted", _next_reset(now, billing_day(name))
        if code == 429:
            return "rate_limited", now + 60
        if code == 401:
            return "invalid", now + 86400 * 365
        return None

    async def _run(kind: str, arg, fmt=None) -> str:
        table = SEARCH if kind == "search" else EXTRACT
        tried = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for name in providers():
                fn = table.get(name)
                if fn is None:
                    tried.append(f"{name}：不支援")
                    continue
                if name.endswith("_keyless"):
                    who = name[:-len("_keyless")]
                    wait = _COOL.get((name, kind), 0) - time.time()
                    if wait > 0:
                        tried.append(f"{name}：限流中，約 {int(wait)} 秒後恢復")
                        continue
                    try:
                        result = await fn(client, None, arg)
                    except httpx.HTTPStatusError as e:
                        sec = _retry_after(e.response)
                        _COOL[(name, kind)] = time.time() + sec
                        tried.append(f"{name}：HTTP {e.response.status_code}（免 key 限流，暫停 {int(sec)} 秒）")
                        continue
                    except httpx.HTTPError as e:
                        tried.append(f"{name}：{type(e).__name__}")
                        continue
                    if kind == "search":
                        return f"（{who} 免 key）\n" + result
                    return f"（{who} 免 key）\n" + fmt(result)
                pool = woof.keys(name)
                while True:
                    entry = pool.get()
                    if entry is None:
                        tried.append(f"{name}：沒有可用的 key")
                        break
                    try:
                        result = await fn(client, entry.key, arg)
                    except httpx.HTTPStatusError as e:
                        st = _status_for(e.response.status_code, name)
                        if st:
                            pool.mark(entry, st[0], st[1], f"HTTP {e.response.status_code}")
                            tried.append(f"{name}/{entry.label}：HTTP {e.response.status_code}，已換下一把")
                            continue
                        return f"{name} 回傳 HTTP {e.response.status_code}：{e.response.text[:300]}"
                    except httpx.HTTPError as e:
                        return f"{name} 連線失敗：{type(e).__name__}: {e}"
                    head = f"（{name} / {entry.label}）\n" if tried else ""
                    if kind == "search":
                        return head + result
                    return head + fmt(result)
        return "所有搜尋服務都無法使用：\n" + "\n".join(f"- {t}" for t in tried) + "\n請到「額度」面板查看各 key 狀態，或在 keys.toml 加 key。"

    async def web_search(args: dict, ctx) -> dict:
        q = (args.get("query") or "").strip()
        if not q:
            return {"output": "query 不能是空的", "is_error": True}
        return {"output": await _run("search", q)}

    async def web_extract(args: dict, ctx) -> dict:
        urls = [u.strip() for u in (args.get("urls") or []) if isinstance(u, str) and u.strip()][:5]
        if not urls:
            return {"output": "urls 不能是空的", "is_error": True}
        bad = [u for u in urls if not u.startswith(("http://", "https://"))]
        if bad:
            return {"output": "網址必須以 http:// 或 https:// 開頭：" + ", ".join(bad), "is_error": True}
        raw_find = args.get("find") or []
        if isinstance(raw_find, str):
            raw_find = raw_find.replace("，", ",").split(",")
        keys = [k.strip() for k in raw_find if isinstance(k, str) and k.strip()][:8]
        full = bool(args.get("full"))
        done = {u: c for u in urls if (c := cache_get(u))}
        miss = [u for u in urls if u not in done]
        note = ""
        if miss:
            def fmt(result):
                for u, md, title, desc in result:
                    if md:
                        done[u] = cache_put(u, md, title, desc)
                return ""
            note = await _run("extract", miss, fmt)
        blocks = [render(done[u], keys, full) if u in done else f"## {u}\n\n沒有擷取到內容。" for u in urls]
        return {"output": (note + "\n" if note.strip() else "") + "\n\n".join(blocks)}

    woof.add_tool("web_search", "搜尋網路，回傳標題、網址和摘要。需要網頁全文時再用 web_extract。",
                  {"type": "object", "properties": {"query": {"type": "string", "description": "搜尋關鍵字"}},
                   "required": ["query"]}, web_search)
    woof.add_tool("web_extract", "擷取最多 5 個網頁。短的直接給全文；長的先給預覽：標題、摘要、標題目錄（附存檔行號）、開頭，"
                  "以及 find 關鍵字命中的段落和前後文。全文都會存檔：要整篇設 full=true，要某一節用 read_file 加目錄行號；"
                  "同網址換 find 或改 full 會讀存檔，不重抓。",
                  {"type": "object", "properties": {
                      "urls": {"type": "array", "items": {"type": "string"}, "description": "網址，最多 5 個"},
                      "find": {"type": "array", "items": {"type": "string"},
                               "description": "想在頁面裡找的字詞，可以多個；不確定找什麼就不填"},
                      "full": {"type": "boolean", "description": "true＝直接給整篇全文"}},
                   "required": ["urls"]}, web_extract)
