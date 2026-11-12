"""工具延遲載入（tool search）。

工具一多（尤其接了 MCP 伺服器），每回合光是工具的參數說明就要送好幾千 token。
啟用時，可延遲的工具不放進送給模型的工具清單，換成三個橋接工具：
  tool_search(queries, limit?)   用關鍵字找工具（可一次多個查詢）
  tool_describe(names)           拿完整參數說明
  tool_call(calls)               執行，calls＝[{name, arguments}, ...]
tool_search 的說明裡附一份「名稱＋一句話」的清單，模型看到名稱就能直接 tool_describe。
tool_call 會拆開，每一筆照一般工具的流程執行（停用檢查、先問我、快照等掛鉤都照常）。

哪些可延遲：MCP 工具一律可以；內建工具只有設定裡列出的。
"""
from __future__ import annotations

import json
import re

from woof.tokens import estimate_text, estimate_tools

BRIDGE = ("tool_search", "tool_describe", "tool_call")
MAX_CALLS = 10


def _first_sentence(desc: str, n: int = 60) -> str:
    s = re.split(r"(?<=[。．.!！?？])\s*|\n", (desc or "").strip(), maxsplit=1)[0]
    return s if len(s) <= n else s[: n - 1] + "…"


def _source(tool) -> str:
    if tool is None:
        return "其他"
    if tool.plugin == "mcp_client":
        m = re.match(r"mcp_([^_]+)_", tool.name)
        return f"MCP {m.group(1)}" if m else "MCP"
    return tool.plugin


def _terms(q: str) -> list[str]:
    q = q.lower()
    words = re.findall(r"[a-z0-9]+", q)
    out = []
    for w in words:
        out.append(w)
        if len(w) > 4 and w.endswith("s"):
            out.append(w[:-1])  # issues → issue
    cjk = re.findall(r"[\u4e00-\u9fff]+", q)
    for run in cjk:
        out += [run[i:i + 2] for i in range(max(1, len(run) - 1))] if len(run) > 1 else [run]
    return out


