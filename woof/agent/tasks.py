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
PLAN_ROUNDS = 3  # 方案最多提幾版（模型審核打回、或使用者要求重提）；設定 [flow] plan_rounds 可改
FLOW_DEFAULTS = {"plan_rounds": PLAN_ROUNDS, "fix_rounds": 1, "review_timeout": 300, "goal_gate": True}
FLOW_LIMITS = {"plan_rounds": (1, 10), "fix_rounds": (0, 5), "review_timeout": (0, 3600)}


def flow_settings() -> dict:
    """方案模式的上限（設定 → 方案模式，存在 config.toml [flow]）：
    plan_rounds 方案最多提幾版；fix_rounds 做完驗收後最多補幾輪；review_timeout 每次審核／拓展最多等幾秒（0＝不限）；
    goal_gate /goal 是否照對話選的方案模式走。"""
    from woof.config import load_config

    raw = (load_config().get("flow") or {})
    out = dict(FLOW_DEFAULTS)
    for k, (lo, hi) in FLOW_LIMITS.items():
        try:
            out[k] = max(lo, min(hi, int(raw.get(k, out[k]))))
        except (TypeError, ValueError):
            pass
    if "goal_gate" in raw:
        out["goal_gate"] = bool(raw["goal_gate"])
    return out


async def _sub(ctx, goal: str, label: str, title: str, kind: str) -> dict | None:
    """跑審核／拓展子代理，超過 review_timeout 就放棄（回 None），流程照目前這版往下走，不會卡死。"""
    from woof.agent.loop import run_subagent

    lim = flow_settings()["review_timeout"]
    coro = run_subagent(ctx, goal, "", "", None, label, title_hint=title, kind=kind)
    if not lim:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=lim)
    except asyncio.TimeoutError:
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": f"{title.split('：')[0]}（子代理）超過 {lim} 秒沒結論，這次不等了，照目前這版繼續"})
        return None



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


NO_REPLY = "（使用者沒有回應"


def ask_timeout() -> int:
    """問使用者時等幾秒，0＝一直等。設定在 config.toml 的 [permissions] ask_timeout，預設 180。"""
    from woof import perms

    return perms.settings()["ask_timeout"]


def no_reply_text(waited: float) -> str:
    """等不到回答時交給模型的話：講清楚是人不在，不是使用者說「隨便」。"""
    w = int(waited)
    span = f"{w // 60} 分 {w % 60} 秒" if w >= 60 and w % 60 else f"{w // 60} 分鐘" if w >= 60 else f"{w} 秒"
    return (f"{NO_REPLY}：等了 {span}都沒回，人可能不在電腦前。這題已自動略過，不要再問同一題。"
            "請自己判斷最合理的做法繼續（例如選你推薦的那個選項）；刪檔、覆蓋、對外發送這類收不回來的事先不要做，"
            "留到最後跟使用者說明，等他回來再決定。）")


def timed_out(answer: str) -> bool:
    return str(answer).startswith(NO_REPLY)


async def ask(turn, question: str, options: list[str] | None = None, timeout: float | None = None) -> str:
    aid = uuid.uuid4().hex[:12]
    opts = [str(o) for o in (options or []) if str(o).strip()][:6]
    limit = ask_timeout() if timeout is None else max(0, int(timeout))
    t0 = time.time()
    fut = asyncio.get_running_loop().create_future()
    ASKS[aid] = {"future": fut, "session_id": turn.session_id, "limit": limit,
                 "deadline": t0 + limit if limit else 0}
    await turn.emit({"type": "ask", "session_id": turn.session_id, "id": aid, "question": question, "options": opts,
                     "timeout": limit, "deadline": ASKS[aid]["deadline"]})
    answer = ""
    try:
        while True:
            dl = ASKS[aid]["deadline"]
            left = (dl - time.time()) if dl else None
            if left is not None and left <= 0:
                answer = no_reply_text(time.time() - t0)
                ASKS[aid]["timed_out"] = True
                return answer
            try:
                # 使用者在卡片上打字會把期限往後延（touch_ask），所以分段等、每段重看一次期限
                answer = await asyncio.wait_for(asyncio.shield(fut), min(left, 30) if left is not None else None)
                return answer
            except asyncio.TimeoutError:
                continue
    finally:
        # 卡片要顯示「已回答：使用者打的字」——答案跟「是不是略過」都要一起送回去，
        # 不然前端只知道這題結束了，會留一張空白的已回答卡在那裡
        a = ASKS.pop(aid, None) or {}
        if not fut.done():
            fut.cancel()
        await turn.emit({"type": "ask_done", "session_id": turn.session_id, "id": aid,
                         "answer": str(answer), "skipped": bool(a.get("skipped")),
                         "timed_out": bool(a.get("timed_out")), "waited": int(time.time() - t0)})


