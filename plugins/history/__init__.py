"""對話檢索插件：session_search。

三種用法（看參數決定）：
  query                  在所有對話裡找關鍵字，回傳符合的對話、訊息編號和片段
  session_id (+ around)  讀某個對話（around＝訊息編號，只讀那則前後幾則）
  都不填                 列出最近的對話
資料來自 woof.db 的 msg_fts（trigram 全文索引，store.search_messages）。
"""
from __future__ import annotations

import time
from datetime import datetime


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


def _text(m: dict, cap: int) -> str:
    parts = []
    for b in m["content"]:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "tool_call":
            parts.append(f"[呼叫 {b.get('name')}]")
        elif t == "tool_result":
            out = (b.get("output") or "").strip()
            parts.append(f"[{b.get('name')} 結果] " + (out[:300] + "…" if len(out) > 300 else out))
        elif t == "image":
            parts.append("[圖片]")
    s = "\n".join(p for p in parts if p).strip()
    return s if len(s) <= cap else s[:cap] + f"…（後面還有 {len(s) - cap} 字）"


def _parse_after(v: str) -> float | None:
    v = (v or "").strip().lower()
    if not v:
        return None
    unit = {"h": 3600, "d": 86400, "w": 604800}.get(v[-1:])
    if unit and v[:-1].isdigit():
        return time.time() - int(v[:-1]) * unit
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def register(woof):
    from woof import store

    async def session_search(args: dict, tctx) -> dict:
        q = str(args.get("query") or "").strip()
        sid = str(args.get("session_id") or "").strip()
        limit = max(1, min(int(args.get("limit") or 5), 10))
        here = tctx.session_id
        if sid:
            s = store.get_session(sid)
            if not s:
                return {"output": f"沒有這個對話：{sid}", "is_error": True}
            msgs = [m for m in store.get_messages(sid) if not (m.get("meta") or {}).get("internal")]
            around = int(args.get("around") or 0)
            if around:
                idx = next((i for i, m in enumerate(msgs) if m["id"] >= around), len(msgs) - 1)
                msgs = msgs[max(0, idx - 4): idx + 5]
                cap = 1500
            else:
                cap = 600 if len(msgs) > 20 else 1500
                if len(msgs) > 40:
                    msgs = msgs[:10] + [None] + msgs[-30:]
            lines = [f"對話「{s['title']}」（{sid}，{_when(s.get('created'))} 開始，模型 {s.get('model')}）"]
            for m in msgs:
                if m is None:
                    lines.append("…（中間省略，用 around=訊息編號 讀特定段落）…")
                    continue
                body = _text(m, cap)
                if body:
                    lines.append(f"\n#{m['id']} {m['role']}（{_when(m.get('created'))}）\n{body}")
            return {"output": "\n".join(lines)}
        if not q:
            rows = [s for s in store.list_sessions() if not s.get("parent")][:limit * 2]
            lines = ["最近的對話："] + [f"- {s['id']}｜{s['title']}｜{_when(s.get('updated'))}｜{s.get('model')}"
                                       + ("｜目前這個對話" if s["id"] == here else "") + ("｜已封存" if s.get("archived") else "")
                                       for s in rows]
            return {"output": "\n".join(lines)}
        hits = store.search_messages(q, limit=80, exclude=here, after=_parse_after(str(args.get("after") or "")))
        if not hits:
            return {"output": f"過去的對話裡找不到「{q}」。這只查對話紀錄；使用者給了檔案、網址等來源就直接去查那個來源。"}
        groups: dict[str, list[dict]] = {}
        for h in hits:
            groups.setdefault(h["session_id"], []).append(h)
        lines = [f"「{q}」出現在 {len(groups)} 個對話（列出前 {min(limit, len(groups))} 個）。"
                 "要看完整內容：session_search(session_id=…, around=訊息編號)"]
        for gid, hs in list(groups.items())[:limit]:
            h0 = hs[0]
            lines.append(f"\n■ {h0['title']}（{gid}，{_when(h0['created'])}）")
            for h in hs[:3]:
                lines.append(f"  #{h['mid']} {h['role']}：{h['snippet']}")
        return {"output": "\n".join(lines)}

    woof.add_tool(
        "session_search",
        "搜尋或翻閱過去的對話紀錄（全文檢索）。使用者問「之前做過什麼」「上次停在哪」「某某是哪次講的」時使用。"
        "query＝找關鍵字（多個關鍵字用空白隔開，全部都要出現）；session_id＝讀那個對話，加 around＝訊息編號只讀那則前後；"
        "都不填＝列出最近的對話。搜尋不含目前這個對話。只查對話紀錄，查不到不代表事情不存在。",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "關鍵字，空白隔開"},
            "session_id": {"type": "string", "description": "要讀的對話 id"},
            "around": {"type": "integer", "description": "訊息編號（搜尋結果的 #數字），只讀那則前後幾則"},
            "after": {"type": "string", "description": "只找這之後的：7d、24h、2w 或 2026-11-01"},
            "limit": {"type": "integer", "description": "最多幾個對話，預設 5"},
        }},
        session_search,
    )
