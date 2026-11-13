"""模型商自動檢索：讀各模型商的 /models，結果存快取；使用者勾選後才寫進 config.toml 的 [[models]]。

- 檢索本身不改設定；勾選寫入時同一模型商、同一模型名只會有一筆。
- 沒有金鑰的模型商不檢索。
- 快取在 data/cache/models/<模型商>.json，記錄上次檢索時間與錯誤，用來判斷要不要重抓。
- Claude Code CLI 不做檢索。
- 有些模型商的 /v1/models 不完整（例如 Respan 不列 :free 免費路由），改讀它的完整目錄（CATALOGS）。
- 每筆記下 free（免費）、vision（目錄寫的輸入類型，只當參考，不寫進設定）、reasoning（思考）。
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .config import DATA, load_config, load_keys, save_config

CACHE = DATA / "cache" / "models"
TTL = 6 * 3600
SKIP_PROVIDER = {"claude_cli"}  # 沒有 CLI 就沒辦法列模型


def wants_auto(v: dict) -> bool:
    if v.get("auto_models") is False:
        return False
    if v.get("provider") in SKIP_PROVIDER:
        return False
    return bool(v.get("base_url") or v.get("provider") == "anthropic")


def _cache_path(vid: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / (re.sub(r"[^\w.-]", "_", vid) + ".json")


def read_cache(vid: str) -> dict:
    try:
        return json.loads(_cache_path(vid).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(vid: str, data: dict) -> None:
    _cache_path(vid).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _is_free(name: str, pricing) -> bool:
    if re.search(r"[:/-]free$", name or "", re.I):
        return True
    if isinstance(pricing, dict):
        vals = [pricing.get(k) for k in ("prompt", "completion")]
        try:
            return all(v is not None and float(v) == 0 for v in vals)
        except (TypeError, ValueError):
            return False
    return False


async def _respan_catalog(key: str) -> list[dict]:
    """Respan 的完整目錄（api.respan.ai/api/models，分頁）；/v1/models 沒有 :free 路由。"""
    out, url = [], "https://api.respan.ai/api/models?page_size=1000"
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
        for _ in range(20):
            r = await c.get(url, headers={"Authorization": f"Bearer {key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}：{r.text[:160]}")
            d = r.json()
            for it in d.get("results") or []:
                name = it.get("model_name") or ""
                if not name:
                    continue
                mods = it.get("input_modalities")
                cap = it.get("capability") or {}
                out.append({
                    "model": name,
                    "label": name,
                    "context_window": it.get("max_context_window") or None,
                    "vision": ("image" in str(mods)) if mods else None,
                    "reasoning": bool(cap.get("reasoning_support")) if "reasoning_support" in cap else None,
                    "free": name.endswith(":free"),
                })
            url = d.get("next")
            if not url:
                break
    return out


# 模型商自己的完整目錄：名稱 → (位址特徵, 讀法)；vendor 也可以用 catalog = "名稱" 指定
CATALOGS = {"respan": ("respan.ai", _respan_catalog)}


async def fetch(v: dict) -> list[dict]:
    key = ""
    if v.get("key_pool"):
        entries = load_keys().get(v["key_pool"]) or []
        key = entries[0]["key"] if entries else ""
    if v.get("provider") == "anthropic":
        url = (v.get("base_url") or "https://api.anthropic.com").rstrip("/") + "/v1/models?limit=1000"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        base = (v.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("沒有 API 位址")
        url = base + "/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        for name, (host, fn) in CATALOGS.items():
            if v.get("catalog") == name or host in base:
                return await fn(key)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        r = await c.get(url, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}：{r.text[:160]}")
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None:
        items = data.get("models")
    out = []
    for it in items or []:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if not mid:
                continue
            mods = (it.get("architecture") or {}).get("input_modalities") or it.get("input_modalities")
            params = it.get("supported_parameters")
            out.append({
                "model": str(mid),
                "label": str(it.get("display_name") or it.get("name") or mid),
                "context_window": it.get("context_length") or it.get("context_window") or None,
                "vision": ("image" in str(mods)) if mods else None,
                "reasoning": ("reasoning" in params or "include_reasoning" in params) if isinstance(params, list) else None,
                "free": _is_free(str(mid), it.get("pricing")),
            })
        elif isinstance(it, str):
            out.append({"model": it, "label": it})
    return out


def add_to_config(vendor_id: str, items: list[dict]) -> tuple[int, int]:
    """把檢索到的模型加進 config.toml [[models]]。回傳 (新增幾個, 已存在幾個)。"""
    cfg = load_config()
    models = cfg.get("models") or []
    have = {(m.get("vendor") or "", m.get("model") or "") for m in models}
    ids = {m.get("id") for m in models}
    added = skipped = 0
    for it in items:
        if (vendor_id, it["model"]) in have:
            skipped += 1
            continue
        base = f"{vendor_id}:{it['model']}"
        mid, n = base, 2
        while mid in ids:
            mid = f"{base}#{n}"
            n += 1
        m = {"id": mid, "vendor": vendor_id, "model": it["model"], "label": it.get("label") or it["model"]}
        if it.get("context_window"):
            m["context_window"] = int(it["context_window"])
        # 目錄寫「只收文字」不寫進 vision：有的模型商目錄寫錯（OpenDesign 的 deepseek-v4.1-flash-eco
        # 目錄只列 text，實際看得到圖）。不寫＝先直接送圖，被拒才記成不能看圖、改走看圖模型（woof/caps.py）
        if it.get("reasoning") is False:
            m["effort_style"] = "none"   # 目錄明說沒有思考；沒寫＝預設有思考、強度高
        models.append(m)
        ids.add(mid)
        have.add((vendor_id, it["model"]))
        added += 1
    if added:
        cfg["models"] = models
        save_config(cfg)
    return added, skipped


def has_key(v: dict) -> bool:
    """有沒有能用的金鑰。本機端點（127.0.0.1 等）不需要金鑰，視為有。"""
    if v.get("key_pool") and load_keys().get(v["key_pool"]):
        return True
    base = (v.get("base_url") or "")
    return any(h in base for h in ("127.0.0.1", "localhost", "[::1]"))


async def refresh(v: dict) -> dict:
    """抓模型清單，只寫快取，不改 config.toml（要加哪些由使用者勾選）。"""
    vid = v["id"]
    if not has_key(v):
        return {"ok": False, "skipped": True, "count": 0, "error": "沒有金鑰，略過檢索", "at": time.time()}
    try:
        items = await fetch(v)
    except Exception as e:
        old = read_cache(vid)
        _write_cache(vid, {"at": time.time(), "count": old.get("count", 0), "items": old.get("items", []),
                           "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "skipped": False, "count": 0, "error": f"{type(e).__name__}: {e}", "at": time.time()}
    _write_cache(vid, {"at": time.time(), "count": len(items), "items": items, "error": ""})
    return {"ok": True, "skipped": False, "count": len(items), "error": "", "at": time.time()}


def pick(vendor_id: str, model_names: list[str]) -> tuple[int, int]:
    """使用者勾選後才寫進 config.toml。回傳 (新增幾個, 已存在幾個)。"""
    items = [it for it in (read_cache(vendor_id).get("items") or []) if it["model"] in set(model_names)]
    return add_to_config(vendor_id, items)
