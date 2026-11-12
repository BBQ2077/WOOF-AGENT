"""各家剩餘額度查詢（只讀端點，不呼叫模型、不花錢）。

支援：DeepSeek（餘額）、OpenRouter（金鑰額度）、Tavily（用量）、Firecrawl（點數）。
沒有餘額端點的商（NVIDIA NIM、Ollama 等）回傳 supported=False。
結果快取 120 秒，避免每次開面板都打一次。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from woof import keypool

CACHE_TTL = 120
_cache: dict[str, tuple[float, dict]] = {}

# 依 key_pool 名稱對應查詢函式
SUPPORTED = {"deepseek", "openrouter", "tavily", "firecrawl"}


def _get(url: str, key: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"連線失敗：{getattr(e, 'reason', e)}") from None


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _amount(v) -> str:
    """金額字串；負數代表欠費（DeepSeek 會回負的餘額）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"欠費 {abs(f):,.2f}" if f < -1e-9 else f"{f:,.2f}"


def _fetch_deepseek(key: str) -> dict:
    d = _get("https://api.deepseek.com/user/balance", key)
    items = []
    for b in d.get("balance_infos") or []:
        cur = b.get("currency", "")
        total = b.get("total_balance")
        # 這個幣別完全沒有金額就不列（DeepSeek 只有 CNY 帳戶時仍會回 USD 0）
        if all(float(b.get(k) or 0) == 0 for k in ("total_balance", "granted_balance", "topped_up_balance")):
            continue
        items.append({"label": f"餘額（{cur}）", "value": _amount(total), "unit": cur})
        if float(b.get("granted_balance") or 0) != 0:
            items.append({"label": "贈送餘額", "value": _amount(b.get("granted_balance")), "unit": cur})
        if float(b.get("topped_up_balance") or 0) != 0:
            items.append({"label": "儲值餘額", "value": _amount(b.get("topped_up_balance")), "unit": cur})
    if not items:
        items.append({"label": "餘額", "value": "0.00", "unit": ""})
    return {"items": items, "available": d.get("is_available")}


def _fetch_openrouter(key: str) -> dict:
    d = (_get("https://openrouter.ai/api/v1/key", key).get("data") or {})
    items = []
    limit = d.get("limit")
    rem = d.get("limit_remaining")
    if limit is not None:
        items.append({"label": "金鑰額度", "value": f"{_money(rem)} / {_money(limit)}", "unit": "USD",
                      "pct": None if not limit else max(0.0, min(100.0, (1 - float(rem or 0) / float(limit)) * 100))})
    else:
        items.append({"label": "金鑰額度", "value": "無上限", "unit": ""})
    items.append({"label": "本月已用", "value": _money(d.get("usage_monthly", d.get("usage"))), "unit": "USD"})
    items.append({"label": "今天已用", "value": _money(d.get("usage_daily")), "unit": "USD"})
    if d.get("is_free_tier") is not None:
        items.append({"label": "免費層", "value": "是" if d.get("is_free_tier") else "否", "unit": ""})
    return {"items": items}


def _fetch_tavily(key: str) -> dict:
    d = _get("https://api.tavily.com/usage", key)
    k = d.get("key") or {}
    a = d.get("account") or {}
    items = [{"label": "本金鑰用量", "value": f"{k.get('usage', 0)} / {k.get('limit') or '無上限'}", "unit": "次"}]
    if a:
        items.append({"label": "方案", "value": str(a.get("current_plan", "—")), "unit": ""})
        items.append({"label": "方案用量", "value": f"{a.get('plan_usage', 0)} / {a.get('plan_limit') or '—'}", "unit": "次"})
    return {"items": items}


def _fetch_firecrawl(key: str) -> dict:
    d = (_get("https://api.firecrawl.dev/v2/team/credit-usage", key).get("data") or {})
    items = [{"label": "剩餘點數", "value": f"{d.get('remainingCredits', '—')} / {d.get('planCredits', '—')}", "unit": "點"}]
    reset = None
    if d.get("billingPeriodEnd"):
        try:
            reset = datetime.fromisoformat(str(d["billingPeriodEnd"]).replace("Z", "+00:00")).isoformat()
        except ValueError:
            reset = None
    return {"items": items, "reset_at": reset}


FETCH = {"deepseek": _fetch_deepseek, "openrouter": _fetch_openrouter,
         "tavily": _fetch_tavily, "firecrawl": _fetch_firecrawl}


def _month_day(y: int, m: int, day: int) -> datetime:
    """某年某月的第 day 天；超過當月天數（例如 31 號遇到 2 月）就取當月最後一天。"""
    import calendar
    last = calendar.monthrange(y, m)[1]
    return datetime(y, m, min(int(day), last))


def next_billing_day(day: int, now: datetime | None = None) -> str | None:
    """每月 day 號為帳單重置日（1–31；超過當月天數取月底）。回傳下一次日期（YYYY-MM-DD）。"""
    if not (1 <= int(day) <= 31):
        return None
    now = now or datetime.now()
    cand = _month_day(now.year, now.month, day)
    if cand.date() <= now.date():
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        cand = _month_day(y, m, day)
    return cand.date().isoformat()


def _key_for(pool_name: str) -> str | None:
    e = keypool.pool(pool_name).get()
    return e.key if e else None


def _query_one(pool_name: str) -> dict:
    fn = FETCH.get(pool_name)
    key = _key_for(pool_name)
    if not key:
        return {"supported": True, "ok": False, "error": "沒有可用的金鑰"}
    if fn is None:
        return {"supported": False}
    try:
        out = fn(key)
        return {"supported": True, "ok": True, **out}
    except RuntimeError as e:
        return {"supported": True, "ok": False, "error": str(e)}


async def query(pool_name: str, force: bool = False) -> dict:
    now = time.time()
    hit = _cache.get(pool_name)
    if hit and not force and now - hit[0] < CACHE_TTL:
        return hit[1]
    if pool_name not in SUPPORTED:
        res = {"supported": False}
    else:
        res = await asyncio.to_thread(_query_one, pool_name)
    res["at"] = int(now)
    _cache[pool_name] = (now, res)
    return res
