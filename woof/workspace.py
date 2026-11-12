"""對話的工作目錄與檔案樹。

- 每個對話可以有自己的工作目錄（engine_state.workdir）；沒設就用設定裡的預設工作目錄。
- 檔案樹只列工作目錄底下的東西；依賴、建置、快取資料夾一律不列（跟常見 IDE 一樣），
  .gitignore 列到的預設不顯示，可以切換。
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .config import DATA

ALWAYS_EXCLUDED = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle", ".idea", ".next", ".nuxt",
    ".svelte-kit", ".turbo", ".parcel-cache", ".cache", ".terraform", ".expo", ".angular",
    "Thumbs.db", "desktop.ini", ".DS_Store",
}
MAX_ENTRIES = 3000


def default_workdir() -> str:
    from .loader import REG
    return REG.config.get("workdir") or str(Path.home())


def of_session(session: dict | None) -> str:
    """這個對話實際用的工作目錄：對話自己設的（資料夾還在）＞預設。"""
    es = (session or {}).get("engine_state") or {}
    wd = es.get("workdir") or ""
    if wd and Path(wd).is_dir():
        return wd
    return default_workdir()


def roots() -> list[Path]:
    """介面可以讀檔、列資料夾的範圍：允許清單、WOOF 資料、預設工作目錄、各對話的工作目錄。"""
    from . import store
    from .loader import REG

    out = [Path(p) for p in REG.config.get("allowed_dirs", []) if p] + [DATA, Path(default_workdir())]
    for s in store.list_sessions():
        wd = ((s.get("engine_state") or {}).get("workdir") or "")
        if wd:
            out.append(Path(wd))
    return out


def allowed(path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for r in roots():
        try:
            if rp.is_relative_to(r.resolve()):
                return True
        except OSError:
            continue
    return False


def _ignore_rules(d: Path, top: Path) -> list[tuple[Path, str]]:
    """從 top 到 d 每一層的 .gitignore（簡化版：支援 名稱、*.副檔名、資料夾/、/開頭錨定、! 不處理）。"""
    rules = []
    chain = [d, *d.parents]
    for p in reversed(chain):
        try:
            if not p.is_relative_to(top):
                continue
        except ValueError:
            continue
        gi = p / ".gitignore"
        if gi.is_file():
            try:
                for line in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith(("#", "!")):
                        rules.append((p, line))
            except OSError:
                pass
    return rules


def _ignored(entry: Path, is_dir: bool, rules: list[tuple[Path, str]]) -> bool:
    for base, pat in rules:
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if not pat or (dir_only and not is_dir):
            continue
        try:
            rel = entry.relative_to(base).as_posix()
        except ValueError:
            continue
        if pat.startswith("/"):
            if fnmatch.fnmatch(rel, pat[1:]):
                return True
        elif "/" in pat:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "*/" + pat):
                return True
        elif fnmatch.fnmatch(entry.name, pat):
            return True
    return False


def list_dir(path: str, top: str | None = None, show_ignored: bool = False) -> dict:
    d = Path(path)
    if not d.is_dir():
        raise FileNotFoundError(path)
    rules = [] if show_ignored else _ignore_rules(d, Path(top) if top else d)
    out, ignored = [], 0
    try:
        it = list(os.scandir(d))
    except PermissionError as e:
        raise PermissionError(f"沒有權限讀取：{path}") from e
    for e in it:
        if e.name in ALWAYS_EXCLUDED:
            continue
        try:
            is_dir = e.is_dir()
            st = e.stat()
        except OSError:
            continue
        p = Path(e.path)
        if rules and _ignored(p, is_dir, rules):
            ignored += 1
            continue
        out.append({"name": e.name, "path": str(p), "dir": is_dir, "size": 0 if is_dir else st.st_size,
                    "mtime": st.st_mtime, "hidden": e.name.startswith(".")})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": str(d), "entries": out[:MAX_ENTRIES], "truncated": len(out) > MAX_ENTRIES, "ignored": ignored}
