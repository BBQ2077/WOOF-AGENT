"""模式預設組：一鍵套用「哪些插件可用」與「工具權限」。

內建幾種常見用途（一般聊天、寫作創作、帶子代理的統籌、只查不改），使用者也可以在設定頁自己存、改、刪。
自訂的與「新對話預設」都存在 data/presets.json：{"default": "id", "custom": [{"id", "name", "desc", "perm", "plugins"}]}

套用範圍有兩種：
  - 目前對話：權限（perm）下一則訊息就換，插件開關排到下次壓縮或新對話才換（見 agent/loop.py 的 apply_pending，
    中途改 plugins_off 會讓這個對話的系統提示詞重組、前綴快取失效）
  - 新對話的預設：存在 data/presets.json 的 default，開新對話時套用
"""
from __future__ import annotations

import json
import re
import threading
import uuid

from .config import DATA

PATH = DATA / "presets.json"
_LOCK = threading.Lock()

# plugins 是「這個模式要用的插件」，沒列到的在套用時會被關掉。權限用 perms.MODES 的代號。
BUILTIN = [
    {"id": "chat", "name": "一般聊天", "desc": "問問題、查資料、看圖。不給終端機和子代理，權限先問你再動手。",
     "perm": "ask", "plugins": ["files", "info", "history", "memory", "web_search", "vision", "tasks", "style", "focus", "context", "tool_search"]},
    {"id": "write", "name": "寫作創作", "desc": "寫檔、改檔直接做，跑指令和連網先問。留著網路搜尋和看圖，可以邊查邊寫。",
     "perm": "edits", "plugins": ["files", "info", "memory", "web_search", "vision", "tasks", "style", "focus", "context", "tool_search"]},
    {"id": "build", "name": "動手做", "desc": "讀寫檔、跑指令、開子代理都放行，適合把整個專案交給它做。",
     "perm": "auto", "plugins": ["files", "terminal", "subagent", "tasks", "info", "history", "memory", "web_search", "vision", "style", "focus", "context", "tool_search", "skills", "schedule"]},
    {"id": "orchestrate", "name": "統籌", "desc": "以子代理為主：自己不動手，把子任務派出去，再收集結果。",
     "perm": "edits", "plugins": ["files", "terminal", "subagent", "tasks", "info", "history", "memory", "web_search", "focus", "context", "tool_search", "schedule"]},
    {"id": "readonly", "name": "只查不改", "desc": "只能讀和搜尋，寫檔、跑指令一律擋下。看程式碼、查資料用。",
     "perm": "readonly", "plugins": ["files", "info", "history", "web_search", "vision", "memory", "tasks", "focus", "context", "tool_search"]},
]

DEFAULT_PERM = "ask"


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


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:24] or "preset") + "-" + uuid.uuid4().hex[:4]


def _perm(perm) -> str:
    from . import perms

    return perm if perm in perms.MODES else DEFAULT_PERM


def _clean(item: dict, plugins: set[str]) -> dict | None:
    """留著看得到的插件；都沒有就當這個模式沒東西可用（仍然准存）。"""
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    pid = str(item.get("id") or "").strip() or _slug(name)
    return {"id": pid, "name": name, "desc": str(item.get("desc") or "").strip(),
            "perm": _perm(item.get("perm")),
            "plugins": sorted({str(x) for x in item.get("plugins") or [] if str(x) in plugins})}


def all_presets() -> list[dict]:
    """內建的在前（固定順序），自訂的接著。每個都附 builtin 標記。"""
    live = _live_plugins()
    out = []
    for b in BUILTIN:
        out.append({**b, "plugins": [p for p in b["plugins"] if p in live], "builtin": True})
    for c in _read().get("custom") or []:
        if not isinstance(c, dict):
            continue
        item = _clean(c, live)
        if item is None:
            continue
        out.append({**item, "builtin": False})
    return out


def get(pid: str | None) -> dict | None:
    return next((p for p in all_presets() if p["id"] == (pid or "")), None)


def _live_plugins() -> set[str]:
    from .loader import REG

    return set(REG.plugins.keys())


def off_for(preset: dict, plugins: set[str] | None = None) -> list[str]:
    """這個模式要關掉哪些插件（沒列到的都關）。"""
    live = set(plugins) if plugins is not None else _live_plugins()
    keep = {p for p in preset.get("plugins") or [] if p in live}
    return sorted(live - keep)


def save(items: list[dict]) -> list[dict]:
    """整份存自訂的模式（內建的不能改）。回傳存下去的那份。"""
    live = _live_plugins()
    clean, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        item = _clean(it, live)
        if item is None:
            continue
        if item["id"] in seen or any(b["id"] == item["id"] for b in BUILTIN):
            item["id"] = _slug(item["name"])
        seen.add(item["id"])
        clean.append(item)
    with _LOCK:
        d = _read()
        d["custom"] = clean
        if not get_in(d, d.get("default")):
            d.pop("default", None)
        _write(d)
    return clean


def get_in(d: dict, pid) -> dict | None:
    """在指定的那 dict 裡找模式（內建＋這個 dict 的 custom），不碰檔案。"""
    live = _live_plugins()
    if pid and any(b["id"] == pid for b in BUILTIN):
        return next(dict(b, plugins=[p for p in b["plugins"] if p in live], builtin=True) for b in BUILTIN if b["id"] == pid)
    for c in d.get("custom") or []:
        if isinstance(c, dict) and c.get("id") == pid:
            item = _clean(c, live)
            return dict(item, builtin=False) if item else None
    return None


def delete(pid: str) -> bool:
    with _LOCK:
        d = _read()
        cur = [c for c in d.get("custom") or [] if isinstance(c, dict)]
        left = [c for c in cur if c.get("id") != pid]
        if len(left) == len(cur):
            return False
        d["custom"] = left
        if d.get("default") == pid:
            d.pop("default", None)
        _write(d)
    return True


def default_id() -> str:
    """新對話預設的模式 id（沒設或設的不存在回空字串）。"""
    want = str(_read().get("default") or "")
    return want if get(want) else ""


def for_new_session() -> dict | None:
    """新對話要套用的模式；沒設或設的不存在回 None。"""
    return get(default_id())


def apply_to_new(session: dict, preset: dict) -> dict:
    """開新對話時套用：權限直接設，插件開關直接設（還沒有訊息，沒有快取會斷）。"""
    from . import store

    es = dict(session.get("engine_state") or {})
    es["plugins_off"] = off_for(preset)
    es["preset"] = preset["id"]
    es.pop("pending_off", None)
    es.pop("pending_gen", None)
    return store.update_session(session["id"], perm=preset["perm"], engine_state=es) or session


def set_default(pid: str) -> str:
    """設成新對話的預設模式；空字串＝不指定。回傳實際存下的 id。"""
    if pid and get(pid) is None:
        raise ValueError("沒有這個模式")
    with _LOCK:
        d = _read()
        if pid:
            d["default"] = pid
        else:
            d.pop("default", None)
        _write(d)
    return pid