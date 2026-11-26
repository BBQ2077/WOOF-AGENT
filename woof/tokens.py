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


def image_tokens() -> int:
    """一張圖約幾 token：Claude 每 28×28 像素 1 個；送出前已縮到每張上限以內（預設 1500），用上限和最長邊 16:9 的較小值估。"""
    import math

    try:
        from woof.agent.loop import img_side, img_tok_max

        s, cap = img_side(), img_tok_max()
    except Exception:
        s, cap = 1568, 1500
    return min(cap, math.ceil(s / 28) * math.ceil(s * 9 / 16 / 28))


def estimate_blocks(content) -> int:
    n = 4  # 每則訊息的角色、分隔等固定開銷
    for b in content or []:
        t = b.get("type")
        if t == "text":
            n += estimate_text(b.get("text") or "")
        elif t == "thinking":
            # Claude 回傳的思考文字只是摘要，完整思考加密在 signature 裡，送回去時照完整長度算輸入
            n += max(estimate_text(b.get("text") or ""), len(b.get("signature") or "") // 3)
        elif t == "redacted_thinking":
            n += len(b.get("data") or "") // 3
        elif t == "tool_call":
            n += estimate_text(b.get("name") or "") + estimate_text(json.dumps(b.get("args") or {}, ensure_ascii=False))
        elif t == "tool_result":
            n += estimate_text(b.get("output") or "") + image_tokens() * len(b.get("images") or [])
        elif t == "image":
            n += image_tokens()
    return n


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_blocks(m.get("content")) for m in messages)


def estimate_tools(specs: list[dict]) -> int:
    return sum(estimate_text(json.dumps({"name": t["name"], "description": t["description"],
                                         "parameters": t.get("schema")}, ensure_ascii=False)) for t in specs)


def pruned_result(b: dict) -> dict:
    """舊工具輸出換成一行佔位：保留工具名稱、成功或失敗、原本長度。"""
    from woof.prompts import get as _p

    out = b.get("output") or ""
    text = _p("tool_pruned", name=b.get("name") or "tool", status="失敗" if b.get("is_error") else "成功", chars=len(out))
    return {**{k: v for k, v in b.items() if k != "images"}, "output": text, "pruned": True}   # 舊圖一起拿掉


def apply_prune(messages: list[dict], es: dict | None) -> list[dict]:
    """engine_state.prune.through 之前（含）的工具結果換成佔位；沒設就原樣回傳。"""
    through = int(((es or {}).get("prune") or {}).get("through") or 0)
    if not through:
        return messages
    out = []
    for m in messages:
        if m.get("role") == "tool" and int(m.get("id") or 0) <= through:
            m = {**m, "content": [pruned_result(b) if b.get("type") == "tool_result" else b for b in m.get("content") or []]}
        out.append(m)
    return out


def used_of(usage: dict | None) -> int:
    """模型回報的上下文用量。舊資料沒有 context_used 時用 input＋output。"""
    if not usage:
        return 0
    if usage.get("context_used") is not None:
        return int(usage.get("context_used") or 0)
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _estimate_all(session_id: str, es: dict, upto: int = 0) -> int:
    """整段估計：系統提示詞快照＋工具＋實際會送出的訊息（摘要、附的原話、到 upto 為止的歷史；之後不送的思考不算）。"""
    from woof import store
    from woof.agent.loop import active_tool_specs, history

    hist = history(session_id)
    if upto:
        hist = [m for m in hist if int(m.get("id") or 0) <= upto]
    summ = es.get("summary") or {}
    n = 0
    if summ.get("text"):
        through = int(summ.get("through") or 0)
        hist = [m for m in hist if int(m.get("id") or 0) > through]
        n += estimate_text(summ["text"]) + estimate_text(summ.get("quotes") or "") + 300
    cut = int(es.get("think_cut") or 0)
    hist = [{**m, "content": [b for b in m.get("content") or [] if b.get("type") not in ("thinking", "redacted_thinking")]}
            if m.get("role") == "assistant" and int(m.get("id") or 0) <= cut else m for m in hist]
    n += estimate_messages(apply_prune(hist, es))
    snap = store.get_prompt_snapshot(session_id)
    n += estimate_text(snap["text"]) if snap else 0
    n += estimate_tools([t for t in active_tool_specs() if not getattr(store_tools().get(t["name"]), "bridge", False)])
    return n


def calib(session_id: str) -> float:
    """估計值跟這個模型實際算法差多少（實際 ÷ 估計，0.8 到 2.5）：拿模型上次實際回報的輸入量，跟同一段內容的估計比。
    中文在 Claude 常常差到 1.5 倍。沒有實際回報時用上次算出來的，再沒有就是 1。"""
    from woof import store

    es = (store.get_session(session_id) or {}).get("engine_state") or {}
    usage = es.get("usage") or {}
    real = int(usage.get("input_tokens") or 0)
    if usage.get("estimated") or not real:
        return float(es.get("tok_k") or 1.0)
    try:
        est = _estimate_all(session_id, es, int(usage.get("through") or 0))
    except Exception:
        return float(es.get("tok_k") or 1.0)
    return max(0.8, min(2.5, real / est)) if est else 1.0


def reestimate(session_id: str, window: int | None = None) -> dict:
    """壓縮、清空、回溯之後，舊的回報不再準：改成整段估計（照 calib 校正過），
    標成 estimated，下一次模型回報再換成實際數字。"""
    from woof import store
    from woof.agent.loop import history

    st = store.get_session(session_id) or {}
    es = dict(st.get("engine_state") or {})
    hist = history(session_id)
    n = int(_estimate_all(session_id, es) * float(es.get("tok_k") or 1.0))
    old = es.get("usage") or {}
    usage = {"input_tokens": n, "output_tokens": 0, "cache_read": None, "cache_write": None,
             "context_window": window or old.get("context_window"), "context_used": n,
             "through": hist[-1]["id"] if hist else 0, "estimated": True}
    es["usage"] = usage
    store.update_session(session_id, engine_state=es)
    return usage


def session_window(session_id: str | None) -> int:
    """這個對話自己設的上下文大小（上下文面板調的）；0＝照壓縮設定。"""
    if not session_id:
        return 0
    from woof import store

    try:
        return max(0, int(((store.get_session(session_id) or {}).get("engine_state") or {}).get("ctx_window") or 0))
    except (TypeError, ValueError):
        return 0


def window_of(model_cfg: dict | None, session_id: str | None = None) -> int:
    """這個對話算用量、判斷壓縮時用的上下文大小。
    這個對話在上下文面板設了上限就用它；否則壓縮設定填了「上下文大小」就用它；都沒填（0）就用模型的 context_window。
    都不超過模型本身的窗口。"""
    from woof.loader import REG

    mw = int((model_cfg or {}).get("context_window") or 0)
    info = REG.plugins.get("context")
    try:
        cap = int((info.settings or {}).get("window") or 0) if info and info.enabled else 0
    except (TypeError, ValueError):
        cap = 0
    if info and info.enabled:
        cap = session_window(session_id) or cap
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
    delta = int(estimate_messages(newer) * float(es.get("tok_k") or 1.0))
    return {"used": anchor + delta, "anchor": anchor, "delta": delta, "estimated": bool(usage.get("estimated"))}
