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

import asyncio
import calendar
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# 同資料夾的 page.py（插件不是一般套件，用檔案路徑載入）
_spec = importlib.util.spec_from_file_location(__name__ + "_page", Path(__file__).with_name("page.py"))
page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page)
_CLEAN_VER = hashlib.sha1(Path(page.__file__).read_bytes()).hexdigest()[:8]   # page.py 改了，舊存檔會重新整理

_PAGE_SEC = 10   # 每頁擷取上限：正常 1–3 秒，大頁約 8 秒；超過就換下一家
_TIMEOUT = httpx.Timeout(8.0, read=_PAGE_SEC + 5)
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
            if m.get("ver") != _CLEAN_VER:   # 清理規則改過：用原始內容重新整理，不重抓
                return cache_put(url, p.with_suffix(".raw").read_text(encoding="utf-8"), m.get("title", ""),
                                 m.get("desc", ""), keep_time=True)
            return {"url": url, "text": p.read_text(encoding="utf-8"), "title": m.get("title", ""),
                    "desc": m.get("desc", ""), "path": str(p), "raw": m.get("raw", 0)}
        except (OSError, ValueError):
            return None

    def cache_put(url: str, md: str, title: str, desc: str, keep_time: bool = False) -> dict:
        text = page.clean(md)
        p = cache_path(url)
        t0 = p.stat().st_mtime if keep_time and p.exists() else None
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".raw").write_text(md or "", encoding="utf-8")
        p.with_suffix(".json").write_text(json.dumps({"url": url, "title": title, "desc": desc, "raw": len(md or ""),
                                                      "ver": _CLEAN_VER}, ensure_ascii=False), encoding="utf-8")
        if t0:
            os.utime(p, (t0, t0))
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

    def _why(r: httpx.Response) -> str:
        """單一網址抓取失敗的原因（不是整個服務限流）。"""
        try:
            d = r.json()
        except ValueError:
            d = {}
        code = str(d.get("code") or "")
        msg = str(d.get("error") or d.get("detail") or r.text or "")[:200]
        if "not support this site" in msg:
            return "此站不提供擷取（服務端封鎖）"
        if r.status_code == 408 or "TIMEOUT" in code:
            return f"逾時（{_PAGE_SEC} 秒內沒抓完）"
        if "ALL_ENGINES_FAILED" in code:
            return "抓取失敗（網站擋爬蟲、要登入或連不上）"
        return f"HTTP {r.status_code} {code or msg[:80]}".strip()

    def _fatal(r: httpx.Response) -> bool:
        """整個服務（或這把 key）暫時不能用：限流、額度、key 無效。"""
        return r.status_code in (401, 402, 429, 432, 433)

    async def _extract_firecrawl(client, key, urls: list[str]) -> tuple[dict, dict, httpx.Response | None]:
        # 每頁一個 scrape（batch/scrape 要另外輪詢），幾頁同時送
        async def one(url):
            try:
                data = await _post(client, "https://api.firecrawl.dev/v2/scrape", key, {
                    # onlyMainContent 會把做成連結的章節標題（mdBook、MDN）一起吃掉；外框改用 excludeTags 和本地清理
                    "url": url, "formats": ["markdown"], "onlyMainContent": False, "removeBase64Images": True,
                    "excludeTags": _EXCLUDE, "timeout": _PAGE_SEC * 1000})
            except httpx.HTTPStatusError as e:
                return url, None, e.response
            except httpx.TimeoutException:
                return url, None, f"逾時（{_PAGE_SEC} 秒內沒抓完）"
            except httpx.HTTPError as e:
                return url, None, f"連線失敗（{type(e).__name__}）"
            d = data.get("data") or {}
            m = d.get("metadata") or {}
            md = d.get("markdown") or d.get("content") or ""
            if not md.strip():
                return url, None, f"回傳空白（HTTP {m.get('statusCode', '?')}，可能要 JS 或登入）"
            return url, (md, m.get("title") or "", m.get("description") or ""), None
        ok, bad, stop = {}, {}, None
        for url, got, err in await asyncio.gather(*(one(u) for u in urls)):
            if got:
                ok[url] = got
            elif isinstance(err, httpx.Response):
                if _fatal(err):
                    stop = err
                else:
                    bad[url] = _why(err)
            else:
                bad[url] = err
        return ok, bad, stop

    async def _extract_tavily(client, key, urls: list[str]) -> tuple[dict, dict, httpx.Response | None]:
        try:
            data = await _post(client, "https://api.tavily.com/extract", key,
                               {"urls": urls, "format": "markdown", "timeout": _PAGE_SEC})
        except httpx.HTTPStatusError as e:
            if _fatal(e.response):
                return {}, {}, e.response
            return {}, {u: _why(e.response) for u in urls}, None
        except httpx.TimeoutException:
            return {}, {u: f"逾時（{_PAGE_SEC} 秒內沒抓完）" for u in urls}, None
        except httpx.HTTPError as e:
            return {}, {u: f"連線失敗（{type(e).__name__}）" for u in urls}, None
        ok = {it.get("url", ""): (it.get("raw_content") or "", it.get("title") or "", "")
              for it in (data.get("results") or []) if (it.get("raw_content") or "").strip()}
        bad = {}
        for it in data.get("failed_results") or []:
            u = it.get("url", "") if isinstance(it, dict) else str(it)
            bad[u] = str((it.get("error") if isinstance(it, dict) else "") or "抓取失敗")[:120]
        for u in urls:   # 網址被服務正規化過時，按順序對回去
            if u not in ok and u not in bad:
                alt = next((k for k in ok if k.rstrip("/") == u.rstrip("/")), None)
                if alt:
                    ok[u] = ok.pop(alt)
                else:
                    bad[u] = "回傳空白（可能要 JS 或登入）"
        return ok, bad, None

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

    async def _search(query: str) -> str | None:
        """依 order 逐家試，key 用完、限流、出錯都自動換下一家／下一把；全部不行回 None。"""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for name in providers():
                fn = SEARCH.get(name)
                if fn is None:
                    continue
                if name.endswith("_keyless"):
                    if _COOL.get((name, "search"), 0) > time.time():
                        continue
                    try:
                        return await fn(client, None, query)
                    except httpx.HTTPStatusError as e:
                        _COOL[(name, "search")] = time.time() + _retry_after(e.response)
                    except httpx.HTTPError:
                        pass
                    continue
                pool = woof.keys(name)
                while (entry := pool.get()) is not None:
                    try:
                        return await fn(client, entry.key, query)
                    except httpx.HTTPStatusError as e:
                        st = _status_for(e.response.status_code, name)
                        if not st:
                            break   # 這家這次出錯，換下一家
                        pool.mark(entry, st[0], st[1], f"HTTP {e.response.status_code}")
                    except httpx.HTTPError:
                        break
        return None

    async def _extract_all(urls: list[str]) -> tuple[dict, dict]:
        """依 order 逐家試。限流、額度用完：換同家下一把 key 或下一家，不算這頁失敗；
        某家判定這頁不行（被擋、逾時）：這頁之後只給別家試，不再用同一家的其他 key 浪費時間和額度。"""
        got, why = {}, {u: [] for u in urls}
        dead: dict[str, set] = {u: set() for u in urls}   # 這頁已被哪幾家判定不行
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for name in providers():
                fn = EXTRACT.get(name)
                vendor = name.replace("_keyless", "")
                left = [u for u in urls if u not in got and vendor not in dead[u]]
                if not left or fn is None:
                    continue

                def take(ok, bad):
                    got.update(ok)
                    for u, w in bad.items():
                        why[u].append(w)
                        dead[u].add(vendor)

                if name.endswith("_keyless"):
                    if _COOL.get((name, "extract"), 0) > time.time():
                        continue
                    ok, bad, stop = await fn(client, None, left)
                    if stop is not None:
                        _COOL[(name, "extract")] = time.time() + _retry_after(stop)
                    take(ok, bad)
                    continue
                pool = woof.keys(name)
                while left and (entry := pool.get()) is not None:
                    ok, bad, stop = await fn(client, entry.key, left)
                    take(ok, bad)
                    if stop is None:
                        break
                    st = _status_for(stop.status_code, name)
                    pool.mark(entry, *(st or ("rate_limited", time.time() + 60)), f"HTTP {stop.status_code}")
                    left = [u for u in left if u not in got and vendor not in dead[u]]
        return got, why

    async def web_search(args: dict, ctx) -> dict:
        q = (args.get("query") or "").strip()
        if not q:
            return {"output": "query 不能是空的", "is_error": True}
        res = await _search(q)
        if res is None:
            return {"output": "這次搜尋沒有結果回來（服務暫時忙），稍後再搜一次或換個關鍵字。"}
        return {"output": res}

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
        why = {}
        if miss:
            got, why = await _extract_all(miss)
            for u, (md, title, desc) in got.items():
                done[u] = cache_put(u, md, title, desc)

        def fail(u):
            # 只講這一頁的狀況；服務限流、換 key 這些內部輪換不給模型看，免得誤會整個工具不能用
            rs = " ".join(why.get(u) or [])
            if "封鎖" in rs or "擋爬蟲" in rs:
                cause = "這個網站不讓擷取"
            elif "逾時" in rs or "Failed to fetch" in rs or "連線失敗" in rs:
                cause = "這個網站回應太慢或連不上"
            elif "空白" in rs:
                cause = "頁面內容要登入或靠 JS 才顯示"
            else:
                cause = "這次沒抓到，稍後同網址可以再試"
            return f"## {u}\n\n這頁沒抓到：{cause}。其他網址不受影響；要這頁的內容，可以用 web_search 搜它的標題看摘要。"
        blocks = [render(done[u], keys, full) if u in done else fail(u) for u in urls]
        return {"output": "\n\n".join(blocks)}

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
