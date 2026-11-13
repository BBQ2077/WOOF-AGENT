"""使用者貼進來的東西：圖片（data/uploads）與貼上的長文（data/pastes）。

兩者都屬於「這個對話的輸入」，所以對話刪掉時連帶清掉，開機也會回收沒人引用的孤兒檔。
模型自己寫出去的檔案在專案資料夾，不歸這裡管，永遠不會被動到。

檔名是亂數（uuid 前 12 碼），所以只能靠「訊息裡有沒有提到」來判斷誰在用。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from .config import DATA

# 只有這兩個目錄會跟對話一起刪：使用者貼進來的圖，和使用者貼上的長文
# （模型自己寫的檔案在專案資料夾，永遠不碰）
DIRS = {"uploads": DATA / "uploads", "pastes": DATA / "pastes", "files": DATA / "files"}
DIR = DIRS["uploads"]
_NAME = re.compile(r"(uploads|pastes|files)[\\/]{1,4}([A-Za-z0-9_-]+\.[A-Za-z0-9]{1,9})")


def names_in(text: str) -> set[str]:
    """訊息裡提到的貼圖／貼文檔名，格式是 "uploads/xxx.png"、"pastes/yyy.txt"。"""
    return {f"{m.group(1)}/{m.group(2)}" for m in _NAME.finditer(text or "")}


def save_paste(text: str) -> Path:
    """貼上的長文存成檔案（跟貼圖同一條生命週期）。"""
    d = DIRS["pastes"]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid.uuid4().hex[:12]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _resolve(key: str) -> Path | None:
    d, _, name = key.partition("/")
    p = DIRS.get(d, DIR) / name
    return p if p.is_file() else None


def files_of(session_id: str) -> list[Path]:
    """這個對話用到的圖（刪對話前先問，訊息刪掉之後就查不到了）。"""
    from . import store

    out: list[Path] = []
    for key in store.upload_names(session_id):
        p = _resolve(key)
        if p:
            out.append(p)
    return out


def remove(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def gc() -> tuple[int, int]:
    """刪掉沒有任何訊息引用的檔，回 (檔案數, bytes)。
    只掃 DIRS 這兩個資料夾；專案裡的產出一律不碰。"""
    from . import store

    import time

    used = store.all_upload_names()
    n = size = 0
    fresh = time.time() - 24 * 3600   # 一天內的不收：剛貼還沒送出、或工具剛存還沒寫進訊息的
    for label, d in DIRS.items():
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or f"{label}/{p.name}" in used:
                continue
            try:
                st = p.stat()
                if st.st_mtime > fresh:
                    continue
                size += st.st_size
                p.unlink()
                n += 1
            except OSError:
                pass
    return n, size
