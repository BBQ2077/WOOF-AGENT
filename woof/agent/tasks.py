"""任務功能：提問（ask_user）、TODO（todo）、/goal 目標執行、子代理審核、預案模式。

- ask：模型或目標執行要問使用者時，發 ask 事件，等 POST /api/asks/{id} 回答。
- todo：寫進對話的 engine_state.todos，發 todos 事件，聊天區上方顯示清單。
- /goal：規劃員把目標拆成步驟；每步由主模型執行，宣告「完成：」後交給子代理審核，
  通過才標成完成；沒過就把理由回饋給主模型重做，最多 3 次，還是不過就問使用者。
- 審核提示詞刻意寫成「只擋明顯沒做的」，避免吹毛求疵。
"""
from __future__ import annotations

import asyncio

from woof.prompts import get as _prompt
from woof.agent import internal
import json
import re
import time
import uuid

from woof import store

TODO_STATUS = ("pending", "doing", "done", "skipped", "failed")
PLAN_TOOLS = {"write_file", "patch", "terminal", "process"}
ASKS: dict[str, dict] = {}
GOAL_TASKS: dict[str, asyncio.Task] = {}
PLAN_ROUNDS = 3  # 方案最多提幾版（模型審核打回、或使用者要求重提）



def _state(session_id: str) -> dict:
    return dict((store.get_session(session_id) or {}).get("engine_state") or {})


def _save(session_id: str, **kw) -> None:
    st = _state(session_id)
    st.update(kw)
    store.update_session(session_id, engine_state=st)


def last_text(session_id: str, role: str) -> str:
    msgs = store.get_messages(session_id)
    last = next((m for m in reversed(msgs) if m["role"] == role), None)
    if not last:
        return ""
    return "".join(b.get("text", "") for b in last.get("content", []) if b.get("type") == "text").strip()


async def ask(turn, question: str, options: list[str] | None = None, timeout: float = 1800) -> str:
    aid = uuid.uuid4().hex[:12]
    opts = [str(o) for o in (options or []) if str(o).strip()][:6]
    ASKS[aid] = {"future": asyncio.get_running_loop().create_future(), "session_id": turn.session_id}
    await turn.emit({"type": "ask", "session_id": turn.session_id, "id": aid, "question": question, "options": opts})
    try:
        return await asyncio.wait_for(ASKS[aid]["future"], timeout)
    except asyncio.TimeoutError:
        return "（使用者沒有回答）"
    finally:
        ASKS.pop(aid, None)
        await turn.emit({"type": "ask_done", "session_id": turn.session_id, "id": aid})


def resolve_ask(aid: str, answer: str) -> bool:
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return False
    a["future"].set_result(answer)
    return True


def set_todos(session_id: str, items: list[dict]) -> list[dict]:
    clean = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            continue
        st = str(it.get("status") or "pending")
        clean.append({"text": str(it["text"]).strip(), "status": st if st in TODO_STATUS else "pending"})
    _save(session_id, todos=clean)
    return clean


async def _push_todos(turn_emit, session_id: str, items: list[dict]) -> None:
    await turn_emit({"type": "todos", "session_id": session_id, "items": items})


async def plan_steps(model_cfg: dict, objective: str, feedback: str = "") -> list[str]:
    from woof.agent.loop import aux_model, complete

    use = aux_model("subagent", model_cfg) or model_cfg
    text = objective + (_prompt("plan_redo", feedback=feedback) if feedback else "")
    txt = await complete(use, _prompt("plan_goal"), [{"role": "user", "content": [{"type": "text", "text": text}]}])
    m = re.search(r"\[.*\]", txt or "", re.S)
    if not m:
        raise RuntimeError("規劃員沒有回傳步驟清單：" + (txt or "")[:120])
    steps = [str(s).strip() for s in json.loads(m.group(0)) if str(s).strip()]
    if not steps:
        raise RuntimeError("規劃員回傳的步驟是空的")
    return steps[:8]


async def review(parent, step: str, report: str) -> tuple[bool, str]:
    from woof.agent.loop import run_subagent

    goal = _prompt("review") + _prompt("review_body", step=step, report=report or "（沒有文字回覆）")
    r = await run_subagent(parent, goal, "", "", None, "審核：", title_hint=f"審核：{step.strip()[:20]}", kind="review_request")
    found = re.findall(r"\{[^{}]*\"pass\"[^{}]*\}", r.get("answer") or "")
    if not found:
        return False, "審核回覆格式不對：" + (r.get("answer") or "")[-120:]
    try:
        j = json.loads(found[-1])
    except Exception:
        return False, "審核回覆解析失敗"
    return bool(j.get("pass")), str(j.get("reason") or "")


