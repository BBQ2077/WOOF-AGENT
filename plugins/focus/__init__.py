"""重點便條：只在上下文壓縮之後，把被摘要掉的狀態接回來一次。

為什麼只在那個時候：便條裡的每一項（待辦、權限、改過的檔、出錯）原本都還在對話裡，
每一步重貼只是多花 token，而且「這回合的要求：…」等於每一步重新開場，模型會從
「使用者到底要什麼」重想一遍（同一段歷史實測：尾端有便條 thinking 1641／1967 字，
沒有便條 503／170 字）。壓縮之後這些才真的從對話裡消失，那時補一張才有意義。
放最後面：系統提示詞和前面的對話是快取前綴，便條只放這次請求的尾巴，不存進對話。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

TPE = datetime.now().astimezone().tzinfo  # 這台電腦的時區
WRITE_TOOLS = ("write_file", "patch", "edit_file")


def _tok(text: str) -> int:
    cjk = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
    return cjk + (len(text) - cjk + 3) // 4


def _short(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build(session: dict, msgs: list[dict], step: int, max_steps: int, budget: int) -> tuple[str, str]:
    """回傳（便條文字, 狀態簽章）。簽章只用「狀態」算，不含步數，用來判斷要不要重貼。"""
    from woof import perms
    from woof.prompts import get as _prompt

    es = session.get("engine_state") or {}
    # 這一回合：最後一則使用者本人說的話之後
    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m["role"] == "user" and not (m.get("meta") or {}).get("internal"):
            start = i
            break
    turn = msgs[start:]

    files, errs, names = [], [], {}
    for m in turn:
        for b in m["content"]:
            if b.get("type") == "tool_call":
                names[b.get("id")] = b.get("name")
                a = b.get("args") or {}
                if b.get("name") in WRITE_TOOLS and a.get("path") and a["path"] not in files:
                    files.append(str(a["path"]))
            elif b.get("type") == "tool_result" and b.get("is_error"):
                errs.append(f"{b.get('name') or names.get(b.get('id')) or '工具'}：{_short(b.get('output'), 60)}")

    # 只放模型自己看不到的狀態。這回合的要求不放：那本來就在對話裡，
    # 每步重念一次會讓模型每步從「使用者要什麼」重新想；壓縮把使用者的話吃掉時
    # 由 context 插件的 compact_inflight 原文重述一次（plugins/context）。
    lines: list[str] = []  # 依重要性排，超過上限從後面砍
    todos = es.get("todos") or []
    if todos:
        done = sum(1 for t in todos if t.get("status") in ("done", "skipped"))
        doing = next((t["text"] for t in todos if t.get("status") == "doing"), "")
        nxt = next((t["text"] for t in todos if t.get("status") == "pending"), "")
        bits = [f"待辦 {done}/{len(todos)} 完成"]
        if doing:
            bits.append("進行中：" + _short(doing, 40))
        if nxt:
            bits.append("下一項：" + _short(nxt, 40))
        lines.append("；".join(bits))
    perm = session.get("perm") or "auto"
    if perm != "auto":
        told = es.get("told") or {}
        at = told.get("at")
        when = datetime.fromtimestamp(at, TPE).strftime("%H:%M") if at else ""
        lines.append(f"權限：{perms.MODES.get(perm, (perm,))[0]}" + (f"（{when} 設定，以設定變更 #{told.get('seq')} 為準）" if when else ""))
    if errs:
        lines.append("最近出錯：" + "；".join(errs[-2:]))
    if files:
        shown = files[-6:]
        lines.append("這回合改過的檔：" + "、".join(shown) + (f" 等 {len(files)} 個" if len(files) > len(shown) else ""))

    # 簽章：狀態＋壓縮次數（壓縮過之後待辦可能被摘要掉，要重貼一次）
    sig = hashlib.sha1(("\n".join(lines) + f"|c{int(es.get('compress_count') or 0)}").encode("utf-8")).hexdigest()[:12]

    when = datetime.now(TPE).strftime("%H:%M")
    # 不寫「第幾步／上限幾步」：模型看到步數會開始盤算剩幾步、重排計畫，反而在原地多想一輪
    while lines:
        body = _prompt("focus_note", when=when, lines="\n".join("- " + x for x in lines))
        if _tok(body) <= budget:
            return body, sig
        lines.pop()
    return "", sig


def register(woof):
    def _n(key: str, dflt: int) -> int:
        try:
            v = woof.settings.get(key)
            return max(0, int(dflt if v in (None, "") else v))
        except (TypeError, ValueError):
            return dflt

    def before_request(messages, ctx):
        from woof import store

        step = int(getattr(ctx, "step", 0) or 0)
        if step < _n("from_step", 2) or not messages or getattr(ctx, "mid_turn", False):
            return None
        session = store.get_session(ctx.session_id) or {}
        es = dict(session.get("engine_state") or {})
        cc = int(es.get("compress_count") or 0)
        # 便條唯一的工作：壓縮把待辦／權限／出錯／改過的檔摘要掉之後，把它們接回來一次。
        # 沒剛壓縮過就完全不貼——這些東西在對話裡本來就有，重貼只是多花 token，
        # 還會讓模型每一步從「使用者到底要什麼」重新想一遍。
        if cc == int(es.get("focus_c") or 0):
            return None
        text, sig = build(session, store.get_messages(ctx.session_id), step, int(getattr(ctx, "max_steps", 0) or 0),
                          max(60, _n("max_tokens", 300)))
        if not text:
            return None
        ctx.focus_note = text
        es["focus_c"] = cc
        es["focus_sig"] = sig
        es["focus_last"] = {"text": text, "step": step + 1}  # 上下文面板顯示「最近一次附上的便條」
        store.update_session(ctx.session_id, engine_state=es)
        last = dict(messages[-1])
        last["content"] = [*last["content"], {"type": "note", "text": text}]
        return [*messages[:-1], last]

    woof.on("before_request", before_request, order=90)
