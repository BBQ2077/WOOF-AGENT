"""輸出風格：內建幾種，使用者可以在設定新增、修改、刪除，按愛心置頂。

存在 data/styles.json：
  {"custom": [{"id", "name", "text"}], "edited": {內建 id: {"name", "text"}}, "pinned": [id, ...]}
每個對話在 engine_state.style 記它選的風格 id；"default" 代表不加任何段落。
"""
from __future__ import annotations

import json
import re
import threading
import uuid

from .config import DATA

PATH = DATA / "styles.json"
_LOCK = threading.Lock()

BUILTIN = [
    {"id": "explain", "name": "解說", "text": "回答時先講思路與取捨，再給結論；用小標題與條列分段，程式碼附上簡短註解。"},
    {"id": "concise", "name": "精簡", "text": "回答盡量精簡：只給結論與必要步驟，不寫客套話、不重複問題、不加總結段落。"},
    {"id": "teach", "name": "教學", "text": "這是教學情境：每個步驟都說明為什麼這樣做，遇到術語先解釋一次，最後給一個可以自己驗證的小練習。"},
]
DEFAULT = {"id": "default", "name": "預設", "text": ""}


def _read() -> dict:
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def all_styles() -> list[dict]:
    """預設在最前，接著置頂的（照置頂順序），再來其他內建、自訂。"""
    d = _read()
    edited = d.get("edited") or {}
    pinned = [x for x in d.get("pinned") or [] if isinstance(x, str)]
    items = []
    for b in BUILTIN:
        e = edited.get(b["id"]) or {}
        items.append({**b, "name": e.get("name") or b["name"], "text": e.get("text") or b["text"],
                      "builtin": True, "edited": bool(e)})
    for c in d.get("custom") or []:
        if isinstance(c, dict) and c.get("id") and c.get("text"):
            items.append({"id": c["id"], "name": c.get("name") or c["id"], "text": c["text"], "builtin": False, "edited": False})
    for it in items:
        it["pinned"] = it["id"] in pinned
    order = {sid: i for i, sid in enumerate(pinned)}
    items.sort(key=lambda it: (0, order[it["id"]]) if it["pinned"] else (1, 0))
    return [{**DEFAULT, "builtin": True, "edited": False, "pinned": False}, *items]


def get(style_id: str | None) -> dict | None:
    return next((s for s in all_styles() if s["id"] == (style_id or "default")), None)


def text_of(style_id: str | None) -> str | None:
    s = get(style_id)
    return (s or {}).get("text") or None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:24] or "style") + "-" + uuid.uuid4().hex[:4]


def save(style_id: str | None, name: str, text: str) -> dict:
    name, text = name.strip(), text.strip()
    if not name or not text:
        raise ValueError("名稱和內容都要填")
    if len(text) > 4000:
        raise ValueError("內容太長（上限 4000 字）")
    with _LOCK:
        d = _read()
        if style_id and any(b["id"] == style_id for b in BUILTIN):
            d.setdefault("edited", {})[style_id] = {"name": name, "text": text}
        elif style_id and any(c.get("id") == style_id for c in d.get("custom") or []):
            for c in d["custom"]:
                if c.get("id") == style_id:
                    c.update(name=name, text=text)
        elif style_id == "default":
            raise ValueError("「預設」不加任何段落，不能修改")
        else:
            style_id = _slug(name)
            d.setdefault("custom", []).append({"id": style_id, "name": name, "text": text})
        _write(d)
    return get(style_id)


def delete(style_id: str) -> None:
    """自訂的直接刪；內建的還原成原本內容。"""
    with _LOCK:
        d = _read()
        if any(b["id"] == style_id for b in BUILTIN):
            (d.get("edited") or {}).pop(style_id, None)
        else:
            d["custom"] = [c for c in d.get("custom") or [] if c.get("id") != style_id]
            d["pinned"] = [x for x in d.get("pinned") or [] if x != style_id]
        _write(d)


def pin(style_id: str, on: bool) -> None:
    if style_id == "default" or get(style_id) is None:
        raise ValueError("這個風格不能置頂")
    with _LOCK:
        d = _read()
        p = [x for x in d.get("pinned") or [] if x != style_id]
        if on:
            p.append(style_id)
        d["pinned"] = p
        _write(d)
