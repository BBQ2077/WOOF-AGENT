"""上下文用量：以模型實際回報的數字為準，只把「之後新增的訊息」用估計補上。

各家 provider 回報的 input_tokens 一律是「這次請求的輸入總量」（已含快取讀取與快取寫入），
所以上下文用量＝input_tokens＋output_tokens，不再另外加 cache_read／cache_write。

錨點存在對話的 engine_state.usage：
  input_tokens / output_tokens / cache_read / cache_write   模型最後一次回報
  through        那次請求送出時，歷史裡最後一則訊息的 id
  context_used   input＋output（畫面上的上下文條直接用）
  estimated      True＝這是估計值（壓縮後、還沒拿到新回報前）
"""
from __future__ import annotations

import json


def estimate_text(text: str) -> int:
    """粗估 token：英文約 4 字元一個，中日韓字約 1.5 字元一個。"""
    if not text:
        return 0
    asc = sum(1 for ch in text if ord(ch) < 128)
    return round(asc / 4 + (len(text) - asc) / 1.5)


def estimate_blocks(content) -> int:
    n = 4  # 每則訊息的角色、分隔等固定開銷
    for b in content or []:
        t = b.get("type")
        if t in ("text", "thinking"):
            n += estimate_text(b.get("text") or "")
        elif t == "tool_call":
            n += estimate_text(b.get("name") or "") + estimate_text(json.dumps(b.get("args") or {}, ensure_ascii=False))
        elif t == "tool_result":
            n += estimate_text(b.get("output") or "")
        elif t == "image":
            n += 1500  # 各家圖片約 1–1.6k tokens
    return n


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_blocks(m.get("content")) for m in messages)


def estimate_tools(specs: list[dict]) -> int:
    return sum(estimate_text(json.dumps({"name": t["name"], "description": t["description"],
                                         "parameters": t.get("schema")}, ensure_ascii=False)) for t in specs)


def used_of(usage: dict | None) -> int:
    """模型回報的上下文用量。舊資料沒有 context_used 時用 input＋output。"""
    if not usage:
        return 0
    if usage.get("context_used") is not None:
        return int(usage.get("context_used") or 0)
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def reestimate(session_id: str, window: int | None = None) -> dict:
    """壓縮、清空、回溯之後，舊的回報不再準：改成整段估計（系統提示詞快照＋工具＋實際會送出的訊息），
    標成 estimated，下一次模型回報再換成實際數字。"""
    from woof import store
    from woof.agent.loop import active_tool_specs, history

    st = store.get_session(session_id) or {}
    es = dict(st.get("engine_state") or {})
    hist = history(session_id)
    summ = es.get("summary") or {}
    n = 0
    if summ.get("text"):
        through = int(summ.get("through") or 0)
        hist = [m for m in hist if int(m.get("id") or 0) > through]
        n += estimate_text(summ["text"]) + 30
    n += estimate_messages(hist)
    snap = store.get_prompt_snapshot(session_id)
    n += estimate_text(snap["text"]) if snap else 0
    n += estimate_tools([t for t in active_tool_specs() if not getattr(store_tools().get(t["name"]), "bridge", False)])
    old = es.get("usage") or {}
    usage = {"input_tokens": n, "output_tokens": 0, "cache_read": None, "cache_write": None,
             "context_window": window or old.get("context_window"), "context_used": n,
             "through": hist[-1]["id"] if hist else 0, "estimated": True}
    es["usage"] = usage
    store.update_session(session_id, engine_state=es)
    return usage


def window_of(model_cfg: dict | None) -> int:
    """這個對話算用量、判斷壓縮時用的上下文大小。
    壓縮設定填了「上下文大小」就用它（不超過模型本身的窗口）；沒填（0）就用模型的 context_window。"""
    from woof.loader import REG

    mw = int((model_cfg or {}).get("context_window") or 0)
    info = REG.plugins.get("context")
    try:
        cap = int((info.settings or {}).get("window") or 0) if info and info.enabled else 0
    except (TypeError, ValueError):
        cap = 0
    if cap > 0:
        return min(cap, mw) if mw else cap
    return mw


def store_tools() -> dict:
    from woof.loader import REG
    return REG.tools


def current(session_id: str) -> dict:
    """目前上下文大約多大：錨點（實際回報）＋錨點之後新增訊息的估計。
    回傳 {used, anchor, delta, estimated}；沒有錨點時 used=0、estimated=True（呼叫端自己決定要不要整段估計）。"""
    from woof import store

    st = store.get_session(session_id) or {}
    es = st.get("engine_state") or {}
    usage = es.get("usage") or {}
    anchor = used_of(usage)
    if not anchor:
        return {"used": 0, "anchor": 0, "delta": 0, "estimated": True}
    through = int(usage.get("through") or 0)
    newer = store.get_messages(session_id, after=through)
    delta = estimate_messages(newer)
    return {"used": anchor + delta, "anchor": anchor, "delta": delta, "estimated": bool(usage.get("estimated"))}