def touch_ask(aid: str) -> float:
    """使用者正在卡片上打字：期限從現在重新算，免得打到一半被略過。回傳新的期限（0＝不限）。"""
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return -1
    if a.get("limit"):
        a["deadline"] = time.time() + a["limit"]
    return a["deadline"]


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
    steps = [s for s in (_step_text(x) for x in json.loads(m.group(0))) if s]
    return steps[:5]   # 空陣列＝這則訊息不用動手（閒聊／問答），交給一般回合回覆


STEP_CHARS = 400


def _step_text(x) -> str:
    """規劃／拓展回的一步整理成一句話。有的模型會回物件、甚至把成品（整份 SVG、程式碼）塞進步驟裡：
    物件取它的說明欄位，成品內容丟掉，太長截斷——不然後面的模型會把它當成「已經有的檔案」。"""
    if isinstance(x, dict):
        x = next((x[k] for k in ("task", "step", "do", "text", "action", "title", "description")
                  if isinstance(x.get(k), str) and x[k].strip()), "")
    elif isinstance(x, (list, tuple)):
        x = "；".join(str(i) for i in x if isinstance(i, (str, int, float)))
    s = str(x or "").strip()
    s = re.split(r"```|<svg\b|<html\b|<!DOCTYPE|<\?xml", s, maxsplit=1, flags=re.I)[0].strip()
    return s[:STEP_CHARS] + ("…" if len(s) > STEP_CHARS else "")


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
    # 「這次要做的事」取最後一則不是接續訊息的使用者訊息；拿接續訊息當任務會一層套一層
    last = next((m for m in reversed(store.get_messages(session_id))
                 if m["role"] == "user" and (m.get("meta") or {}).get("internal") != "resume"), None)
    ask = "".join(b.get("text", "") for b in (last or {}).get("content", []) if b.get("type") == "text").strip()
    if ask:
        out.append("這次要做的事：" + ask[:300])
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


def _jobj(text: str, key: str) -> dict | None:
    """從子代理回覆抓出含 key 的 JSON 物件。先照正規 JSON 解析（前後夾雜文字、字串裡有大括號都可以）；
    模型常在字串裡放沒跳脫的引號（例：start "" "C:/x.html"），整段解析失敗時逐欄位寬鬆抓。"""
    s = text or ""
    dec = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r"\{", s)]):
        try:
            j, _ = dec.raw_decode(s, i)
        except ValueError:
            continue
        if isinstance(j, dict) and key in j:
            return j
    if f'"{key}"' not in s:
        return None
    out: dict = {}
    m = re.search(r'"pass"\s*:\s*(true|false)', s)
    if m:
        out["pass"] = m.group(1) == "true"
    for k in ("steps", "gaps"):
        m = re.search(rf'"{k}"\s*:\s*\[\s*(.*?)\s*\]\s*(?:,\s*"|\}})', s, re.S)
        if m:
            body = m.group(1).strip()
            out[k] = ([x.replace('\\"', '"') for x in re.split(r'"\s*,\s*"', body[1:-1])]
                      if len(body) >= 2 and body[0] == '"' and body[-1] == '"' else [])
    for k in ("notes", "reason"):
        m = re.search(rf'"{k}"\s*:\s*"(.*?)"\s*(?:,\s*"[a-z_]+"\s*:|\}})', s, re.S)
        if m:
            out[k] = m.group(1).replace('\\"', '"')
    return out if key in out else None


