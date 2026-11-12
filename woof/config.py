"""路徑與設定載入。"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import tomli_w

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PLUGINS = ROOT / "plugins"
WEB_DIST = ROOT / "web" / "dist"

DATA.mkdir(exist_ok=True)

HOME = Path(os.path.expanduser("~"))

DEFAULT_CONFIG: dict = {
    "workdir": str(HOME / "WOOF-files"),
    "allowed_dirs": [str(HOME / "WOOF-files")],
    "models": [],
}


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read_toml(ROOT / "config.toml"))
    return cfg


KEY_ERRORS: dict[str, str] = {}   # "池/標籤" → 解不開的原因（設定頁顯示「請重新貼 key」）


def load_keys() -> dict[str, list[dict]]:
    """keys.toml：每個服務一個陣列，元素含 key 與 label。

    key 用 Windows DPAPI 加密存（dpapi:…，woof/dpapi.py）；讀到還沒加密的明文 key 會自動改存成加密的。
    """
    from . import dpapi

    raw = _read_toml(ROOT / "keys.toml")
    out: dict[str, list[dict]] = {}
    plain = False
    KEY_ERRORS.clear()
    for name, entries in raw.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not (isinstance(e, dict) and e.get("key")):
                continue
            k = str(e["key"])
            if k.startswith(dpapi.PREFIX):
                try:
                    k = dpapi.decrypt(k)
                except Exception as ex:  # 換了電腦或 Windows 帳號
                    KEY_ERRORS[f"{name}/{e.get('label') or ''}"] = f"解不開（{ex}），請重新貼 key"
                    continue
            else:
                plain = True
            out.setdefault(name, []).append({**e, "key": k})
    if plain and dpapi.available():
        try:
            save_keys(out)
        except Exception:
            pass
    return out


def save_config(cfg: dict) -> None:
    """整份寫回 config.toml。呼叫端負責保留未改的欄位。"""
    path = ROOT / "config.toml"
    path.write_text(tomli_w.dumps(cfg), encoding="utf-8")


def _enc(key: str) -> str:
    from . import dpapi

    return key if key.startswith(dpapi.PREFIX) else dpapi.encrypt(key)


def save_keys(pools: dict[str, list[dict]]) -> None:
    lines = ["# WOOF 金鑰：用 Windows 帳號加密（DPAPI），只有這台電腦的這個帳號解得開。要改請到「設定 → API 金鑰」"]
    for name, entries in pools.items():
        for e in entries:
            if not e.get("key"):
                continue
            lines.append(f"[[{name}]]")
            lines.append("label = " + json.dumps(e.get("label") or "主要", ensure_ascii=False))
            lines.append("key = " + json.dumps(_enc(e["key"]), ensure_ascii=False))
            lines.append("")
    nl = chr(10)
    text = nl.join(lines).rstrip() + nl
    tmp = ROOT / "keys.toml.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(ROOT / "keys.toml")  # 先寫暫存檔再換掉，寫到一半當掉也不會弄壞原檔
