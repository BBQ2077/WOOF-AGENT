"""正在跑的回合、方案流程、背景子代理記在磁碟上（data/live_runs.json）。

程式重開、當掉、被強制關閉後，開程式時照這份紀錄自動接回：背景子代理接著做完再回報，
方案流程從中斷的那一步接回，一般回合用接續訊息接著做。做完、你按停止、出錯時才從紀錄拿掉；
程式關閉途中被收掉的不算做完，留著下次接。同一個工作連續接回 MAX_RECOVER 次都沒有進度（做完一項 TODO 算進度），就不再自動接，留「繼續」給你按。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from ..config import DATA

log = logging.getLogger("woof.live")

MAX_RECOVER = 2
SHUTTING = False          # 程式正在關閉：這時被收掉的回合、流程、子代理不算結束，紀錄留著
_lock = threading.Lock()
_live: dict[str, dict] = {}
_loaded = False


def _file():
    return DATA / "live_runs.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        _live.update(json.loads(_file().read_text(encoding="utf-8")) or {})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("讀取執行紀錄失敗")


def _save() -> None:
    try:
        f = _file()
        if not _live:
            f.unlink(missing_ok=True)
            return
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(_live, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception:
        log.exception("寫入執行紀錄失敗")


def mark(session_id: str, key: str, value=True) -> None:
    """key：turn（一般回合）／flow（方案流程）／bg（背景子代理，value 是接回需要的參數）。"""
    if not session_id:
        return
    with _lock:
        _load()
        rec = _live.setdefault(session_id, {"at": time.time()})
        rec[key] = value
        _save()


def unmark(session_id: str, key: str) -> None:
    if SHUTTING or not session_id:
        return
    with _lock:
        _load()
        rec = _live.get(session_id)
        if rec is None or key not in rec:
            return
        rec.pop(key, None)
        if not any(k in rec for k in ("turn", "flow", "bg")):
            _live.pop(session_id, None)
        _save()


def progress(session_id: str) -> None:
    """有進度（做完一項 TODO、補完一輪）：接回次數歸零。長工作中間重開幾次都照樣接回，只有一直沒進度才停。"""
    with _lock:
        _load()
        rec = _live.get(session_id)
        if rec and rec.get("n"):
            rec["n"] = 0
            _save()


def take() -> dict[str, dict]:
    """開程式時拿出上次沒做完的（拿出來就清掉；接回時會重新記上，接回次數加一）。"""
    with _lock:
        _load()
        out = {k: dict(v) for k, v in _live.items()}
        _live.clear()
        _save()
        return out


def remark(session_id: str, rec: dict) -> None:
    """接回時把紀錄放回去（次數加一），接回途中又被關掉，下次還接得到。"""
    with _lock:
        _load()
        r = dict(rec)
        r["n"] = int(rec.get("n") or 0) + 1
        r["at"] = time.time()
        _live[session_id] = r
        _save()