def _parse_check(ans: str) -> tuple[bool, list[str], str]:
    j = _jobj(ans, "pass")
    if j is None:
        return False, [], "對抗檢查回覆格式不對：" + (ans or "")[-120:]
    raw = j.get("gaps") or []
    if isinstance(raw, str):
        raw = [raw]
    gaps = [str(x).strip() for x in raw if str(x).strip()][:3]
    ok = bool(j.get("pass")) and not gaps
    return ok, ([] if ok else gaps), str(j.get("reason") or "")


async def review(ctx, request: str, report: str) -> tuple[bool, str]:
    """手動審核（審核上一則回覆、審核改動）：交給審核模型看證據，回（有沒有過, 理由）。"""
    goal = _prompt("review") + _prompt("review_body", objective=request, plan="（手動審核，沒有分步驟）",
                                       report=report or "（沒有文字回覆）")
    r = await _sub(ctx, goal, "審核：", f"審核模型：{request.strip()[:20]}", "review_request")
    if r is None:
        return False, "審核模型逾時，沒有結論"
    ok, gaps, why = _parse_check(r.get("answer") or "")
    return ok, "；".join(gaps) or why


async def final_check(ctx, session_id: str, objective: str, steps: list[str], emit) -> tuple[bool, list[str]]:
    """全部跑完驗一次：目標真的做到了嗎。回（有沒有過, 缺什麼）。"""
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("review") + _prompt("review_body", objective=objective, plan=plan, report=_recent_report(session_id))
    r = await _sub(ctx, goal, "對抗檢查：", f"審核模型：{objective.strip()[:20]}", "review_request")
    if r is None:
        return False, []
    ok, gaps, why = _parse_check(r.get("answer") or "")
    if not ok and not gaps and why:
        gaps = [why[:120]]
    return ok, gaps


async def _run_step(ctx, session_id: str, objective: str, step: str, idx: int, total: int, emit,
                    plan: list[str] | None = None, notes: str = "") -> bool:
    """做一步：不再每步派子代理審核（那是八股與卡住的來源），做完就往下。
    notes＝拓展模型的提醒（哪些該先查、哪些是配套），每一步都附上，不然只在畫面上閃一下、模型根本沒看到。"""
    from woof.agent.loop import run_model_loop

    plan_txt = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan or [step]))
    if notes:
        plan_txt += "\n【拓展模型的提醒】" + notes + "\n（提醒裡點名要查的，輪到相關的步驟就先用 web_search／web_extract 真的查，不要靠猜或反覆試）"
    text = _prompt("goal_step", objective=objective, n=idx + 1, total=total, step=step, plan=plan_txt)
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


async def _run_fix(ctx, session_id: str, gaps: list[str], emit) -> bool:
    """驗收後補缺口：用「補做沒做到的部分」提示詞（設定 → 方案模式可改），不是再塞一個「步驟 9/8」。"""
    from woof.agent.loop import run_model_loop

    text = _prompt("review_fail", reason="；".join(gaps))
    m = store.add_message(session_id, "user", [{"type": "text", "text": text}], meta=internal.meta("review_feedback"))
    await emit({"type": "message", "session_id": session_id, "message": m})
    try:
        await run_model_loop(ctx)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await emit({"type": "notice", "session_id": session_id, "text": f"補缺口時出錯：{e}"})
        return False


STEPS_MAX = 8   # 拓展補成的一整套最多幾步（規劃本身只拆 1～5 步）


def _goal_save(session_id: str, objective: str, status: str, steps: list[str] | None = None, idx: int = 0,
               new: bool = False, notes: str | None = None) -> None:
    """方案的步驟與做到第幾步存在 goal 裡（不放 todos：模型自己呼叫 todo 會蓋掉），程式重開後「繼續」才接得回來。
    since＝流程開始時最後一則訊息；之後你自己下了新指令，就不再接回這個方案。"""
    g = {"objective": objective, "status": status, "at": time.time()}
    if steps:
        old = _state(session_id).get("goal") or {}
        g.update(steps=steps, idx=idx, since=(None if new else old.get("since")) or _last_mid(session_id),
                 notes=notes if notes is not None else old.get("notes") or "")
    _save(session_id, goal=g)


