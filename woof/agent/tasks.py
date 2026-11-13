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
    answer = "（使用者沒有回答）"
    try:
        answer = await asyncio.wait_for(ASKS[aid]["future"], timeout)
        return answer
    except asyncio.TimeoutError:
        answer = "（使用者沒有回答）"
        return answer
    finally:
        # 卡片要顯示「已回答：使用者打的字」——答案跟「是不是略過」都要一起送回去，
        # 不然前端只知道這題結束了，會留一張空白的已回答卡在那裡
        a = ASKS.pop(aid, None) or {}
        await turn.emit({"type": "ask_done", "session_id": turn.session_id, "id": aid,
                         "answer": str(answer), "skipped": bool(a.get("skipped"))})


def skip_ask(session_id: str, answer: str = "（使用者直接回話，跳過這個問題）", skipped: bool = False) -> int:
    """把這個對話還在等的提問一次結掉。使用者打字送新訊息進來時用：那一則就是回答，
    不要讓回合卡在等一個沒人會點的卡片上（兩邊都等到 1800 秒逾時）。"""
    n = 0
    for a in list(ASKS.values()):
        if a.get("session_id") == session_id and not a["future"].done():
            a["skipped"] = skipped or "略過" in str(answer)
            a["future"].set_result(answer)
            n += 1
    return n


def has_ask(session_id: str) -> bool:
    """這個對話有沒有還在等的提問（含方案／目標流程裡的）。"""
    return any(a.get("session_id") == session_id and not a["future"].done() for a in ASKS.values())


def resolve_ask(aid: str, answer: str) -> bool:
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return False
    a["skipped"] = "略過" in str(answer)
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
    return steps[:5]   # 空陣列＝這則訊息不用動手（閒聊／問答），交給一般回合回覆


def _recent_report(session_id: str, limit: int = 8) -> str:
    """最後幾則助理文字與工具輸出，當對抗檢查員的證據。"""
    parts: list[str] = []
    for m in reversed(store.get_messages(session_id)):
        role = m.get("role")
        blocks = m.get("content") or []
        if role == "assistant":
            t = " ".join(b.get("text") or "" for b in blocks if b.get("type") == "text").strip()
            if t:
                parts.append("【助理】" + t[:500])
        elif role == "tool":
            outs = [str(b.get("output") or "").strip().replace("\n", " ")[:200]
                    for b in blocks if b.get("type") == "tool_result" and str(b.get("output") or "").strip()]
            if outs:
                parts.append("【工具輸出】" + " / ".join(outs))
        if len(parts) >= limit:
            break
    return "\n".join(reversed(parts)) or "（沒有紀錄）"


def resume_digest(session_id: str, tools: int = 8) -> str:
    """「繼續」時把已經做過的事交給模型，省掉它從頭逛一遍專案（list_tree、重讀每個檔）。"""
    s = store.get_session(session_id) or {}
    out: list[str] = []
    todos = s.get("todos") or []
    if todos:
        done = sum(1 for t in todos if t.get("status") == "done")
        left = [str(t.get("text") or "") for t in todos if t.get("status") != "done"]
        out.append(f"待辦 {done}/{len(todos)}" + ("；還沒完成：" + "；".join(left[:5]) if left else "（都做完了）"))
    kids = store.child_sessions(session_id) if hasattr(store, "child_sessions") else []
    if kids:
        names = "、".join(str(k.get("title") or k.get("id"))[:18] for k in kids[:4])
        out.append(f"之前派出的子代理 {len(kids)} 個（{names}）：程式重開過，它們不會再回報，要結果就重新派一次，"
                   "不要等一個不會來的報告")
    goal = ((s.get("engine_state") or {}).get("goal") or {})
    if goal.get("objective"):
        out.append("目標：" + str(goal["objective"])[:300])
    ask = last_text(session_id, "user")
    if ask and not ask.startswith("（接續"):
        out.append("這次要做的事：" + ask.strip()[:300])
    recent = _recent_report(session_id, tools)
    if recent and recent != "（沒有紀錄）":
        out.append("最近做過（最舊→最新）：\n" + recent[-2500:])
    paths = changed_paths(session_id)
    if paths:
        out.append("這個對話寫過／改過的檔案：" + "、".join(paths[-8:]))
    if not out:
        return ""
    return ("（接續用，不必重新探索）\n" + "\n".join(out)
            + "\n直接從沒做完的地方接著做；真的需要內容再讀那一個檔，不要從頭把資料夾逛一遍。")


