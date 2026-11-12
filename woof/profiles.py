"""多組設定（profile）：各自獨立的金鑰、模型、記憶、Skills、對話與排程。

每一組的東西完全分開，互不共用：
  預設（id = default）  config.toml、keys.toml、data/（就是專案本來的資料，不動它）
  其他（id = work…）    data/profiles/<id>/config.toml、keys.toml、data/

「目前這組」由環境變數 WOOF_PROFILE 決定，後端啟動時讀一次就定了，所以切換設定組要重啟後端
（改由 Electron 主程序接手：換掉環境變數、重啟 Python、重新載入視窗）。
清單存在 data/profiles.json（所有設定組共用）：{version, last, profiles: [{id, name, created}]}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

DEFAULT_ID = "default"
DEFAULT_NAME = "預設"
NAME_MAX = 20
_SAFE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    dir: Path
    data: Path
    is_default: bool = False

    @property
    def config(self) -> Path:
        return self.dir / "config.toml"

    @property
    def keys(self) -> Path:
        return self.dir / "keys.toml"

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "is_default": self.is_default,
                "path": self.dir.as_posix(), "data": self.data.as_posix()}


def _empty() -> dict:
    return {"version": 1, "last": DEFAULT_ID, "profiles": [{"id": DEFAULT_ID, "name": DEFAULT_NAME, "created": 0}]}


def load() -> dict:
    """讀清單；壞掉或缺檔就當成只有預設那一組（原資料仍在，不會不見）。"""
    try:
        doc = json.loads(config.REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    rows = [r for r in (doc.get("profiles") or []) if isinstance(r, dict) and _valid_id(str(r.get("id") or ""))]
    if not any(r["id"] == DEFAULT_ID for r in rows):
        rows.insert(0, {"id": DEFAULT_ID, "name": DEFAULT_NAME, "created": 0})
    names = {r["id"]: str(r.get("name") or "").strip() or DEFAULT_NAME for r in rows}
    return {"version": 1, "last": str(doc.get("last") or DEFAULT_ID),
            "profiles": [{"id": r["id"], "name": names[r["id"]], "created": int(r.get("created") or 0)} for r in rows]}


def save(doc: dict) -> None:
    config.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, config.REGISTRY)


def _valid_id(pid: str) -> bool:
    return pid == DEFAULT_ID or bool(config.PROFILE_ID.match(pid))


def check_name(name: str) -> str:
    """整理名稱並檢查；丟出 ValueError 時訊息直接給使用者看。"""
    n = " ".join(str(name or "").split())
    if not n:
        raise ValueError("請填名稱")
    if len(n) > NAME_MAX:
        raise ValueError(f"名稱最多 {NAME_MAX} 個字")
    if any(c in n for c in '\\/:*?"<>|'):
        raise ValueError("名稱不能含 \\ / : * ? \" < > |")
    return n


def _used_names(doc: dict, skip: str = "") -> set[str]:
    return {r["name"] for r in doc["profiles"] if r["id"] != skip}


def _dir_of(pid: str) -> Path:
    return config.BASE_CFG if pid == DEFAULT_ID else config.PROFILES_DIR / pid


def get(pid: str) -> Profile:
    doc = load()
    row = next((r for r in doc["profiles"] if r["id"] == pid), None)
    if row is None:
        raise ValueError("找不到這組設定")
    return Profile(id=row["id"], name=row["name"], dir=_dir_of(row["id"]),
                   data=config.BASE_DATA if row["id"] == DEFAULT_ID else _dir_of(row["id"]) / "data",
                   is_default=row["id"] == DEFAULT_ID)


def current() -> Profile:
    return get(config.PROFILE)


def all_profiles() -> list[Profile]:
    return [get(r["id"]) for r in load()["profiles"]]


def last_used() -> str:
    return load()["last"]


def set_last(pid: str) -> None:
    doc = load()
    if not any(r["id"] == pid for r in doc["profiles"]):
        raise ValueError("找不到這組設定")
    doc["last"] = pid
    save(doc)


def _new_id(name: str, doc: dict) -> str:
    base = _SAFE.sub("-", name).strip("-").lower() or "profile"
    base = base[:24].strip("-") or "profile"
    used = {r["id"] for r in doc["profiles"]}
    if base not in used:
        return base
    for i in range(2, 100):
        if f"{base}-{i}" not in used:
            return f"{base}-{i}"
    return f"{base}-{int(time.time() * 1000)}"


def create(name: str) -> Profile:
    """新增一組：目錄、空的 config.toml 與 keys.toml 都建好，之後各組完全獨立。"""
    name = check_name(name)
    doc = load()
    if name in _used_names(doc):
        raise ValueError("已經有同名的設定組")
    pid = _new_id(name, doc)
    p = Profile(id=pid, name=name, dir=config.PROFILES_DIR / pid, data=config.PROFILES_DIR / pid / "data")
    if p.dir.exists():
        raise ValueError("這組設定已經存在")
    (p.data / "prompts").mkdir(parents=True, exist_ok=True)
    p.config.write_text(_starter_config(), encoding="utf-8")
    p.keys.write_text("# WOOF 金鑰：用 Windows 帳號加密（DPAPI），只有這台電腦的這個帳號解得開。要改請到「設定 → API 金鑰」\n",
                      encoding="utf-8")
    doc["profiles"].append({"id": pid, "name": name, "created": int(time.time())})
    doc["last"] = pid
    save(doc)
    return p


def _starter_config() -> str:
    from .config import DEFAULT_CONFIG

    import tomli_w

    return tomli_w.dumps({"workdir": DEFAULT_CONFIG["workdir"],
                          "allowed_dirs": list(DEFAULT_CONFIG["allowed_dirs"]), "models": []})


def rename(pid: str, name: str) -> Profile:
    name = check_name(name)
    if pid == DEFAULT_ID:
        raise ValueError("預設那組的名稱不能改")
    doc = load()
    if not any(r["id"] == pid for r in doc["profiles"]):
        raise ValueError("找不到這組設定")
    if name in _used_names(doc, skip=pid):
        raise ValueError("已經有同名的設定組")
    for r in doc["profiles"]:
        if r["id"] == pid:
            r["name"] = name
    save(doc)
    return get(pid)


def remove(pid: str) -> None:
    """刪除一整組（config.toml、keys.toml、data 一起）。預設那組不能刪。"""
    if pid == DEFAULT_ID:
        raise ValueError("預設那組不能刪除")
    doc = load()
    if not any(r["id"] == pid for r in doc["profiles"]):
        raise ValueError("找不到這組設定")
    d = _dir_of(pid)
    if d.exists():
        shutil.rmtree(d)
    doc["profiles"] = [r for r in doc["profiles"] if r["id"] != pid]
    if doc["last"] == pid:
        doc["last"] = DEFAULT_ID
    save(doc)