def _last_mid(session_id: str) -> int:
    msgs = store.get_messages(session_id)
    return int(msgs[-1]["id"]) if msgs else 0


def resumable_goal(session_id: str) -> dict | None:
    """有沒有做到一半的方案可以接：有步驟、狀態是執行中／被停，而且之後你沒有另外下新的指令。"""
    g = _state(session_id).get("goal") or {}
    planning = g.get("status") == "planning" and g.get("gate") and g.get("since")   # 規劃／拓展／審核途中被打斷
    if not planning and (not g.get("steps") or g.get("status") not in ("running", "stopped")):
        return None
    since = int(g.get("since") or 0)
    for m in store.get_messages(session_id, after=since):
        if m["role"] == "user" and not (m.get("meta") or {}).get("internal"):
            return None
    return g


def resume_goal(session_id: str, emit, reason: str) -> bool:
    """「繼續」：方案做到一半被打斷（程式重開、停止、出錯）就接回流程——先把中斷的那步做完，再一步步往下，最後照樣驗收。"""
    g = resumable_goal(session_id)
    t = GOAL_TASKS.get(session_id)
    if not g or (t and not t.done()):
        return False
    from woof.agent.loop import clear_interrupted

    s2 = clear_interrupted(session_id)
    if s2:
        asyncio.get_running_loop().create_task(emit({"type": "session_updated", "session": s2}))
    if not g.get("steps"):
        # 還沒排出方案就被打斷：照原本的模式從規劃重來（拓展、審核都會再跑一次）
        GOAL_TASKS[session_id] = _track(session_id, run_gated(session_id, g["objective"], emit, g["gate"],
                                                              auto=bool(g.get("auto"))), emit)
        return True
    GOAL_TASKS[session_id] = _track(session_id, run_goal(session_id, g["objective"], emit, steps=list(g["steps"]),
                                                         start=int(g.get("idx") or 0), resume_reason=reason,
                                                         notes=str(g.get("notes") or "")), emit)
    return True


async def _resume_step(ctx, session_id: str, reason: str, emit) -> bool:
    """接續被打斷的那一步：用一般的「中斷後接續」訊息（含已做過的摘要），不重送步驟。"""
    from woof.agent.loop import run_model_loop
    from woof.prompts import get as _p

    text = _p("resume", reason=reason)
    digest = resume_digest(session_id)
    if digest:
        text = text.rstrip() + "\n\n" + digest
    m = store.add_message(session_id, "user", [{"type": "text", "text": text}], meta=internal.meta("resume"))
    await emit({"type": "message", "session_id": session_id, "message": m})
    try:
        await run_model_loop(ctx)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await emit({"type": "notice", "session_id": session_id, "text": f"接續出錯：{e}"})
        return False


