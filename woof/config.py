"""路徑與設定載入。

設定分多組（profile）：「預設」就是專案本來的 config.toml、keys.toml 與 data 目錄；其他每組放在
data/profiles/<id>/，有自己的 config.toml、keys.toml 與 data（對話、記憶、排程都在裡面）。
後端啟動時用環境變數 WOOF_PROFILE 指定，切換設定組會重啟後端（資料路徑在啟動時就決定了）。
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import tomli_w

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
WEB_DIST = ROOT / "web" / "dist"

# 測試用暫存資料夾：一給設定，資料與設定檔就都放到那裡，不碰真正的 data\、config.toml、keys.toml
TEST_DATA = Path(os.environ["WOOF_DATA"]) if os.environ.get("WOOF_DATA") else None
BASE_DATA = TEST_DATA or ROOT / "data"   # profiles\ 與 profiles.json 放這裡（所有設定組共用）
BASE_CFG = TEST_DATA or ROOT             # 「預設」這組的設定檔
PROFILES_DIR = BASE_DATA / "profiles"
REGISTRY = BASE_DATA / "profiles.json"
PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

_want = (os.environ.get("WOOF_PROFILE") or "").strip()
PROFILE = _want if _want != "default" and PROFILE_ID.match(_want) and (PROFILES_DIR / _want).is_dir() else "default"
PROFILE_DIR = BASE_CFG if PROFILE == "default" else PROFILES_DIR / PROFILE
CONFIG_PATH = PROFILE_DIR / "config.toml"
KEYS_PATH = PROFILE_DIR / "keys.toml"
DATA = BASE_DATA if PROFILE == "default" else PROFILE_DIR / "data"

PROFILE_DIR.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

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
    cfg.update(_read_toml(CONFIG_PATH))
    return cfg


KEY_ERRORS: dict[str, str] = {}   # "池/標籤" → 解不開的原因（設定頁顯示「請重新貼 key」）


def load_keys() -> dict[str, list[dict]]:
    """keys.toml：每個服務一個陣列，元素含 key 與 label。

    key 用 Windows DPAPI 加密存（dpapi:…，woof/dpapi.py）；讀到還沒加密的明文 key 會自動改存成加密的。
    """
    from . import dpapi

    raw = _read_toml(KEYS_PATH)
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
    """整份寫回目前這組的 config.toml。呼叫端負責保留未改的欄位。"""
    path = CONFIG_PATH
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
    tmp = KEYS_PATH.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(KEYS_PATH)  # 先寫暫存檔再換掉，寫到一半當掉也不會弄壞原檔
