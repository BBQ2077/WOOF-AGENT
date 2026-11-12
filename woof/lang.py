"""回覆語言跟著介面語言走（設定 → 外觀 → 介面語言，存在 data/ui-preferences.json 的 woof.lang）。

系統提示詞在對話第一次送出時固定；對話中途切換語言，改用「設定變更」附在下一則訊息（不重組系統提示詞，快取不斷）。
"""
from __future__ import annotations


def ui_lang() -> str:
    from . import ui_prefs
    try:
        v = ui_prefs.read()["values"].get("woof.lang")
    except Exception:  # noqa: BLE001
        v = None
    return "en" if v == "en" else "zh"


def reply_name(lang: str | None = None) -> str:
    return "English" if (lang or ui_lang()) == "en" else "繁體中文"