def _parse_check(ans: str) -> tuple[bool, list[str], str]:
    found = re.findall(r"\{[\s\S]*?\}", ans or "")
    if not found:
        return False, [], "對抗檢查回覆格式不對：" + (ans or "")[-120:]
    try:
        j = json.loads(found[-1])
    except Exception:
        return False, [], "對抗檢查回覆解析失敗"
    gaps = [str(x).strip() for x in (j.get("gaps") or j.get("reason") or []) if str(x).strip()][:3]
    ok = bool(j.get("pass")) and not gaps
    return ok, ([] if ok else gaps), str(j.get("reason") or "")


async def final_check(ctx, session_id: str, objective: str, steps: list[str], emit) -> tuple[bool, list[str]]:
    """全部跑完只驗一次：目標真的做到了嗎。回（有沒有過, 缺什麼）。"""
    from woof.agent.loop import run_subagent

    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("review") + _prompt("review_body", objective=objective, plan=plan, report=_recent_report(session_id))
    r = await run_subagent(ctx, goal, "", "", None, "對抗檢查：",
                           title_hint=f"審核模型：{objective.strip()[:20]}", kind="review_request")
    ok, gaps, why = _parse_check(r.get("answer") or "")
    if not ok and not gaps and why:
        gaps = [why[:120]]
    return ok, gaps


async def _run_step(ctx, session_id: str, objective: str, step: str, idx: int, total: int, emit) -> bool:
    """做一步：不再每步派子代理審核（那是八股與卡住的來源），做完就往下。"""
    from woof.agent.loop import run_model_loop

    text = _prompt("goal_step", objective=objective, n=idx + 1, total=total, step=step)
    m = store.add_message(session_id, "user", [{"type": "text", "text": text}], meta=internal.meta("goal_step"))
    await emit({"type": "message", "session_id": session_id, "message": m})
    try:
        await run_model_loop(ctx)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await emit({"type": "notice", "session_id": session_id, "text": f"步驟 {idx + 1} 執行出錯：{e}"})
        return False