async def run_goal(session_id: str, objective: str, emit, steps: list[str] | None = None,
                   start: int = 0, resume_reason: str = "", notes: str = "") -> None:
    """目標：拆步驟（能一步就一步）→ 照做 → 全部做完只做一次對抗檢查 → 有缺口補一輪。
    start／resume_reason：程式重開或停止後「繼續」，從第 start 步接回（那一步先用接續訊息做完）。"""
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id)
    model_cfg = REG.model_cfg((session or {}).get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        await emit({"type": "notice", "session_id": session_id, "text": "目標需要 API 型模型，請換一個模型"})
        return
    if not resume_reason:
        _save(session_id, goal={"objective": objective, "status": "planning", "at": time.time()})
    ctx = TurnContext(store.get_session(session_id), model_cfg,
                      (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
    try:
        if steps is None:
            steps = await plan_steps(model_cfg, objective)
        steps = [s for s in (_step_text(x) for x in steps) if s][:STEPS_MAX]
        if not steps:
            from woof.agent.loop import run_model_loop

            await emit({"type": "notice", "session_id": session_id, "text": "這個目標不需要拆步驟，我直接回你"})
            await run_model_loop(ctx)
            _save(session_id, goal={"objective": objective, "status": "chat", "at": time.time()})
            return
        start = max(0, min(start, len(steps)))
        items = set_todos(session_id, [{"text": s, "status": "done" if i < start else "pending"} for i, s in enumerate(steps)])
        _goal_save(session_id, objective, "running", steps, start, new=not resume_reason, notes=notes)
        await _push_todos(emit, session_id, items)
        if start:
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"接回方案：從第 {start + 1}/{len(steps)} 步繼續" if start < len(steps) else "接回方案：步驟都做完了，接著驗收"})
        failed = 0
        for i, step in enumerate(steps):
            if i < start:
                continue
            items[i]["status"] = "doing"
            _save(session_id, todos=items)
            _goal_save(session_id, objective, "running", steps, i)
            await _push_todos(emit, session_id, items)
            if i == start and resume_reason:
                ok = await _resume_step(ctx, session_id, resume_reason, emit)
            else:
                ok = await _run_step(ctx, session_id, objective, step, i, len(steps), emit, plan=steps, notes=notes)
            items[i]["status"] = "done" if ok else "failed"
            if not ok:
                failed += 1
            _save(session_id, todos=items)
            _goal_save(session_id, objective, "running", steps, i + 1)
            await _push_todos(emit, session_id, items)

        # 做完驗一次：真的做到了嗎；有缺口就補，最多補 fix_rounds 輪（設定 → 方案模式），不會無限打回。
        # 純問答在前面 steps 為空時就 return 了，不會走到這。
        fix_max = flow_settings()["fix_rounds"]
        ok, gaps = await final_check(ctx, session_id, objective, steps, emit)
        for rnd in range(1, fix_max + 1):
            if ok or not gaps:
                break
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"審核模型（子代理）看過，認為還缺（補第 {rnd}/{fix_max} 輪）：" + "；".join(gaps)})
            fixed = await _run_fix(ctx, session_id, gaps, emit)
            if not fixed:
                break
            ok, gaps = await final_check(ctx, session_id, objective, steps, emit)

        status = "done" if ok else ("partial" if failed < len(steps) else "error")
        _save(session_id, goal={"objective": objective, "status": status, "at": time.time()})
        if ok:
            msg = "做完了。審核模型（子代理）看過，沒找到沒做到的地方。"
        elif gaps:
            msg = (f"做完了，補了 {fix_max} 輪已到上限，審核模型（子代理）認為還有沒過的：" if fix_max
                   else "做完了（設定不補做），審核模型（子代理）認為還有沒過的：") + "；".join(gaps)
        else:
            msg = "做完了。審核模型（子代理）沒回出可用的判斷，建議你自己看一眼結果。"
        await emit({"type": "notice", "session_id": session_id, "text": msg})
    except asyncio.CancelledError:
        cur = _state(session_id)
        if cur.get("todos"):
            for t in cur["todos"]:
                if t.get("status") == "doing":
                    t["status"] = "pending"
        g = dict(cur.get("goal") or {})
        g.update(objective=objective, status="stopped")   # 步驟與進度留著：按「繼續」從停下的那步接回
        _save(session_id, todos=cur.get("todos") or [], goal=g)
        raise
    except Exception as e:
        _save(session_id, goal={"objective": objective, "status": "error", "error": str(e)[:300]})
        await emit({"type": "notice", "session_id": session_id, "text": f"目標執行失敗：{e}"})


def _track(session_id: str, coro, emit) -> asyncio.Task:
    """跑目標／方案流程。結束（做完、出錯、被停）時補送一個事件：
    流程裡最後一個事件（回覆的 run_end）送出時流程還沒結束，帶的 busy 是 True；
    不補這一下，前端會一直顯示「還在跑」。"""
    task = asyncio.create_task(coro)
    # 開頭就送一下：拓展、審核在子對話跑的那段，主對話自己沒有事件，前端拿不到「流程進行中」會以為卡住
    # （這時任務已登記在 GOAL_TASKS，送出時 broadcast 算出的 flow 是 True）
    asyncio.get_running_loop().create_task(emit({"type": "flow_start", "session_id": session_id}))

    def _done(_t):
        try:
            asyncio.get_running_loop().create_task(emit({"type": "flow_end", "session_id": session_id}))
        except RuntimeError:
            pass

    task.add_done_callback(_done)
    return task


