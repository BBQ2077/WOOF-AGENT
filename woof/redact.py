"""金鑰遮罩：工具輸出（終端、讀檔、網頁…）在送進模型、存進對話、顯示在畫面之前，把金鑰換成「前 6 碼…後 4 碼」。

兩層：
1. keys.toml 裡的每一把 key（逐字比對，最準）
2. 常見格式：sk-、sk-ant-、nvapi-、fc-、tvly-、ghp_、github_pat_、AKIA、AIza、hf_、xai-、Bearer、
   以及 api_key=…、"token": "…" 這類寫法
設定 config.toml 的 redact_secrets = false 可關掉（例如在寫遮罩本身時）。
"""
from __future__ import annotations

import re
import time

# (樣式, 要遮的是第幾組)
_PATTERNS = [
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{20,}|sk-(?:proj-|or-v1-)?[A-Za-z0-9_\-]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|"
                r"fc-[A-Za-z0-9]{20,}|tvly-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
                r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|hf_[A-Za-z0-9]{30,}|xai-[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_\-]{20,}|"
                r"xox[baprs]-[A-Za-z0-9\-]{20,})"), 1),
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{20,})"), 2),
    (re.compile(r"(?i)([\"']?(?:api[_-]?key|apikey|secret|token|password|passwd|access[_-]?key)[\"']?\s{0,3}[:=]\s{0,3}[\"']?)"
                r"(?!dpapi:)([A-Za-z0-9._\-+/=]{16,})"), 2),
]
_cache: dict = {"at": 0.0, "keys": []}


def mask(v: str) -> str:
    return v[:6] + "…" + v[-4:] if len(v) >= 18 else "***"


def _known() -> list[str]:
    if time.time() - _cache["at"] > 10:
        try:
            from .config import load_keys

            ks = {e["key"] for es in load_keys().values() for e in es if len(e.get("key") or "") >= 8}
        except Exception:
            ks = set()
        _cache.update(at=time.time(), keys=sorted(ks, key=len, reverse=True))
    return _cache["keys"]


def enabled() -> bool:
    if time.time() - _cache.get("en_at", 0) > 10:
        try:
            from .config import load_config

            _cache["en"] = load_config().get("redact_secrets", True) is not False
        except Exception:
            _cache["en"] = True
        _cache["en_at"] = time.time()
    return _cache["en"]


def redact(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    for k in _known():
        if k in text:
            text = text.replace(k, mask(k))
    for p, g in _PATTERNS:
        def _sub(m, g=g):
            s = m.group(0)
            v = m.group(g)
            return s[:m.start(g) - m.start(0)] + mask(v) + s[m.end(g) - m.start(0):]
        text = p.sub(_sub, text)
    return text