async def run_goal(session_id: str, objective: str, emit, steps: list[str] | None = None) -> None:
    """目標：拆步驟（能一步就一步）→ 照做 → 全部做完只做一次對抗檢查 → 有缺口補一輪。"""
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
        steps = [s for s in steps if str(s).strip()][:5]
        if not steps:
            from woof.agent.loop import run_model_loop

            await emit({"type": "notice", "session_id": session_id, "text": "這個目標不需要拆步驟，我直接回你"})
            await run_model_loop(ctx)
            _save(session_id, goal={"objective": objective, "status": "chat", "at": time.time()})
            return
        items = set_todos(session_id, [{"text": s, "status": "pending"} for s in steps])
        _save(session_id, goal={"objective": objective, "status": "running", "at": time.time()})
        await _push_todos(emit, session_id, items)
        ctx = TurnContext(store.get_session(session_id), model_cfg,
                          (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
        failed = 0
        for i, step in enumerate(steps):
            items[i]["status"] = "doing"
            _save(session_id, todos=items)
            await _push_todos(emit, session_id, items)
            ok = await _run_step(ctx, session_id, objective, step, i, len(steps), emit)
            items[i]["status"] = "done" if ok else "failed"
            if not ok:
                failed += 1
            _save(session_id, todos=items)
            await _push_todos(emit, session_id, items)

        # 最後只驗一次：真的做到了嗎；有缺口就補一輪，不再無限重試。
        # 走到這裡代表使用者按了方案／對抗、而且真的拆了步驟，就一定要驗一次；
        # 純問答在前面 steps 為空時就 return 了，不會走到這。
        ok, gaps = await final_check(ctx, session_id, objective, steps, emit)
        if not ok and gaps:
            await emit({"type": "notice", "session_id": session_id,
                        "text": "審核模型（子代理）看過，認為還缺：" + "；".join(gaps)})
            fixed = await _run_step(ctx, session_id, objective, "只補這些缺口：" + "；".join(gaps),
                                    len(steps), len(steps), emit)
            if fixed:
                ok, gaps = await final_check(ctx, session_id, objective, steps, emit)

        status = "done" if ok else ("partial" if failed < len(steps) else "error")
        _save(session_id, goal={"objective": objective, "status": status, "at": time.time()})
        if ok:
            msg = "做完了。審核模型（子代理）看過，沒找到沒做到的地方。"
        elif gaps:
            msg = "做完了，但審核模型（子代理）認為還有沒過的：" + "；".join(gaps)
        else:
            msg = "做完了。審核模型（子代理）沒回出可用的判斷，建議你自己看一眼結果。"
        await emit({"type": "notice", "session_id": session_id, "text": msg})
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
                           title_hint=f"審核模型：方案審核", kind="review_request")
    found = re.findall(r"\{[^{}]*\"pass\"[^{}]*\}", r.get("answer") or "")
    if not found:
        return False, "方案審核回覆格式不對"
    try:
        j = json.loads(found[-1])
    except Exception:
        return False, "方案審核回覆解析失敗"
    return bool(j.get("pass")), str(j.get("reason") or "")


async def expand_plan(parent, objective: str, steps: list[str]) -> tuple[list[str], str]:
    """「方案・拓展」：把 A 補成 A/B/C/D 一整套，並點名哪些該先查。回（步驟, 一句話說明）。"""
    from woof.agent.loop import run_subagent

    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("expand") + _prompt("expand_body", objective=objective, plan=plan)
    r = await run_subagent(parent, goal, "", "", None, "思維拓展：",
                           title_hint="拓展模型", kind="expand_request")
    ans = r.get("answer") or ""
    found = re.findall(r"\{[\s\S]*?\}", ans)
    for raw in reversed(found):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        new = [str(x).strip() for x in (j.get("steps") or []) if str(x).strip()]
        if new:
            return new[:5], str(j.get("notes") or "")[:200]
    return [], ""


async def run_gated(session_id: str, objective: str, emit, gate: str) -> None:
    """先提方案：規劃（1～5 步）→ 人工核准／模型審核／思維拓展 → 照步驟做 → 最後對抗檢查一次。

    gate：human（人工核准）、model（模型審核只擋漏步驟與不可逆損害）、expand（先補成完整的一套再做）。
    """
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
            steps = [s for s in steps if str(s).strip()][:5]
            if not steps:
                # 打招呼、閒聊、單純問答：不拆步驟、不出方案卡、不做對抗檢查，直接當一般對話回
                from woof.agent.loop import run_model_loop

                await run_model_loop(ctx)
                _save(session_id, goal={"objective": objective, "status": "chat", "at": time.time()})
                return
            if gate == "expand":
                # 思維拓展：這個模型容易「你說 A 就只做 A」，先把它補成完整的一套再動手
                exp, notes = await expand_plan(ctx, objective, steps)
                if exp:
                    steps = exp
                if notes:
                    await emit({"type": "notice", "session_id": session_id,
                                "text": "拓展模型（子代理）補的：" + notes})
            items = set_todos(session_id, [{"text": s, "status": "pending"} for s in steps])
            await _push_todos(emit, session_id, items)
            plan_txt = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            if len(steps) == 1:
                # 只有一步就沒有方案好審：直接做，不要多一層確認
                approved = True
                break
            if gate == "model":
                ok, reason = await review_plan(ctx, objective, steps)
                if ok:
                    approved = True
                    break
                await emit({"type": "notice", "session_id": session_id,
                            "text": f"審核模型（子代理）打回這版方案：{reason or '不完整'}；換一版重提"})
                feedback = reason or "方案不完整"
                continue
            ans = await ask(ctx, f"規劃模型提的方案（{len(steps)} 步），你核可才動手：\n{plan_txt}", ["照做", "換個做法", "取消"])
            if "照做" in ans or "核准" in ans or "略過" in ans:
                # 略過＝使用者不想審這題，讓流程照這版走
                approved = True
                break
            if "取消" in ans or "沒有回答" in ans:
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，沒有動手"})
                return
            feedback = "使用者要換一個做法"
        if not approved:
            ans = await ask(ctx, f"提了 {PLAN_ROUNDS} 版，審核（{gate_name}）都沒過，最後一次意見：{feedback}。要照最後這版做，還是取消？",
                            ["照做", "取消"])
            if "取消" in ans or "沒有回答" in ans:
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，沒有動手"})
                return
        await run_goal(session_id, objective, emit, steps=steps)
    except asyncio.CancelledError:
        _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
        raise
    except Exception as e:
        _save(session_id, goal={"objective": objective, "status": "error", "error": str(e)[:300]})
        await emit({"type": "notice", "session_id": session_id, "text": f"方案流程失敗：{e}"})


def is_running(session_id: str) -> bool:
    """這個對話有沒有目標／方案流程在跑（不含一般回合）。"""
    t = GOAL_TASKS.get(session_id)
    return bool(t and not t.done())


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
