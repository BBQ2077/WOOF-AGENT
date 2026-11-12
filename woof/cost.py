"""成本：模型單價（美元／每百萬 token）換算用量金額，以及每日／每月預算。

單價填在模型設定的 price = [輸入, 輸出, 快取讀取]；快取讀取沒填就照輸入價。
沒填單價的模型不算錢（顯示「未設單價」）。
預算在 config 的 [budget]：daily、monthly（美元，0＝不限）、action：warn｜pause｜fallback。
"""
from __future__ import annotations

from datetime import datetime

from woof import store

ACTIONS = ("warn", "pause", "fallback")


def price_of(model: dict | None) -> tuple[float, float, float] | None:
    p = (model or {}).get("price")
    if not isinstance(p, (list, tuple)) or not p:
        return None
    try:
        vals = [float(x) for x in p]
    except (TypeError, ValueError):
        return None
    pin = vals[0]
    pout = vals[1] if len(vals) > 1 else pin
    pcache = vals[2] if len(vals) > 2 else pin
    return pin, pout, pcache


def cost_of(model: dict | None, inp: int, out: int, cache_read: int) -> float | None:
    pr = price_of(model)
    if pr is None:
        return None
    pin, pout, pcache = pr
    fresh = max(0, int(inp or 0) - int(cache_read or 0))
    return (fresh * pin + int(cache_read or 0) * pcache + int(out or 0) * pout) / 1_000_000


def _models(cfg: dict) -> dict[str, dict]:
    return {m.get("id"): m for m in cfg.get("models", []) or []}


def spent(cfg: dict, since: float, sid: str | None = None) -> dict:
    """since 起（或單一對話）的金額：total、依模型、依日期；沒單價的模型列在 unpriced。"""
    ms = _models(cfg)
    total, by_model, by_day, unpriced = 0.0, {}, {}, set()
    for day, model, i, o, cr in store.usage_rows(since, sid):
        c = cost_of(ms.get(model), i or 0, o or 0, cr or 0)
        if c is None:
            if (i or 0) + (o or 0):
                unpriced.add(model or "?")
            continue
        total += c
        by_model[model] = by_model.get(model, 0.0) + c
        by_day[day] = by_day.get(day, 0.0) + c
    return {"total": total, "by_model": by_model, "by_day": by_day, "unpriced": sorted(unpriced)}


def budget(cfg: dict) -> dict:
    b = cfg.get("budget") or {}
    act = str(b.get("action") or "warn")
    return {"daily": float(b.get("daily") or 0), "monthly": float(b.get("monthly") or 0),
            "action": act if act in ACTIONS else "warn"}


def clean_budget(body: dict) -> dict:
    out = {}
    for k in ("daily", "monthly"):
        try:
            v = float(body.get(k) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{k} 要是數字")
        if v < 0:
            raise ValueError(f"{k} 不能是負數")
        if v:
            out[k] = round(v, 4)
    act = str(body.get("action") or "warn")
    out["action"] = act if act in ACTIONS else "warn"
    return out


def status(cfg: dict) -> dict:
    """今天、本月花了多少，有沒有超過預算。"""
    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    mon0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    b = budget(cfg)
    today = spent(cfg, day0)["total"]
    month = spent(cfg, mon0)["total"]
    over = []
    if b["daily"] and today >= b["daily"]:
        over.append(f"今天已花 ${today:.2f}，超過每日上限 ${b['daily']:.2f}")
    if b["monthly"] and month >= b["monthly"]:
        over.append(f"本月已花 ${month:.2f}，超過每月上限 ${b['monthly']:.2f}")
    return {**b, "today": today, "month": month, "over": over, "fallback": (cfg.get("aux") or {}).get("fallback") or ""}