def start_goal(session_id: str, objective: str, emit) -> None:
    """/goal：對話選了方案模式（人工／對抗／拓展）且設定 goal_gate 開著，就先走那個模式再照做；
    目標是放著讓它自己跑的，審核打回到上限或你沒回應時照最後一版做，不會停在那邊等。"""
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        raise RuntimeError("這個對話已經有目標在執行，先用 /goal stop 停止")
    gate = ((store.get_session(session_id) or {}).get("engine_state") or {}).get("gate") or "off"
    if gate in ("human", "model", "expand") and flow_settings()["goal_gate"]:
        coro = run_gated(session_id, objective, emit, gate, auto=True)
    else:
        coro = run_goal(session_id, objective, emit)
    GOAL_TASKS[session_id] = _track(session_id, coro, emit)


def stop_goal(session_id: str) -> bool:
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


async def review_plan(parent, objective: str, steps: list[str]) -> tuple[bool, str]:
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("plan_review") + _prompt("plan_review_body", objective=objective, plan=plan)
    r = await _sub(parent, goal, "方案審核：", "審核模型：方案審核", "review_request")
    if r is None:
        return True, "審核超時，照這版做"
    j = _jobj(r.get("answer") or "", "pass")
    if j is None:
        return False, "方案審核回覆格式不對"
    return bool(j.get("pass")), str(j.get("reason") or "")


async def expand_plan(parent, objective: str, steps: list[str], feedback: str = "") -> tuple[list[str], str]:
    """「方案・拓展」：把 A 補成 A/B/C/D 一整套，並點名哪些該先查。回（步驟, 一句話說明）。
    feedback＝審核模型打回上一版拓展的意見，這次照著修。"""
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("expand") + _prompt("expand_body", objective=objective, plan=plan)
    if feedback:
        goal += f"\n\n【審核模型打回上一版拓展的意見】{feedback}\n照這個意見修正，仍然只輸出 JSON。"
    r = await _sub(parent, goal, "思維拓展：", "拓展模型：思維拓展", "expand_request")
    if r is None:
        return [], ""
    j = _jobj(r.get("answer") or "", "steps")
    new = [s for s in (_step_text(x) for x in ((j or {}).get("steps") or [])) if s]
    if new:
        if len(new) > STEPS_MAX:   # 多出來的併進最後一步，不要直接丟掉（之前排的「出預覽圖、寫說明」就這樣被截掉）
            new = new[:STEPS_MAX - 1] + ["；".join(new[STEPS_MAX - 1:])]
        return new, str((j or {}).get("notes") or "")[:600]
    if parent is not None:
        await parent.emit({"type": "notice", "session_id": parent.session_id,
                           "text": "拓展模型（子代理）的回覆讀不出步驟，照原本的方案做"})
    return [], ""


