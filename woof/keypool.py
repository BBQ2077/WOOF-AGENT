"""金鑰池：同一服務多把 API key 的挑選與狀態。

第一階段提供基本挑選與標記；額度查詢、重置日計算、跨服務備援在第二階段加入。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import load_keys


def reset_at(body: str) -> float | None:
    """錯誤訊息裡的額度重置時間（例如「quota resets at 2026-11-20T02:05」）；沒寫時區當本機時間。"""
    mm = re.search(r"reset\w*\s+(?:at|on)\s+(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)", body or "", re.I)
    if not mm:
        return None
    try:
        t = datetime.fromisoformat(mm.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    ts = t.timestamp()   # 沒有時區就是本機時間
    return ts if ts > time.time() else None


@dataclass
class KeyEntry:
    key: str
    label: str
    status: str = "ok"          # ok / exhausted / rate_limited / invalid / disabled
    until: float | None = None  # 狀態到期時間（epoch 秒）
    note: str = ""

    def available(self) -> bool:
        if self.status == "ok":
            return True
        if self.status in ("exhausted", "rate_limited") and self.until and time.time() >= self.until:
            self.status, self.until, self.note = "ok", None, ""
            return True
        return False

    def public(self) -> dict:
        return {"label": self.label, "status": self.status, "until": self.until, "note": self.note,
                "hint": self.key[:6] + "…" + self.key[-4:] if len(self.key) > 12 else "…"}


@dataclass
class KeyPool:
    name: str
    entries: list[KeyEntry] = field(default_factory=list)

    def get(self) -> KeyEntry | None:
        for e in self.entries:
            if e.available():
                return e
        return None

    def mark(self, entry: KeyEntry, status: str, until: float | None = None, note: str = "") -> None:
        entry.status, entry.until, entry.note = status, until, note


POOLS: dict[str, KeyPool] = {}


def reload_pools() -> None:
    old = {(p.name, e.key): e for p in POOLS.values() for e in p.entries}
    POOLS.clear()
    for name, items in load_keys().items():
        pool = KeyPool(name)
        for i, it in enumerate(items):
            prev = old.get((name, it["key"]))
            entry = prev or KeyEntry(key=it["key"], label=it.get("label") or f"#{i + 1}")
            entry.label = it.get("label") or entry.label
            pool.entries.append(entry)
        POOLS[name] = pool


def pool(name: str) -> KeyPool:
    if not POOLS:
        reload_pools()
    return POOLS.get(name) or KeyPool(name)


def snapshot() -> dict:
    return {n: [e.public() for e in p.entries] for n, p in POOLS.items()}