def register(woof):
    from woof.agent import loop

    def _defer_names() -> set[str]:
        return {x.strip() for x in str(woof.settings.get("defer") or "").split(",") if x.strip()}

    def _deferrable(name: str) -> bool:
        tool = loop.REG.tools.get(name)
        if tool is None or name in BRIDGE:
            return False
        return tool.plugin == "mcp_client" or name in _defer_names()

    def _listing(specs: list[dict]) -> str:
        budget = max(200, int(woof.settings.get("listing_max") or 4000))
        groups: dict[str, list[dict]] = {}
        for t in specs:
            groups.setdefault(_source(loop.REG.tools.get(t["name"])), []).append(t)
        full = []
        for g in sorted(groups):
            full.append(f"{g}:")
            full += [f"  - {t['name']}: {_first_sentence(t['description'])}" for t in groups[g]]
        text = "\n".join(full)
        if estimate_text(text) <= budget:
            return text
        names = [f"{g}: " + ", ".join(t["name"] for t in groups[g]) for g in sorted(groups)]
        text = "\n".join(names)
        if estimate_text(text) <= budget:
            return text
        return "\n".join(f"{g}: {len(groups[g])} 個工具" for g in sorted(groups))

    def _bridge_specs(deferred: list[dict]) -> list[dict]:
        listing = _listing(deferred)
        return [
            {"name": "tool_search",
             "description": ("搜尋還沒載入的工具。下面清單裡的工具要用時，先 tool_describe 拿參數，再用 tool_call 執行；"
                             "不確定名稱就用 tool_search 以關鍵字找（queries 可一次放多個）。\n"
                             "可用但未載入的工具：\n" + listing),
             "schema": {"type": "object", "properties": {
                 "queries": {"type": "array", "items": {"type": "string"}, "description": "一個或多個查詢，每個是一種能力的關鍵字"},
                 "limit": {"type": "integer", "description": "每個查詢最多回傳幾個（預設 5）"}},
                 "required": ["queries"]}},
            {"name": "tool_describe",
             "description": "取得一個或多個未載入工具的完整參數說明。用 tool_call 之前先呼叫。",
             "schema": {"type": "object", "properties": {
                 "names": {"type": "array", "items": {"type": "string"}, "description": "工具名稱"}},
                 "required": ["names"]}},
            {"name": "tool_call",
             "description": "執行未載入的工具。calls 每一筆是 {name, arguments}；互不相關的呼叫可以一次放多筆（最多 10 筆）。",
             "schema": {"type": "object", "properties": {
                 "calls": {"type": "array", "items": {"type": "object", "properties": {
                     "name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name"]}}},
                 "required": ["calls"]}},
        ]

    def tool_specs(specs, ctx):
        mode = str(woof.settings.get("mode") or "自動")
        plain = [t for t in specs if t["name"] not in BRIDGE]
        if mode == "關閉":
            return plain
        deferred = [t for t in plain if _deferrable(t["name"])]
        if not deferred:
            return plain
        if mode == "自動" and estimate_tools(deferred) < float(woof.settings.get("threshold") or 1500):
            return plain
        keep = [t for t in plain if t not in deferred]
        return keep + _bridge_specs(deferred)

    def _pool(ctx) -> dict[str, dict]:
        """這個回合可以透過橋接取用的工具（已排除停用的）。"""
        allowed = loop.active_tool_specs(getattr(ctx, "tool_exclude", None))
        return {t["name"]: t for t in allowed if t["name"] not in BRIDGE}

    async def h_search(args, tctx):
        pool = _pool(tctx.turn)
        qs = args.get("queries") or []
        if isinstance(qs, str):
            qs = [qs]
        limit = max(1, min(int(args.get("limit") or 5), 25))
        results, tools = [], {}
        for q in qs[:10]:
            terms = _terms(str(q))
            scored = []
            for n, t in pool.items():
                hay = (n.replace("_", " ") + " " + (t["description"] or "")).lower()
                s = sum((3 if term in n.lower() else 0) + (1 if term in hay else 0) for term in terms)
                if s:
                    scored.append((s, n))
            scored.sort(key=lambda x: (-x[0], x[1]))
            hits = [n for _, n in scored[:limit]]
            row = {"query": q, "matches": hits}
            if not hits:
                row["available_sources"] = sorted({_source(loop.REG.tools.get(n)) for n in pool})
            results.append(row)
            for n in hits:
                req = (pool[n].get("schema") or {}).get("required") or []
                tools[n] = {"description": pool[n]["description"], "required": req}
        return {"output": json.dumps({"results": results, "tools": tools}, ensure_ascii=False, indent=1)}

    async def h_describe(args, tctx):
        pool = _pool(tctx.turn)
        names = args.get("names") or []
        if isinstance(names, str):
            names = [names]
        found = {n: {"description": pool[n]["description"], "parameters": pool[n].get("schema") or {}}
                 for n in names if n in pool}
        out = {"tools": found}
        miss = [n for n in names if n not in pool]
        if miss:
            out["not_found"] = miss
        return {"output": json.dumps(out, ensure_ascii=False, indent=1)}

    async def h_call(args, tctx):
        calls = args.get("calls") or []
        if isinstance(calls, dict):
            calls = [calls]
        if not calls:
            return {"output": "calls 是空的", "is_error": True}
        if len(calls) > MAX_CALLS:
            return {"output": f"一次最多 {MAX_CALLS} 筆，請分批", "is_error": True}
        ctx = tctx.turn
        outs = []
        for i, c in enumerate(calls):
            name = str((c or {}).get("name") or "")
            if name in BRIDGE:
                outs.append({"type": "tool_result", "name": name, "output": "不能用 tool_call 呼叫橋接工具", "is_error": True})
                continue
            sub = {"id": f"{tctx.call_id}-{i + 1}", "name": name, "args": (c or {}).get("arguments") or {}}
            outs.append(await loop._run_tool(sub, ctx))
        if len(outs) == 1:
            return {"output": outs[0]["output"], "is_error": outs[0]["is_error"]}
        return {"output": json.dumps([{"name": o["name"], "ok": not o["is_error"], "output": o["output"]} for o in outs],
                                     ensure_ascii=False, indent=1),
                "is_error": all(o["is_error"] for o in outs)}

    woof.on("tool_specs", tool_specs, order=90)
    # 橋接工具本身也要登記，執行時才找得到；不會出現在一般工具清單（tool_specs 掛鉤沒啟用時會被拿掉）
    for spec, fn in zip(_bridge_specs([]), (h_search, h_describe, h_call)):
        woof.add_tool(spec["name"], spec["description"], spec["schema"], fn)
        loop.REG.tools[spec["name"]].bridge = True
