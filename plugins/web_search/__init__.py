"""網路搜尋與網頁擷取。

搜尋順序由設定 order 決定（預設 firecrawl_keyless → firecrawl → tavily）：先用 Firecrawl 官方免 key 端點省額度，
限流或失敗才換 key。同一家有多把 key 時一把用到完再換下一把；全部用完就換下一家。key 不刪除，到期自動恢復。
firecrawl_keyless 是官方免 key 端點（/v2/scrape、/v2/search，不帶 Authorization，有每 IP 限流）。

錯誤處理（官方文件）：
  Firecrawl  402 Payment Required → 額度用完，標記到下個重置日
  Tavily     432 Plan limit / 433 PayGo limit → 額度用完，標記到下個重置日
  兩家       429 → 限流，標記 60 秒後再試
  兩家       401 → key 無效，標記後換下一把
"""
from __future__ import annotations

import calendar
import time

import httpx

_TIMEOUT = httpx.Timeout(30.0, read=90.0)


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
        raw = woof.settings.get("order") or "firecrawl_keyless,firecrawl,tavily"
        return [p.strip() for p in str(raw).split(",") if p.strip()]

    def limit() -> int:
        return max(1, min(int(woof.settings.get("results") or 5), 10))

    def chars() -> int:
        return max(2000, int(woof.settings.get("extract_chars") or 12000))

    async def _post(client: httpx.AsyncClient, url: str, key: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
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
                               {"url": url, "formats": ["markdown"]})
            d = data.get("data") or {}
            out.append((url, d.get("markdown") or d.get("content") or "（沒有內容）"))
        return out

    async def _extract_tavily(client, key: str, urls: list[str]) -> list[tuple[str, str]]:
        data = await _post(client, "https://api.tavily.com/extract", key, {"urls": urls})
        return [(it.get("url", ""), it.get("raw_content") or "（沒有內容）") for it in (data.get("results") or [])]

    SEARCH = {"firecrawl": _search_firecrawl, "tavily": _search_tavily,
              "firecrawl_keyless": lambda c, _k, q: _search_firecrawl(c, None, q)}
    EXTRACT = {"firecrawl": _extract_firecrawl, "tavily": _extract_tavily,
               "firecrawl_keyless": lambda c, _k, u: _extract_firecrawl(c, None, u)}

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

    async def _run(kind: str, arg) -> str:
        table = SEARCH if kind == "search" else EXTRACT
        tried = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for name in providers():
                fn = table.get(name)
                if fn is None:
                    tried.append(f"{name}：不支援")
                    continue
                if name.endswith("_keyless"):
                    try:
                        result = await fn(client, None, arg)
                    except httpx.HTTPStatusError as e:
                        tried.append(f"{name}：HTTP {e.response.status_code}（免 key 有每 IP 限流）")
                        continue
                    except httpx.HTTPError as e:
                        tried.append(f"{name}：{type(e).__name__}")
                        continue
                    if kind == "search":
                        return "（firecrawl 免 key）\n" + result
                    blocks = [f"## {u}\n\n{_clip(body, chars())}" for u, body in result]
                    return "（firecrawl 免 key）\n" + ("\n\n".join(blocks) or "沒有擷取到內容。")
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
                    blocks = [f"## {u}\n\n{_clip(body, chars())}" for u, body in result]
                    return head + ("\n\n".join(blocks) or "沒有擷取到內容。")
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
        return {"output": await _run("extract", urls)}

    woof.add_tool("web_search", "搜尋網路，回傳標題、網址和摘要。需要網頁全文時再用 web_extract。",
                  {"type": "object", "properties": {"query": {"type": "string", "description": "搜尋關鍵字"}},
                   "required": ["query"]}, web_search)
    woof.add_tool("web_extract", "擷取最多 5 個網頁的全文（Markdown）。",
                  {"type": "object", "properties": {
                      "urls": {"type": "array", "items": {"type": "string"}, "description": "網址，最多 5 個"}},
                   "required": ["urls"]}, web_extract)
