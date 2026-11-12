"""輸出風格：每個對話各自選一種，內容放進系統提示。

風格清單在 woof/styles.py（內建＋設定頁新增的，可置頂）。預設不加任何段落。
"""
from __future__ import annotations

from woof import store, styles


def build_text(session_id: str) -> str | None:
    st = store.get_session(session_id) or {}
    return styles.text_of((st.get("engine_state") or {}).get("style", "default"))


def register(woof):
    def build_prompt(ctx):
        sid = getattr(ctx, "session_id", None)
        return build_text(sid) if sid else None

    woof.on("build_prompt", build_prompt, order=30)
