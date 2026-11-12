"""重點便條：多步工作時，每次送模型前在最後一則的最後面附一小段「目前狀態」。

為什麼放最後面：系統提示詞和前面的對話是快取前綴，動到就整段重算；便條只放在這次請求的尾巴，
不存進對話，下一步換到新的尾巴，前面的內容一個字都不變，快取照常命中。
內容全部由 WOOF 從對話狀態算出來（不花模型呼叫）：這回合的要求、步數、待辦進度、這回合改過的檔、最近出錯、權限。
"""
from __future__ import annotations

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


def build(session: dict, msgs: list[dict], step: int, max_steps: int, budget: int) -> str:
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
    req = " ".join(b.get("text") or "" for b in (turn[0]["content"] if turn else []) if b.get("type") == "text")

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

    lines: list[str] = []  # 依重要性排，超過上限從後面砍
    if req.strip():
        lines.append(f"這回合的要求：「{_short(req, 120)}」")
    lines.append(f"目前第 {step + 1} 步" + (f"（上限 {max_steps}）" if max_steps else ""))
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

    when = datetime.now(TPE).strftime("%H:%M")
    while lines:
        text = _prompt("focus_note", when=when, lines="\n".join("- " + x for x in lines))
        if _tok(text) <= budget:
            return text
        lines.pop()
    return ""


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
        text = build(session, store.get_messages(ctx.session_id), step, int(getattr(ctx, "max_steps", 0) or 0),
                     max(60, _n("max_tokens", 300)))
        if not text:
            return None
        ctx.focus_note = text
        es = dict((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
        es["focus_last"] = {"text": text, "step": step + 1}  # 上下文面板顯示「最近一次附上的便條」
        store.update_session(ctx.session_id, engine_state=es)
        last = dict(messages[-1])
        last["content"] = [*last["content"], {"type": "note", "text": text}]
        return [*messages[:-1], last]

    woof.on("before_request", before_request, order=90)
