"""子代理。

delegate_task 會為每個子任務開一個子對話（側欄看得到，標記為子代理），
子代理用自己的上下文跑工具，跑完只把最後的報告交回主代理。
模型用「設定 → 模型用途 → 子代理」，沒設就跟主代理同一個。
主代理可以用 model 參數換模型，但只能挑使用者在插件設定「可指派的模型」勾的（防止模型自己挑貴的燒錢）；
每個使用者回合最多派幾個也有上限。子代理不能再派子代理（delegate_task 從子代理的工具裡拿掉）。
"""
from __future__ import annotations

import asyncio

# 寫入子代理同一時間只跑一個，避免互相覆蓋檔案
_WRITE_LOCK = asyncio.Lock()


def _rules(t: dict) -> str:
    if t.get("mode") != "write":
        return "【派工規範】唯讀：只能讀取與查詢，不可修改任何檔案或外部狀態，不可再派子代理。"
    scope = str(t.get("scope") or "").strip()
    return ("【派工規範】寫入：只能修改下列範圍內的檔案：" + (scope or "（主代理沒指定範圍，請先回報，不要改動任何檔案）")
            + "。範圍外一律不動，只做這一個子任務，不可再派子代理。")


def _scope_dirs(scope, cwd: str) -> list:
    """write 模式的 scope：多個路徑用 ; 或 , 分隔，相對路徑以主對話的工作目錄為準。"""
    import os
    from pathlib import Path

    out = []
    for raw in str(scope or "").replace(",", ";").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = Path(cwd) / p
        out.append(p.resolve())
    return out


def _kw(t: dict, parent) -> dict:
    if t.get("mode") == "write":
        return {"read_only": False, "write_scope": _scope_dirs(t.get("scope"), parent.cwd)}
    return {"read_only": True}


def _allowed(woof) -> list[str]:
    return [x.strip() for x in str(woof.settings.get("models") or "").split(",") if x.strip()]


def _pick_model(woof, t: dict) -> tuple[str, str]:
    """主代理指定的模型只能是使用者勾的「可指派的模型」；沒勾就一律用設定的子代理模型。回 (model_id, 錯誤)。"""
    want = str(t.get("model") or "").strip()
    if not want:
        return "", ""
    ok = _allowed(woof)
    if not ok:
        return "", ""  # 使用者沒開放：忽略主代理的選擇，用預設子代理模型（不報錯，免得多花一步）
    models = woof.config.get("models", [])
    hit = next((m for m in models if m["id"] in ok and want in (m["id"], m.get("model"), m.get("label"))), None)
    if hit is None:
        names = "、".join(f"{m.get('label', m['id'])}（{m['id']}）" for m in models if m["id"] in ok)
        return "", f"model「{want}」不在使用者開放的清單裡，只能用：{names}；或不填用預設"
    return hit["id"], ""