async def run_gated(session_id: str, objective: str, emit, gate: str, auto: bool = False) -> None:
    """先提方案：規劃（1～5 步）→ 人工核准／模型審核／思維拓展 → 照步驟做 → 最後對抗檢查。

    gate：human（人工核准）、model（模型審核只擋漏步驟與不可逆損害）、expand（先補成完整的一套再做）。
    方案最多提 plan_rounds 版、做完最多補 fix_rounds 輪（設定 → 方案模式）。
    auto（/goal）：打回到上限、或你沒回應時照最後一版做，不停下來等。
    """
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id)
    model_cfg = REG.model_cfg((session or {}).get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        await emit({"type": "notice", "session_id": session_id, "text": "方案模式需要 API 型模型，請換一個模型"})
        return
    ctx = TurnContext(session, model_cfg, (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
    gate_name = {"human": "人工審核", "expand": "思維拓展"}.get(gate, "模型審核")
    feedback, steps, notes = "", None, ""
    rounds = flow_settings()["plan_rounds"]

    try:
        approved, base = False, None
        since = _last_mid(session_id)
        for rnd in range(1, rounds + 1):
            # gate／since 存著：規劃、拓展、審核途中被打斷（程式重開、停止），「繼續」會從規劃重來，不會變成沒有審核的一般接續
            _save(session_id, goal={"objective": objective, "status": "planning", "at": time.time(),
                                    "gate": gate, "auto": auto, "since": since})
            if gate != "expand" or base is None:
                # 拓展模式只規劃一次；被打回時拿原本的步驟照審核意見重新拓展
                base = await plan_steps(model_cfg, objective, "" if gate == "expand" else feedback)
                base = [s for s in (_step_text(x) for x in base) if s][:5]
            steps = base
            if not steps:
                # 打招呼、閒聊、單純問答：不拆步驟、不出方案卡、不做對抗檢查，直接當一般對話回
                from woof.agent.loop import run_model_loop

                await run_model_loop(ctx)
                _save(session_id, goal={"objective": objective, "status": "chat", "at": time.time()})
                return
            if gate == "expand":
                # 思維拓展：這個模型容易「你說 A 就只做 A」，先把它補成完整的一套，再交給審核模型看過才動手
                exp, notes = await expand_plan(ctx, objective, base, feedback)
                if exp:
                    steps = exp
                if notes:
                    await emit({"type": "notice", "session_id": session_id,
                                "text": "拓展模型（子代理）補的：" + notes})
            items = set_todos(session_id, [{"text": s, "status": "pending"} for s in steps])
            await _push_todos(emit, session_id, items)
            plan_txt = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            if gate in ("model", "expand"):
                # 對抗、拓展都要審：一步的方案也審（漏步驟、憑空假設的前提，一步一樣會出事）
                ok, reason = await review_plan(ctx, objective, steps)
                if ok:
                    await emit({"type": "notice", "session_id": session_id,
                                "text": f"審核模型（子代理）看過第 {rnd} 版方案（{len(steps)} 步）：通過"
                                        + (f"。{reason}" if reason else "")})
                    approved = True
                    break
                await emit({"type": "notice", "session_id": session_id,
                            "text": f"審核模型（子代理）打回第 {rnd}/{rounds} 版方案：{reason or '不完整'}"
                                    + (("；照意見重新拓展" if gate == "expand" else "；換一版重提") if rnd < rounds else "")})
                feedback = reason or "方案不完整"
                continue
            if len(steps) == 1:
                # 人工核准：只有一步就沒有方案好核可，直接做，不要多一層確認
                approved = True
                break
            ans = await ask(ctx, f"規劃模型提的方案（{len(steps)} 步），你核可才動手：\n{plan_txt}", ["照做", "換個做法", "取消"])
            if "照做" in ans or "核准" in ans or "略過" in ans:
                # 略過＝使用者不想審這題，讓流程照這版走
                approved = True
                break
            if auto and timed_out(ans):
                await emit({"type": "notice", "session_id": session_id, "text": "你沒回應，目標照這版方案做"})
                approved = True
                break
            if "取消" in ans or timed_out(ans):
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，沒有動手"})
                return
            feedback = "使用者要換一個做法"
        if not approved and auto:
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"提了 {rounds} 版，審核（{gate_name}）都沒過；目標不停下來，照最後這版做"})
        elif not approved:
            ans = await ask(ctx, f"提了 {rounds} 版，審核（{gate_name}）都沒過，最後一次意見：{feedback}。要照最後這版做，還是取消？",
                            ["照做", "取消"])
            if "取消" in ans or timed_out(ans):
                _save(session_id, goal={"objective": objective, "status": "stopped", "at": time.time()})
                await emit({"type": "notice", "session_id": session_id, "text": "已取消，沒有動手"})
                return
        await run_goal(session_id, objective, emit, steps=steps, notes=notes)
    except asyncio.CancelledError:
        g = dict(_state(session_id).get("goal") or {})   # run_goal 已存的步驟與進度留著，「繼續」才接得回來
        g.update(objective=objective, status="stopped" if g.get("steps") else "planning", at=time.time())
        _save(session_id, goal=g)
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
    GOAL_TASKS[session_id] = _track(session_id, run_gated(session_id, objective, emit, gate), emit)


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