async def _run_step(ctx, session_id: str, objective: str, step: str, idx: int, total: int,
                    emit) -> tuple[bool, str]:
    from woof.agent.loop import run_model_loop

    text = _prompt("goal_step", objective=objective, n=idx + 1, total=total, step=step)
    reason = ""
    for attempt in range(3):
        kind = "goal_step" if attempt == 0 else "review_feedback"
        m = store.add_message(session_id, "user", [{"type": "text", "text": text}], meta=internal.meta(kind))
        await emit({"type": "message", "session_id": session_id, "message": m})
        await run_model_loop(ctx)
        report = last_text(session_id, "assistant")
        ok, reason = await review(ctx, step, report)
        if ok:
            return True, ""
        await emit({"type": "notice", "session_id": session_id,
                    "text": f"步驟 {idx + 1} 審核未通過（第 {attempt + 1} 次）：{reason}"})
        text = _prompt("review_fail", reason=reason)
    return False, reason


async def run_goal(session_id: str, objective: str, emit, steps: list[str] | None = None) -> None:
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id)
    model_cfg = REG.model_cfg((session or {}).get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        await emit({"type": "notice", "session_id": session_id, "text": "目標需要 API 型模型，請換一個模型"})
        return
    _save(session_id, goal={"objective": objective, "status": "planning", "at": time.time()})
    try:
        if steps is None:
            steps = await plan_steps(model_cfg, objective)
        items = set_todos(session_id, [{"text": s, "status": "pending"} for s in steps])
        _save(session_id, goal={"objective": objective, "status": "running", "at": time.time()})
        await _push_todos(emit, session_id, items)
        ctx = TurnContext(store.get_session(session_id), model_cfg,
                          (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
        for i, step in enumerate(steps):
            items[i]["status"] = "doing"
            _save(session_id, todos=items)
            await _push_todos(emit, session_id, items)
            ok, reason = await _run_step(ctx, session_id, objective, step, i, len(steps), emit)
            if not ok:
                ans = await ask(ctx, f"步驟「{step}」審核 3 次都沒過（最後理由：{reason}）。怎麼處理？",
                                ["跳過這步", "停止目標"])
                if "停止" in ans:
                    items[i]["status"] = "failed"
                    _save(session_id, todos=items, goal={"objective": objective, "status": "stopped"})
                    await _push_todos(emit, session_id, items)
                    return
                items[i]["status"] = "skipped"
            else:
                items[i]["status"] = "done"
            _save(session_id, todos=items)
            await _push_todos(emit, session_id, items)
        _save(session_id, goal={"objective": objective, "status": "done", "at": time.time()})
        await emit({"type": "notice", "session_id": session_id, "text": "目標完成，每一步都已通過審核或已處理"})
    except asyncio.CancelledError:
        cur = _state(session_id)
        if cur.get("todos"):
            for t in cur["todos"]:
                if t.get("status") == "doing":
                    t["status"] = "pending"
        _save(session_id, todos=cur.get("todos") or [], goal={"objective": objective, "status": "stopped"})
        raise
    except Exception as e:
        _save(session_id, goal={"objective": objective, "status": "error", "error": str(e)[:300]})
        await emit({"type": "notice", "session_id": session_id, "text": f"目標執行失敗：{e}"})


def start_goal(session_id: str, objective: str, emit) -> None:
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        raise RuntimeError("這個對話已經有目標在執行，先用 /goal stop 停止")
    GOAL_TASKS[session_id] = asyncio.create_task(run_goal(session_id, objective, emit))


def stop_goal(session_id: str) -> bool:
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


async def review_plan(parent, objective: str, steps: list[str]) -> tuple[bool, str]:
    from woof.agent.loop import run_subagent

    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("plan_review") + _prompt("plan_review_body", objective=objective, plan=plan)
    r = await run_subagent(parent, goal, "", "", None, "方案審核：",
                           title_hint=f"方案審核：{objective.strip()[:20]}", kind="review_request")
    found = re.findall(r"\{[^{}]*\"pass\"[^{}]*\}", r.get("answer") or "")
    if not found:
        return False, "方案審核回覆格式不對"
    try:
        j = json.loads(found[-1])
    except Exception:
        return False, "方案審核回覆解析失敗"
    return bool(j.get("pass")), str(j.get("reason") or "")


async def run_gated(session_id: str, objective: str, emit, gate: str) -> None:
    """先提方案：規劃 → 審核方案（人工或模型）→ 核准後才照步驟執行（每步仍由子代理審核）。"""
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id)
    model_cfg = REG.model_cfg((session or {}).get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        await emit({"type": "notice", "session_id": session_id, "text": "方案模式需要 API 型模型，請換一個模型"})
        return
    ctx = TurnContext(session, model_cfg, (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
    gate_name = "人工審核" if gate == "human" else "模型審核"
    feedback, steps = "", None

    try:
        approved = False
        for rnd in range(1, PLAN_ROUNDS + 1):
            _save(session_id, goal={"objective": objective, "status": "planning", "at": time.time()})
            steps = await plan_steps(model_cfg, objective, feedback)
            items = set_todos(session_id, [{"text": s, "status": "pending"} for s in steps])
            await _push_todos(emit, session_id, items)
            plan_txt = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            if gate == "model":
                ok, reason = await review_plan(ctx, objective, steps)
                await emit({"type": "notice", "session_id": session_id,
                            "text": f"方案第 {rnd} 版：模型審核{'通過' if ok else '打回'}" + (f"（{reason}）" if reason else "")})
                if ok:
                    approved = True
                    break
                feedback = reason or "方案不完整"
                continue
            ans = await ask(ctx, f"方案第 {rnd} 版（{len(steps)} 步）：\n{plan_txt}\n\n要照這個方案執行嗎？",
                            ["核准執行", "重新提方案", "取消"])
            if "核准" in ans:
                approved = True
                break
            if "取消" in ans or "沒有回答" in ans:
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，方案沒有執行"})
                return
            feedback = "使用者要求重新提方案"
        if not approved:
            ans = await ask(ctx, f"方案已提出 {PLAN_ROUNDS} 版，{gate_name}都沒有通過（最後理由：{feedback}）。"
                                 "要照最後這版執行，還是取消？", ["照這版執行", "取消"])
            if "取消" in ans or "沒有回答" in ans:
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，方案沒有執行"})
                return
        await emit({"type": "notice", "session_id": session_id,
                    "text": f"方案已{gate_name}通過，開始照步驟執行"})
        await run_goal(session_id, objective, emit, steps=steps)
    except asyncio.CancelledError:
        _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
        raise
    except Exception as e:
        _save(session_id, goal={"objective": objective, "status": "error", "error": str(e)[:300]})
        await emit({"type": "notice", "session_id": session_id, "text": f"方案流程失敗：{e}"})


def start_gated(session_id: str, objective: str, emit, gate: str) -> None:
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        raise RuntimeError("這個對話已經有目標或方案在執行，先按停止")
    GOAL_TASKS[session_id] = asyncio.create_task(run_gated(session_id, objective, emit, gate))


_WRITE_OK = ("已寫入 ", "已修改 ")


def changed_paths(session_id: str) -> list[str]:
    """本對話寫入或修改成功的檔案路徑（從工具結果取，去重、保留順序）。"""
    import re as _re
    seen: list[str] = []
    for m in store.get_messages(session_id):
        for b in m.get("content", []) or []:
            if b.get("type") != "tool_result" or b.get("is_error"):
                continue
            out = str(b.get("output") or "")
            if not out.startswith(_WRITE_OK):
                continue
            mm = _re.match(r"已(?:寫入|修改) (.+?)(?:（|$)", out.split("\n")[0])
            if mm:
                p = mm.group(1).strip()
                if p and p not in seen:
                    seen.append(p)
    return seen


CHANGES_MAX = 12_000


async def review_changes(session_id: str, emit) -> dict:
    """審查「本對話改過的檔案目前內容」。沒有改動直接回報，不呼叫模型。"""
    from pathlib import Path
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id) or {}
    model_cfg = REG.model_cfg(session.get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        raise RuntimeError("審核需要 API 型模型")
    paths = changed_paths(session_id)
    if not paths:
        raise RuntimeError("這個對話還沒有寫入或修改過檔案，沒有改動可以審核")
    blocks = []
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            blocks.append(f"### {p}\n（檔案已不存在）")
            continue
        try:
            txt = fp.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            blocks.append(f"### {p}\n（讀取失敗：{e}）")
            continue
        if len(txt) > CHANGES_MAX:
            txt = txt[:CHANGES_MAX] + f"\n…（以下截斷，共 {len(txt)} 字元）"
        blocks.append(f"### {p}\n```\n{txt}\n```")
    report = "\n\n".join(blocks)
    request = last_text(session_id, "user") or "（沒有找到使用者的要求）"
    ctx = TurnContext(session, model_cfg, model_cfg.get("default_effort"), emit)
    ok, reason = await review(ctx, f"本對話改動的檔案（{len(paths)} 個）。使用者要求：{request[:600]}", report)
    text = f"【改動審核】{'通過' if ok else '未通過'}：{reason}\n\n審查範圍：" + "、".join(paths)
    m = store.add_message(session_id, "assistant", [{"type": "text", "text": text}])
    await emit({"type": "message", "session_id": session_id, "message": m})
    return {"ok": ok, "reason": reason, "paths": paths}


async def review_last(session_id: str, emit) -> dict:
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id) or {}
    model_cfg = REG.model_cfg(session.get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        raise RuntimeError("審核需要 API 型模型")
    report = last_text(session_id, "assistant")
    if not report:
        raise RuntimeError("這個對話還沒有助手的回覆可以審核")
    request = last_text(session_id, "user") or "（沒有找到使用者的要求）"
    ctx = TurnContext(session, model_cfg, model_cfg.get("default_effort"), emit)
    ok, reason = await review(ctx, f"上一則使用者要求：{request[:600]}", report)
    text = f"【子代理審核】{'通過' if ok else '未通過'}：{reason}"
    m = store.add_message(session_id, "assistant", [{"type": "text", "text": text}])
    await emit({"type": "message", "session_id": session_id, "message": m})
    return {"ok": ok, "reason": reason}