def register(woof):
    used: dict[str, int] = {}  # 每個使用者回合已派出的子代理數（擋住模型反覆派工燒錢）

    async def delegate_task(args: dict, tctx) -> dict:
        from woof.agent.loop import run_subagent

        parent = getattr(tctx, "turn", None)
        if parent is None:
            return {"output": "這個環境不能開子代理", "is_error": True}
        tasks = args.get("tasks") or []
        if args.get("goal"):
            tasks = [{"goal": args["goal"], "context": args.get("context", "")}] + list(tasks)
        tasks = [{"mode": args.get("mode"), "scope": args.get("scope"), "model": args.get("model"), **t} for t in tasks if isinstance(t, dict)]
        tasks = [t for t in tasks if str(t.get("goal") or "").strip()]
        if not tasks:
            return {"output": "至少要給一個 goal", "is_error": True}
        limit = max(1, int(woof.settings.get("max_parallel") or 3))
        if len(tasks) > limit:
            return {"output": f"一次最多 {limit} 個子任務，這次給了 {len(tasks)} 個", "is_error": True}
        for t in tasks:
            t["model"], err = _pick_model(woof, t)
            if err:
                return {"output": err, "is_error": True}
        cap = int(woof.settings.get("max_per_turn") or 0)
        key = f"{getattr(parent, 'root_session_id', parent.session_id)}:{getattr(parent, 'turn_mid', 0)}"
        if cap and used.get(key, 0) + len(tasks) > cap:
            return {"output": f"這一回合已經派了 {used.get(key, 0)} 個子代理，上限 {cap} 個（設定 → 插件 → subagent）。"
                              "剩下的自己做，或回報使用者", "is_error": True}
        used[key] = used.get(key, 0) + len(tasks)
        if len(used) > 200:
            for k in list(used)[:100]:
                used.pop(k, None)

        async def progress(line: str) -> None:
            await tctx.output(line + "\n")

        async def one(i: int, t: dict):
            label = f"[{i + 1}] " if len(tasks) > 1 else ""
            await progress(f"{label}開始：{str(t['goal']).strip()[:80]}")
            try:
                ctx_text = (_rules(t) + "\n" + str(t.get("context") or "")).strip()
                if t.get("mode") == "write":
                    await progress(f"{label}寫入模式：排隊等待（同一時間只跑一個寫入子代理）")
                    async with _WRITE_LOCK:
                        r = await run_subagent(parent, str(t["goal"]), ctx_text, str(t.get("model") or ""), progress, label, **_kw(t, parent))
                else:
                    r = await run_subagent(parent, str(t["goal"]), ctx_text, str(t.get("model") or ""), progress, label, **_kw(t, parent))
                await progress(f"{label}完成（{r['steps']} 次工具）")
                return r
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await progress(f"{label}失敗：{e}")
                return {"error": f"{type(e).__name__}: {e}", "goal": t["goal"]}

        results = await asyncio.gather(*[one(i, t) for i, t in enumerate(tasks)])
        parts, bad = [], 0
        for i, r in enumerate(results):
            head = f"## 子任務 {i + 1}" if len(results) > 1 else "## 子代理結果"
            if "error" in r:
                bad += 1
                parts.append(f"{head}（失敗）\n{r['error']}")
            else:
                facts = []
                if r.get("files_touched"):
                    facts.append("改過的檔：" + "、".join(r["files_touched"]))
                if r.get("commands_run"):
                    facts.append("跑過的指令：" + "；".join(r["commands_run"]))
                if r.get("errors"):
                    facts.append("出錯的工具：" + "；".join(r["errors"]))
                tail = ("\n\n［實際紀錄］\n" + "\n".join(facts)) if facts else ""
                parts.append(f"{head}（{r['model']}，{r['steps']} 次工具，對話 {r['session_id']}）\n{r['answer']}{tail}")
        return {"output": "\n\n".join(parts), "is_error": bad == len(results)}

    woof.add_tool(
        "delegate_task",
        "把獨立、可以分開做的子任務交給子代理。子代理有自己乾淨的上下文和全部工具，做完只回傳報告，"
        "適合：大量搜尋或讀檔、互不相關的平行工作、會塞爆上下文的調查。"
        "子代理看不到目前的對話，goal 和 context 要寫清楚完整需求、路徑和限制。",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "單一子任務：要達成什麼、完成標準"},
            "context": {"type": "string", "description": "子代理需要知道的背景：路徑、已知事實、限制"},
            "mode": {"type": "string", "enum": ["read", "write"],
                     "description": "read＝唯讀，可以多個並行；write＝會改檔案，同一時間只跑一個。預設 read"},
            "scope": {"type": "string", "description": "write 模式才需要：允許修改的路徑（例如 data/report/）。範圍外不准動"},
            "model": {"type": "string", "description": "選填。換別的模型做：只能用使用者在設定裡開放的模型（填錯會告訴你可用的）；"
                                                          "簡單、量大的工作挑便宜的。不填＝預設子代理模型"},
            "tasks": {"type": "array", "description": "多個子任務，每個可以是 {goal, context, mode, scope}",
                      "items": {"type": "object", "properties": {
                          "goal": {"type": "string"}, "context": {"type": "string"},
                          "mode": {"type": "string", "enum": ["read", "write"]}, "scope": {"type": "string"},
                          "model": {"type": "string"}},
                          "required": ["goal"]}},
        }},
        delegate_task,
    )
