"""模型能力的預設值與自動偵測。

原則（照常見做法：先假設有，撞到再退）：
  - 思考：OpenAI 相容的模型沒寫 efforts 時，一律當成支援思考，預設「高」。
    effort_style 沒寫時依位址判斷送法（OpenRouter／DeepSeek／NVIDIA NIM／其他一律 OpenAI 的 reasoning_effort）。
  - 看圖：沒寫 vision 時當成會看圖（自動）；手動寫 true／false 時照寫的，自動偵測不會蓋掉。
  - 撞到「不支援這個參數」或「不能看圖」的錯誤：記到 data/model_caps.json，下次就不送；
    看圖改走「設定 → 輔助模型 → 看圖」先把圖轉成文字。設定頁可以清掉重新偵測。
  - 被限流（429）沒設等待秒數時，預設最多等 30 秒。
"""
from __future__ import annotations

import json
import threading
import time

from .config import DATA

FILE = DATA / "model_caps.json"
_lock = threading.Lock()
_cache: dict | None = None

EFFORTS_STD = ["off", "low", "medium", "high"]
EFFORTS_BY_STYLE = {
    "nim": ["off", "on"],
    "deepseek": ["off", "low", "high", "max"],
}
# DeepSeek 走 OpenAI 相容代理（opencode zen 等，位址不是 api.deepseek.com）時，檔位照 DeepSeek 官方文件：
# reasoning_effort 收 low/high/max，沒有 medium（medium 會被對映成 high）。官方 thinking_mode 文件 2026-11 查證。
# 這種代理只送頂層 reasoning_effort，關不掉思考，所以不列 "off"。
EFFORTS_DEEPSEEK_PROXY = ["low", "high", "max"]
DEFAULT_WAIT = 30

# 錯誤字樣（小寫比對）
UNSUPPORTED_PARAM = ("unknown parameter", "unsupported parameter", "unrecognized request argument",
                     "unrecognized parameter", "extra inputs are not permitted", "not supported",
                     "does not support", "doesn't support", "unsupported_parameter", "unknown_parameter",
                     "invalid parameter", "unexpected keyword", "additional properties", "not allowed")
EFFORT_WORDS = ("reasoning", "thinking", "chat_template_kwargs", "enable_thinking", "effort")
IMAGE_REJECT = ("only 'text' content type is supported", "only text content type is supported",
                "image_url is not supported", "image content is not supported", "multimodal is not supported",
                "multimodal content is not supported", "multimodal input is not supported",
                "vision is not supported", "vision input is not supported", "does not support images",
                "does not support image", "does not support multimodal", "does not support vision",
                "unexpected item type in content", "unknown variant `image_url`", "unknown variant image_url",
                "no endpoints found that support image input", "image input is not supported",
                "images are not supported", "image_url' is not supported", "invalid content type",
                "multimodal processing is not enabled", "received multimodal data", "does not accept images")


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def learned(model_id: str) -> dict:
    with _lock:
        return dict(_load().get(model_id) or {})


def learn(model_id: str, **flags) -> None:
    with _lock:
        d = _load()
        cur = d.setdefault(model_id, {})
        cur.update(flags)
        cur["at"] = time.time()
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def forget(model_id: str | None = None) -> None:
    with _lock:
        d = _load()
        if model_id is None:
            d.clear()
        else:
            d.pop(model_id, None)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def guess_style(base_url: str) -> str:
    b = (base_url or "").lower()
    if "openrouter.ai" in b:
        return "openrouter"
    if "deepseek.com" in b:
        return "deepseek"
    if "nvidia.com" in b:
        return "nim"
    return "openai"


def apply(m: dict) -> dict:
    """補上沒寫的能力欄位（原地修改並回傳）。只處理 OpenAI 相容端點；Claude 系列照設定檔。"""
    got = learned(m["id"])
    if m.get("provider") == "openai_compat":
        if not m.get("effort_style"):
            m["effort_style"] = guess_style(m.get("base_url") or "")
        if got.get("no_effort"):
            m["effort_style"] = "none"
            m["efforts"] = []
            m["default_effort"] = None
        elif m["effort_style"] != "none" and not m.get("efforts"):
            if "deepseek" in f"{m.get('id', '')} {m.get('model', '')}".lower() and m["effort_style"] != "deepseek":
                m["efforts"] = list(EFFORTS_DEEPSEEK_PROXY)
            else:
                m["efforts"] = list(EFFORTS_BY_STYLE.get(m["effort_style"], EFFORTS_STD))
        if m.get("efforts") and m.get("default_effort") not in m["efforts"]:
            m["default_effort"] = "high" if "high" in m["efforts"] else ("on" if "on" in m["efforts"] else m["efforts"][-1])
        if m.get("efforts"):  # 設定檔寫 MAX / Max 也認：內部一律小寫，顯示才用中文標籤（前端 EFFORT_LABEL）
            m["efforts"] = [str(e).strip().lower() for e in m["efforts"] if str(e).strip()]
            if isinstance(m.get("default_effort"), str):
                m["default_effort"] = m["default_effort"].strip().lower()
        if m.get("rate_limit_wait") is None:
            m["rate_limit_wait"] = DEFAULT_WAIT
    # 看圖三種：沒寫 vision＝自動（先直接送，被拒才記住改走看圖模型）；寫 true／false＝手動指定，蓋過自動偵測
    m["vision_mode"] = "auto" if m.get("vision") is None else ("on" if m["vision"] else "off")
    m["vision"] = m["vision_mode"] != "off"
    if got.get("no_vision") and m["vision_mode"] == "auto":
        m["vision"] = False
    m["learned"] = {k: v for k, v in got.items() if k != "at"}
    return m


# 某個強度值被拒（例如 muse-ember 不收 reasoning_effort="none"）：換成相近的值，不是整個關掉思考
EFFORT_FALLBACK = {"none": "minimal", "minimal": "low", "xhigh": "high", "max": "high"}


def effort_value(model_id: str, v: str | None) -> str | None:
    """實際要送的強度值：跳過這個模型拒收過的值；沒得換就回 None（不送）。"""
    bad = set(learned(model_id).get("bad_effort") or [])
    while v in bad and v in EFFORT_FALLBACK:
        v = EFFORT_FALLBACK[v]
    return None if (v is None or v in bad) else v


def learn_bad_effort(model_id: str, v: str) -> None:
    bad = list(learned(model_id).get("bad_effort") or [])
    if v not in bad:
        bad.append(v)
    learn(model_id, bad_effort=bad)


_DOWN: dict[str, tuple[float, str]] = {}   # 暫時不能用的模型（服務商暫停、下架）：id → (到期時間, 原因)；只放記憶體


def is_unavailable(text: str) -> bool:
    """服務商說這個模型現在不能用（免費路由暫停、沒有可用節點、下架）。"""
    t = (text or "").lower()
    return any(x in t for x in ("has paused", "promotion", "no endpoints", "model not found", "does not exist",
                                "is not available", "currently unavailable", "no available", "deprecated", "decommissioned"))


def mark_down(model_id: str, why: str, secs: int = 3600) -> None:
    _DOWN[model_id] = (time.time() + secs, why)


def down(model_id: str) -> str:
    """暫停中就回傳原因，否則空字串。"""
    v = _DOWN.get(model_id)
    if not v:
        return ""
    if v[0] < time.time():
        _DOWN.pop(model_id, None)
        return ""
    return v[1]


def is_image_reject(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in IMAGE_REJECT)


def is_effort_reject(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in UNSUPPORTED_PARAM) and any(w in t for w in EFFORT_WORDS)
