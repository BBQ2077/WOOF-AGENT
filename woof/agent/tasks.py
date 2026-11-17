"""任務功能：提問（ask_user）、TODO（todo）、/goal 目標執行、子代理審核、預案模式。

- ask：模型或目標執行要問使用者時，發 ask 事件，等 POST /api/asks/{id} 回答。
- todo：寫進對話的 engine_state.todos，發 todos 事件，聊天區上方顯示清單。
- /goal：規劃員把目標拆成步驟；每步由主模型執行，宣告「完成：」後交給子代理審核，
  通過才標成完成；沒過就把理由回饋給主模型重做，最多 3 次，還是不過就問使用者。
- 審核提示詞刻意寫成「只擋明顯沒做的」，避免吹毛求疵。
"""
from __future__ import annotations

import asyncio
import logging

from woof.prompts import get as _prompt
from woof.agent import internal
import json
import re
import time
import uuid

from woof import store

log = logging.getLogger("woof.tasks")

TODO_STATUS = ("pending", "doing", "done", "skipped", "failed", "cancelled")
TODO_CLOSED = ("done", "skipped", "failed", "cancelled")
PLAN_TOOLS = {"write_file", "patch", "terminal", "process"}
ASKS: dict[str, dict] = {}
GOAL_TASKS: dict[str, asyncio.Task] = {}
FLOW_BATCH: dict[str, str] = {}   # 這一輪方案流程的批次 id：拓展、方案審核、驗收子代理在側欄收成同一批
PLAN_ROUNDS = 3  # 方案最多提幾版（模型審核打回、或使用者要求重提）；設定 [flow] plan_rounds 可改
FLOW_DEFAULTS = {"plan_rounds": PLAN_ROUNDS, "fix_rounds": 1, "review_timeout": 300, "context_chars": 1500, "goal_gate": True,
                 "expand_model": "", "review_model": "", "max_steps": 30}
FLOW_LIMITS = {"plan_rounds": (1, 10), "fix_rounds": (0, 5), "review_timeout": (0, 3600), "context_chars": (0, 20000),
               "max_steps": (1, 100)}


def flow_settings() -> dict:
    """方案模式的上限（設定 → 方案模式，存在 config.toml [flow]）：
    plan_rounds 方案最多提幾版；fix_rounds 做完驗收後最多補幾輪；review_timeout 每次審核／拓展最多等幾秒（0＝不限）；
    context_chars 規劃／拓展／審核附上的前文最多幾字（0＝不附）；max_steps 方案最多排幾步；
    goal_gate /goal 是否照對話選的方案模式走；expand_model／review_model 拓展、審核用的模型（空＝跟這個對話）。"""
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
    for k in ("expand_model", "review_model"):
        out[k] = str(raw.get(k) or "")
    return out


def _sub_model(ctx, kind: str) -> str:
    """拓展／審核用哪個模型：方案模式設定有指定（且模型還在）就用它；沒有就跟這個對話。
    對話的模型不能當子代理（例如 Claude Code CLI）時回空字串，交給「模型用途 → 子代理」。"""
    from woof.agent.loop import find_model
    from woof.loader import REG

    want = flow_settings().get("expand_model" if kind == "expand_request" else "review_model") or ""
    if want and find_model(want):
        return want
    m = getattr(ctx, "model_cfg", None) or {}
    return m["id"] if m.get("id") and m.get("provider") in REG.providers else ""


async def _sub(ctx, goal: str, label: str, title: str, kind: str) -> dict | None:
    """跑審核／拓展子代理。review_timeout 算的是「卡住多久」不是總時間：
    它在思考、輸出、跑工具都算有動靜，會一直等；連續 review_timeout 秒完全沒動靜才放棄（回 None），流程照目前這版往下走。
    （模型本身連線卡住由每一步的等待上限處理、會重試；這裡是最後一道保險，不會卡死。）"""
    from woof.agent.loop import run_subagent

    lim = flow_settings()["review_timeout"]
    st = {"t": time.monotonic(), "tools": 0}

    def activity(ev: dict) -> None:
        st["t"] = time.monotonic()
        t = ev.get("type")
        if t == "tool_start":
            st["tools"] += 1
        elif t == "tool_end":
            st["tools"] = max(0, st["tools"] - 1)

    from woof.agent.loop import SUB_BATCH

    # 明確帶這一輪流程的批次（不沿用 delegate_task 留在 context 裡的那一批）；建立任務時 context 就複製進去了，馬上還原
    tok = SUB_BATCH.set(FLOW_BATCH.get(getattr(ctx, "session_id", "") or "", ""))
    try:
        task = asyncio.ensure_future(run_subagent(ctx, goal, "", _sub_model(ctx, kind), None, label,
                                                  title_hint=title, kind=kind, activity=activity))
    finally:
        SUB_BATCH.reset(tok)
    if not lim:
        return await task
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=min(lim, 10))
            if done:
                return task.result()
            # 工具在跑不算卡住（工具有自己的逾時）；其餘連續 lim 秒沒有任何事件才算
            if st["tools"] == 0 and time.monotonic() - st["t"] > lim:
                break
    except asyncio.CancelledError:
        task.cancel()
        raise
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                    "text": f"{title.split('：')[0]}（子代理）連續 {lim} 秒沒有任何動靜（沒在思考、輸出或跑工具），"
                            "當作卡住，這次不等了，照目前這版繼續"})
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


NO_REPLY_PLAN = "使用者核可方案時沒有回應（人可能不在）：照這版做，刪檔、覆蓋、對外發送這類收不回來的事先不要做，留到最後說明，等他回來再決定。"


def timed_out(answer: str) -> bool:
    return str(answer).startswith(NO_REPLY)


async def ask(turn, question: str, options: list[str] | None = None, timeout: float | None = None,
              record: bool = True) -> str:
    """問使用者。record＝答完把問答記進對話（engine_state 的 asks），之後打開對話還看得到；
    ask_user 工具不用記，工具呼叫和結果本來就存在訊息裡。"""
    aid = uuid.uuid4().hex[:12]
    after = _last_mid(turn.session_id) if record else 0
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
        done = {"id": aid, "question": question, "options": opts, "answer": str(answer),
                "skipped": bool(a.get("skipped")), "timed_out": bool(a.get("timed_out")),
                "waited": int(time.time() - t0), "at": time.time(), "after": after}
        if record and str(answer):
            try:
                _save(turn.session_id, asks=((_state(turn.session_id).get("asks") or [])[-49:] + [done]))
            except Exception:
                pass
        await turn.emit({"type": "ask_done", "session_id": turn.session_id, **done,
                         "record": bool(record and str(answer)), "tool": not record})


ASK_MAX_Q = 5        # 一次最多問幾題
ASK_MAX_OPTS = 4     # 多題卡片每題最多幾個選項（第一個是推薦；選項外一律可以自己打字）


def ask_questions(raw) -> tuple[list[dict] | None, str]:
    """整理 ask_user 的 questions：回（[{qid, question, options, multi}], 錯誤訊息）。"""
    if not isinstance(raw, list) or not raw:
        return None, "questions 要是非空陣列：[{question, options?, multi_select?}]"
    if len(raw) > ASK_MAX_Q:
        return None, f"一次最多問 {ASK_MAX_Q} 題；有先後關係的分開問"
    out = []
    for i, it in enumerate(raw):
        if isinstance(it, str):
            it = {"question": it}
        if not isinstance(it, dict) or not str(it.get("question") or "").strip():
            return None, f"questions[{i}] 要有 question"
        opts = it.get("options", it.get("choices")) or []
        if not isinstance(opts, list):
            return None, f"questions[{i}].options 要是字串陣列"
        opts = [str(o).strip() for o in opts if str(o).strip()][:ASK_MAX_OPTS]
        out.append({"qid": f"q{i}", "question": str(it["question"]).strip(), "options": opts,
                    "multi": bool(it.get("multi_select")) and bool(opts)})
    return out, ""


def _ask_line(v) -> str:
    if isinstance(v, list):
        v = "、".join(str(x).strip() for x in v if str(x).strip())
    return " ".join(str(v or "").split())


def ask_result(qs: list[dict], answer) -> str:
    """多題卡片的結果交給模型：每題一段【n】題目 → 回答／（略過）。
    answer 是 {qid: 文字或陣列}；字串表示使用者沒填卡片、直接在輸入框回話（或逾時）。"""
    if isinstance(answer, str):
        if timed_out(answer):
            return answer
        return f"使用者沒有填提問卡片，直接回話：{answer}\n（卡片上每一題都當沒回答，從這句話判斷要怎麼做）"
    rows, n = [], 0
    for i, q in enumerate(qs):
        v = _ask_line((answer or {}).get(q["qid"]))
        n += bool(v)
        rows.append(f"【{i + 1}】{q['question']}\n→ {v or '（略過，你自己判斷）'}")
    return f"使用者回答了 {n}／{len(qs)} 題：\n" + "\n".join(rows)


async def ask_many(turn, qs: list[dict], timeout: float | None = None) -> str:
    """一張卡片問多題（ask_user 的 questions），使用者一次送出。回交給模型的文字（見 ask_result）。"""
    aid = uuid.uuid4().hex[:12]
    limit = ask_timeout() if timeout is None else max(0, int(timeout))
    t0 = time.time()
    fut = asyncio.get_running_loop().create_future()
    ASKS[aid] = {"future": fut, "session_id": turn.session_id, "limit": limit,
                 "deadline": t0 + limit if limit else 0}
    head = qs[0]["question"] if len(qs) == 1 else f"{len(qs)} 個問題"
    await turn.emit({"type": "ask", "session_id": turn.session_id, "id": aid, "question": head, "options": [],
                     "questions": qs, "timeout": limit, "deadline": ASKS[aid]["deadline"]})
    answer = ""
    try:
        while True:
            dl = ASKS[aid]["deadline"]
            left = (dl - time.time()) if dl else None
            if left is not None and left <= 0:
                answer = no_reply_text(time.time() - t0)
                ASKS[aid]["timed_out"] = True
                return ask_result(qs, answer)
            try:
                answer = await asyncio.wait_for(asyncio.shield(fut), min(left, 30) if left is not None else None)
                return ask_result(qs, answer)
            except asyncio.TimeoutError:
                continue
    finally:
        a = ASKS.pop(aid, None) or {}
        if not fut.done():
            fut.cancel()
        await turn.emit({"type": "ask_done", "session_id": turn.session_id, "id": aid, "question": head,
                         "options": [], "answer": answer if isinstance(answer, str) else ask_result(qs, answer),
                         "skipped": bool(a.get("skipped")), "timed_out": bool(a.get("timed_out")),
                         "waited": int(time.time() - t0), "at": time.time(), "after": 0,
                         "record": False, "tool": True})


def touch_ask(aid: str) -> float:
    """使用者正在卡片上打字：期限從現在重新算，免得打到一半被略過。回傳新的期限（0＝不限）。"""
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return -1
    if a.get("limit"):
        a["deadline"] = time.time() + a["limit"]
    return a["deadline"]


def hold_ask(aid: str) -> bool:
    """使用者點了卡片的輸入框：這一題不再倒數，一直等到他回答或按略過。"""
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return False
    a["deadline"] = 0
    a["limit"] = 0
    return True


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


def resolve_ask(aid: str, answer) -> bool:
    """answer：一般提問是文字；多題卡片是 {qid: 文字或陣列}。"""
    a = ASKS.get(aid)
    if not a or a["future"].done():
        return False
    a["skipped"] = (not any(_ask_line(v) for v in answer.values())) if isinstance(answer, dict) else "略過" in str(answer)
    a["future"].set_result(answer)
    return True


def set_todos(session_id: str, items: list[dict]) -> list[dict]:
    """模型寫 TODO：使用者取消的項目（同樣文字）保持取消，模型不能改回；cancelled 只有使用者能設。"""
    gone = {t.get("text") for t in (_state(session_id).get("todos") or []) if t.get("status") == "cancelled"}
    clean = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            continue
        text = str(it["text"]).strip()
        st = str(it.get("status") or "pending")
        st = "cancelled" if text in gone else ("skipped" if st == "cancelled" else st if st in TODO_STATUS else "pending")
        clean.append({"text": text, "status": st})
    _save(session_id, todos=clean)
    return clean


def cancel_todo(session_id: str, index: int, text: str, cancel: bool) -> tuple[list[dict], str]:
    """使用者在畫面上取消（或恢復）一項 TODO。回傳 (新清單, 原本狀態)；找不到回 ([], "")。"""
    items = [dict(t) for t in (_state(session_id).get("todos") or [])]
    i = index if 0 <= index < len(items) and items[index].get("text") == text else \
        next((k for k, t in enumerate(items) if t.get("text") == text), None)
    if i is None:
        return [], ""
    old = str(items[i].get("status") or "pending")
    if cancel and old in ("pending", "doing", "failed"):
        items[i]["status"] = "cancelled"
    elif not cancel and old == "cancelled":
        items[i]["status"] = "pending"
    else:
        return items, old
    _save(session_id, todos=items)
    return items, old


async def _push_todos(turn_emit, session_id: str, items: list[dict]) -> None:
    await turn_emit({"type": "todos", "session_id": session_id, "items": items})


async def plan_steps(model_cfg: dict, objective: str, feedback: str = "") -> list[str]:
    from woof.agent.loop import aux_model, complete

    use = aux_model("subagent", model_cfg) or model_cfg
    text = objective + (_prompt("plan_redo", feedback=feedback) if feedback else "")
    most = flow_settings()["max_steps"]
    txt = await complete(use, _prompt("plan_goal", max_steps=most), [{"role": "user", "content": [{"type": "text", "text": text}]}])
    m = re.search(r"\[.*\]", txt or "", re.S)
    if not m:
        raise RuntimeError("規劃員沒有回傳步驟清單：" + (txt or "")[:120])
    steps = [s for s in (_step_text(x) for x in json.loads(m.group(0))) if s]
    return steps[:most]   # 空陣列＝這則訊息不用動手（閒聊／問答），交給一般回合回覆


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


def resume_digest(session_id: str, tools: int = 8, ask: bool = True) -> str:
    """「繼續」時把已經做過的事交給模型，省掉它從頭逛一遍專案（list_tree、重讀每個檔）。"""
    s = store.get_session(session_id) or {}
    out: list[str] = []
    todos = (s.get("engine_state") or {}).get("todos") or []
    if todos:
        done = sum(1 for t in todos if t.get("status") == "done")
        left = [str(t.get("text") or "") for t in todos if t.get("status") not in TODO_CLOSED]
        out.append(f"待辦 {done}/{len(todos)}" + ("；還沒完成：" + "；".join(left[:5]) if left else "（都做完了）"))
    from woof.agent.loop import bg_children

    alive = bg_children(session_id)
    if alive:
        names = "；".join(f"{k}（{str((store.get_session(k) or {}).get('title') or '')[:30]}）" for k in alive[:12])
        out.append(f"你派出的子代理還有 {len(alive)} 個在跑或排隊（重開後已自動接回，做完照樣回報）：{names}。"
                   "不要重派同樣的工作；要改方向帶它的 session_id 追加要求，不需要了才用 stop_subagent 停")
    goal = ((s.get("engine_state") or {}).get("goal") or {})
    if goal.get("objective"):
        out.append("目標：" + str(goal["objective"])[:300])
    # 「這次要做的事」取最後一則不是接續訊息的使用者訊息；拿接續訊息當任務會一層套一層
    last = next((m for m in reversed(store.get_messages(session_id))
                 if m["role"] == "user" and (m.get("meta") or {}).get("internal") != "resume"), None)
    ask_t = "".join(b.get("text", "") for b in (last or {}).get("content", []) if b.get("type") == "text").strip()
    if ask and ask_t:
        out.append("這次要做的事：" + ask_t[:300])
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
    for k in ("steps", "gaps", "ideas", "checks"):
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
    gaps = [str(x).strip() for x in raw if str(x).strip()][:5]
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


def _ref_text(ref: dict | None) -> str:
    """拓展模型想到的方向與要先查的問題。它沒有查證，只是參考：只附給執行的模型，審核不看（免得被當成規格）。"""
    ideas, checks = (ref or {}).get("ideas") or [], (ref or {}).get("checks") or []
    if not ideas and not checks:
        return ""
    out = "\n\n【拓展的參考（拓展模型沒查證的想法，不是規格）】"
    if ideas:
        out += "\n可以考慮的方向：\n" + "\n".join(f"- {x}" for x in ideas)
    if checks:
        out += "\n動手前該查的：\n" + "\n".join(f"- {x}" for x in checks)
    return out


def _ref_list(raw, n: int) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    return [s for s in (_step_text(x) for x in (raw or [])) if s][:n]


def _ref_of(g: dict) -> dict:
    return {"ideas": _ref_list(g.get("ideas"), IDEAS_MAX), "checks": _ref_list(g.get("checks"), CHECKS_MAX)}


async def final_check(ctx, session_id: str, objective: str, steps: list[str], emit,
                      prev: list[str] | None = None) -> tuple[bool, list[str]]:
    """全部跑完驗一次：目標真的做到了、做得夠好嗎（對照同類做得好的樣子，不對照拓展的猜測）。回（有沒有過, 缺什麼）。
    prev＝上一次驗收列的缺口（補完再驗）：先核對那些補好了沒，不要每次換一個審核就換一批新要求，補不完。"""
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("review") + _prompt("review_body", objective=objective, plan=plan, report=_recent_report(session_id))
    goal += ("\n\n【驗收範圍】只驗上面這些步驟排到的部分有沒有做到、做好。目標比這輪步驟大時（例如整個遊戲這輪只排到前幾個階段），"
             "步驟沒排到的部分不算缺口，不要因為整個專案還沒做完就判不過。")
    if prev:
        goal += ("\n\n【上一次驗收列的缺口，已經補過一輪】\n" + "\n".join(f"- {g}" for g in prev)
                 + "\n先逐條核對這些補好了沒（要自己看證據，不能只看報告）。"
                 "沒補好的照列；另外只有會讓目標做不到、或明顯做不好的新問題才列，挑剔小地方不算缺口。")
    r = await _sub(ctx, goal, "對抗檢查：", f"審核模型：{objective.strip()[:20]}", "review_request")
    if r is None:
        return False, []
    ok, gaps, why = _parse_check(r.get("answer") or "")
    if not ok and not gaps and why:
        gaps = [why[:120]]
    return ok, gaps


STAGE: dict[str, str] = {}   # 方案流程現在在哪一段：plan／expand／plan_review／step／wait_bg／check（插話提示、畫面狀態用）


async def _stage(session_id: str, emit, key: str, text: str = "") -> None:
    """記下流程走到哪一段，也告訴畫面：模型沒在回應的空檔（審核、等子代理）要顯示在等什麼，不能沿用上一個請求的計時。"""
    STAGE[session_id] = key
    await emit({"type": "flow_stage", "session_id": session_id, "stage": key, "text": text})


async def _settle(ctx, session_id: str, emit) -> None:
    """步驟做完、驗收之前收尾：這個對話派出去的背景子代理還沒回報就等，回報（還有等的時候你插的話）一到就馬上交給模型處理；
    模型處理時再派的也一起等。全部回來、模型也處理完了才驗收——不然驗的是還在做的半成品，子代理的結果也要等驗收完才送得進去。"""
    from woof.agent.loop import bg_children, flush_steer, peek_steer, run_model_loop

    shown = -1
    while True:
        if peek_steer(session_id):
            await asyncio.sleep(0.5)   # 同時回來的一起送
            await _stage(session_id, emit, "step")
            shown = -1
            await flush_steer(session_id, emit)
            try:
                await run_model_loop(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await emit({"type": "notice", "session_id": session_id, "text": f"處理子代理回報時出錯：{e}"})
            continue
        n = len(bg_children(session_id))
        if not n:
            return
        if n != shown:
            shown = n
            await _stage(session_id, emit, "wait_bg", f"等 {n} 個子代理回報（回報一到就交給模型；可以直接插話）")
        await asyncio.sleep(0.5)


async def _check(ctx, session_id: str, objective: str, steps: list[str], emit,
                 prev: list[str] | None = None) -> tuple[bool, list[str]]:
    """收尾後驗收。驗收途中你插話：驗收照跑，這句馬上交給主模型處理；主模型處理過插話（可能改了東西）就再驗一次。"""
    from woof.agent.loop import flush_steer, run_model_loop

    async def on_steer():
        await emit({"type": "notice", "session_id": session_id,
                    "text": "收到插話：驗收照樣跑，主模型先處理你這句"})
        await flush_steer(session_id, emit)
        await run_model_loop(ctx)

    while True:
        await _settle(ctx, session_id, emit)
        await _stage(session_id, emit, "check", "驗收中：審核模型（子代理）在看成品（可以直接插話，驗收不會停）")
        res, handled = await _keep_steer(session_id, final_check(ctx, session_id, objective, steps, emit, prev), on_steer)
        if not handled:
            return res
        await emit({"type": "notice", "session_id": session_id, "text": "驗收途中處理過你的插話，照改過的成品再驗一次"})


async def _flush_user_steer(session_id: str, emit) -> str:
    """步驟開始前，把規劃／審核／上一步結束時送到的插話先寫進對話（排在這一步的指示前面）。
    有使用者自己打的字就回一句提醒附在步驟指示後面，不然模型照方案往下做會略過它。"""
    from woof.agent.loop import flush_steer, peek_steer

    said = any(not (it.get("meta") or {}).get("internal") for it in peek_steer(session_id))
    if not await flush_steer(session_id, emit) or not said:
        return ""
    return ("\n\n（使用者在方案進行中插話了，見上面那則：照他說的調整這一步和之後的做法；"
            "跟方案衝突時以使用者說的為準，他要停下或改方向就照做。）")


async def _run_step(ctx, session_id: str, objective: str, step: str, idx: int, total: int, emit,
                    plan: list[str] | None = None, notes: str = "", ref: dict | None = None) -> bool:
    """做一步：不再每步派子代理審核（那是八股與卡住的來源），做完就往下。
    notes＝拓展模型的提醒（哪些該先查、哪些是配套），每一步都附上，不然只在畫面上閃一下、模型根本沒看到。"""
    from woof.agent.loop import run_model_loop

    plan_txt = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan or [step]))
    if notes:
        plan_txt += "\n【方案的提醒】" + notes + "\n（提醒裡點名要查的，輪到相關的步驟就先用 web_search／web_extract 真的查，不要靠猜或反覆試）"
    if _ref_text(ref):
        plan_txt += (_ref_text(ref) + "\n（這些是拓展模型沒查證就想到的，只供參考：真的去查網路上同類做得好的做法，"
                     "實作中想到更好的就用更好的；做哪些、怎麼做由你判斷，以查到的和實際做出來的為準，不要照著猜的內容硬做。"
                     "目標是把成品做好，不是做到能交差。）")
    await _stage(session_id, emit, "step")
    text = _prompt("goal_step", objective=objective, n=idx + 1, total=total, step=step, plan=plan_txt)
    text += await _flush_user_steer(session_id, emit)
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

    await _stage(session_id, emit, "step")
    text = _prompt("review_fail", reason="；".join(gaps)) + await _flush_user_steer(session_id, emit)
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


IDEAS_MAX = 10  # 拓展想到的方向最多幾條
CHECKS_MAX = 6  # 拓展點名要先查的最多幾條
RESEARCH_STEP = ("先查證：上網查同類做得好的成品與做法（拓展列的「動手前該查的」也一起查），"
                 "看哪些方向值得做、有沒有比拓展想的更好的做法，決定這次要做到哪些")


def _goal_save(session_id: str, objective: str, status: str, steps: list[str] | None = None, idx: int = 0,
               new: bool = False, notes: str | None = None, ref: dict | None = None) -> None:
    """方案的步驟與做到第幾步存在 goal 裡（不放 todos：模型自己呼叫 todo 會蓋掉），程式重開後「繼續」才接得回來。
    since＝流程開始時最後一則訊息；之後你自己下了新指令，就不再接回這個方案。"""
    g = {"objective": objective, "status": status, "at": time.time()}
    old = _state(session_id).get("goal") or {}
    if status == "running" and not new:
        g.update({k: old[k] for k in ("doing", "phase", "gaps", "rnd") if k in old})
    if steps:
        g.update(steps=steps, idx=idx, since=(None if new else old.get("since")) or _last_mid(session_id),
                 notes=notes if notes is not None else old.get("notes") or "",
                 **(ref if ref is not None else _ref_of(old)))
    _save(session_id, goal=g)


def _goal_set(session_id: str, **kw) -> None:
    """在目前的 goal 上記段落資訊（doing／phase／gaps／rnd）；值是 None 就拿掉。"""
    g = dict(_state(session_id).get("goal") or {})
    for k, v in kw.items():
        if v is None:
            g.pop(k, None)
        else:
            g[k] = v
    _save(session_id, goal=g)


REPLY_CHARS = 500   # 附前文時助手回覆最多留幾字


def _with_context(session_id: str, objective: str) -> str:
    """這則訊息接在前面的對話後面時（「0分」「不夠厲害 多想」「改成藍色」），單看這句規劃員會當成閒聊、
    拓展與審核也不知道在講什麼。附上之前在做的事與上一輪的回覆，第一則訊息就原樣回。"""
    budget = flow_settings()["context_chars"]
    if budget <= 0:
        return objective
    msgs = store.get_messages(session_id)

    def text(m):
        return "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text").strip()

    def real_user(m):
        return m["role"] == "user" and not (m.get("meta") or {}).get("internal") and text(m)

    cut = next((i for i in range(len(msgs) - 1, -1, -1) if real_user(msgs[i]) and text(msgs[i]) == objective.strip()),
               len(msgs))
    before = msgs[:cut]
    # 以使用者說的為主：這次這句整句放最前面；字數先給他之前說的（最多 3 句、每句 300 字）；
    # 助手回覆只留結尾一小段，看懂這句接在什麼後面就夠——給多了，規劃、拓展、審核會繞著模型的話轉，反而扭曲使用者的意思
    asks = [re.sub(r"\s+", " ", text(m))[:300] for m in before if real_user(m)][-3:]
    while asks and sum(len(a) for a in asks) > budget:
        asks.pop(0)
    room = min(REPLY_CHARS, budget // 3, max(0, budget - sum(len(a) for a in asks)))
    reply = next((text(m) for m in reversed(before) if m["role"] == "assistant" and text(m)), "")
    reply = re.sub(r"\s+", " ", reply)[-room:] if reply and room > 0 else ""
    if not asks and not reply:
        return objective
    out = ["【這次使用者說】" + objective]
    if asks:
        out.append("【這個對話之前使用者說的（舊→新）】\n" + "\n".join("- " + a for a in asks))
    if reply:
        out.append("【前情：上一輪助手回覆的結尾（只用來看懂這句接在什麼後面，不是需求）】\n…" + reply)
    out.append("（以使用者說的為準：照他的原話理解他要什麼，不要把助手之前提的方案、計畫、內容當成他要的，也不要改寫或延伸他的意思。"
               "如果這句是在評價或不滿意上一輪的成果，就是要把那個成果重做或改好。）")
    return "\n".join(out)


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
        meta = m.get("meta") or {}
        # 插話（方案執行中補充的話）不算新指令：模型已經看到了，方案照樣接回
        if m["role"] == "user" and not meta.get("internal") and not meta.get("steer"):
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
        # TODO 還沒開始做就被打斷：接回中斷的那一段（列 TODO／拓展審核／調 TODO／等你核可），已經做好的照用
        GOAL_TASKS[session_id] = _track(session_id, run_gated(session_id, g["objective"], emit, g["gate"],
                                                              auto=bool(g.get("auto")), extra=g.get("extra") or "",
                                                              resume=g), emit)
        return True
    GOAL_TASKS[session_id] = _track(session_id, run_goal(session_id, g["objective"], emit, steps=list(g["steps"]),
                                                         start=int(g.get("idx") or 0), resume_reason=reason,
                                                         notes=str(g.get("notes") or ""), ref=_ref_of(g)), emit)
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
                   start: int = 0, resume_reason: str = "", notes: str = "", ref: dict | None = None,
                   keep_todos: bool = False) -> None:
    """目標：拆步驟（能一步就一步）→ 照做 → 全部做完只做一次對抗檢查 → 有缺口補一輪。
    resume_reason：程式重開或停止後接回：做到一半的那一項用接續訊息做完，驗收、補做做到一半的接著那一段。"""
    from woof.agent import live
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
        steps = [s for s in (_step_text(x) for x in steps) if s][:flow_settings()["max_steps"] + 1]   # +1：拓展加的「先查證」
        if not steps:
            from woof.agent.loop import run_model_loop

            await emit({"type": "notice", "session_id": session_id, "text": "這個目標不需要拆步驟，我直接回你"})
            await run_model_loop(ctx)
            _save(session_id, goal={"objective": objective, "status": "chat", "at": time.time()})
            return
        start = max(0, min(start, len(steps)))
        cur = _state(session_id).get("todos") or []
        if (keep_todos or resume_reason) and cur:
            items = [dict(t) for t in cur]   # 模型自己列的 TODO（含狀態）照用；接回時進度也在裡面（模型中途改過也照最新的）
            steps = [t["text"] for t in items]
        else:
            items = set_todos(session_id, [{"text": s, "status": "done" if i < start else "pending"} for i, s in enumerate(steps)])
        _goal_save(session_id, objective, "running", steps, start, new=not resume_reason, notes=notes, ref=_ref_of(ref or {}))
        await _push_todos(emit, session_id, items)
        if resume_reason:
            left = sum(1 for t in items if t.get("status") in ("pending", "doing"))
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"接回方案：還有 {left}/{len(items)} 項 TODO" if left else "接回方案：TODO 都做完了，接著驗收"})
        prev_goal = _state(session_id).get("goal") or {}
        was_doing = prev_goal.get("doing") if resume_reason else None
        resume_check = dict(prev_goal) if resume_reason and prev_goal.get("phase") in ("check", "fix") else None
        # 照 TODO 一項一項做：每項開始前重讀 TODO——模型做的時候發現問題自己加項、改項、刪項，就照新的走
        failed, first, guard = 0, True, 0
        while guard < flow_settings()["max_steps"] * 3:
            guard += 1
            items = [dict(t) for t in (_state(session_id).get("todos") or items)]
            i = next((k for k, t in enumerate(items) if t.get("status") in ("pending", "doing")), None)
            if i is None:
                break
            steps = [t["text"] for t in items]
            step = steps[i]
            items[i]["status"] = "doing"
            _save(session_id, todos=items)
            _goal_save(session_id, objective, "running", steps, i)
            _goal_set(session_id, doing=step)
            await _push_todos(emit, session_id, items)
            live_steps = [t["text"] for t in items if t.get("status") != "cancelled"]
            if first and resume_reason and was_doing == step:
                ok = await _resume_step(ctx, session_id, resume_reason, emit)   # 中斷在這一項做到一半：接著做完
            else:
                ok = await _run_step(ctx, session_id, objective, step, live_steps.index(step) if step in live_steps else i,
                                     len(live_steps), emit, plan=live_steps, notes=notes, ref=ref)
            first = False
            if not ok:
                failed += 1
            items = [dict(t) for t in (_state(session_id).get("todos") or items)]
            j = i if i < len(items) and items[i].get("text") == step else \
                next((k for k, t in enumerate(items) if t.get("text") == step), None)
            if j is not None and items[j].get("status") in ("pending", "doing"):
                items[j]["status"] = "done" if ok else "failed"
            steps = [t["text"] for t in items]
            _save(session_id, todos=items)
            _goal_save(session_id, objective, "running", steps, i + 1)
            live.progress(session_id)   # 有進度：重開接回次數歸零，長工作重開幾次都照樣接回
            await _push_todos(emit, session_id, items)

        # 做完驗一次：真的做到了嗎；有缺口就補，最多補 fix_rounds 輪（設定 → 方案模式），不會無限打回。
        # 純問答在前面 steps 為空時就 return 了，不會走到這。
        # 驗收、補做在哪一段也存著：程式重開接回時，補到一半的接著補完、再驗，輪數照算，不從頭驗
        fix_max = flow_settings()["fix_rounds"]
        _goal_set(session_id, doing=None)
        cur_todos = _state(session_id).get("todos") or []
        if cur_todos:   # 使用者取消的項目不交給驗收，不然會被當成缺口補回來
            steps = [t["text"] for t in cur_todos if t.get("status") != "cancelled"] or steps
        rc = resume_check if first else None   # 接回時 TODO 還有沒做的就照常做完、重新驗收
        if rc and rc.get("phase") == "fix" and rc.get("gaps"):
            rnd0, gaps = int(rc.get("rnd") or 1), list(rc["gaps"])
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"接回方案：接著補第 {rnd0}/{fix_max} 輪（上次驗收列的缺口）"})
            _goal_set(session_id, phase="fix", gaps=gaps, rnd=rnd0)
            await _resume_step(ctx, session_id, resume_reason, emit)
            ok, gaps = await _check(ctx, session_id, objective, steps, emit, prev=gaps)
        else:
            rnd0 = 0
            _goal_set(session_id, phase="check")
            ok, gaps = await _check(ctx, session_id, objective, steps, emit)
        for rnd in range(rnd0 + 1, fix_max + 1):
            if ok or not gaps:
                break
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"審核模型（子代理）看過，認為還缺（補第 {rnd}/{fix_max} 輪）：" + "；".join(gaps)})
            _goal_set(session_id, phase="fix", gaps=gaps, rnd=rnd)
            fixed = await _run_fix(ctx, session_id, gaps, emit)
            if not fixed:
                break
            live.progress(session_id)
            _goal_set(session_id, phase="check")
            ok, gaps = await _check(ctx, session_id, objective, steps, emit, prev=gaps)

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


AFTER_FLOW = None   # server 掛上：流程正常結束還有沒送進去的插話，接著開一回合處理


def steer_note(session_id: str) -> str:
    """方案流程在跑時插話：告訴使用者這則什麼時候會送到模型。"""
    g = _state(session_id).get("goal") or {}
    st = STAGE.get(session_id)
    if st == "plan_review":
        return "已插入：主模型馬上照你這句調整 TODO，拓展／審核照樣看完再補意見"
    if g.get("status") == "planning" or st in ("plan", "expand"):
        return "已插入：主模型列 TODO 時馬上看到這句"
    if st == "check":
        return "已插入：驗收照樣跑，這句馬上交給主模型處理"
    if st == "wait_bg":
        return "已插入，馬上交給模型處理（子代理照樣在背景跑）"
    return "已插入，這一步的工具跑完就送給模型"


def _track(session_id: str, coro, emit) -> asyncio.Task:
    """跑目標／方案流程。結束（做完、出錯、被停）時補送一個事件：
    流程裡最後一個事件（回覆的 run_end）送出時流程還沒結束，帶的 busy 是 True；
    不補這一下，前端會一直顯示「還在跑」。"""
    from woof.agent import live

    FLOW_BATCH[session_id] = uuid.uuid4().hex[:8]   # 一輪流程（一次要求、或按一次「繼續」）一批
    live.mark(session_id, "flow")   # 程式重開後自動接回這個方案
    task = asyncio.create_task(coro)
    # 開頭就送一下：拓展、審核在子對話跑的那段，主對話自己沒有事件，前端拿不到「流程進行中」會以為卡住
    # （這時任務已登記在 GOAL_TASKS，送出時 broadcast 算出的 flow 是 True）
    asyncio.get_running_loop().create_task(emit({"type": "flow_start", "session_id": session_id}))

    def _done(_t):
        STAGE.pop(session_id, None)
        if GOAL_TASKS.get(session_id) in (None, task):
            live.unmark(session_id, "flow")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(emit({"type": "flow_end", "session_id": session_id}))
            if AFTER_FLOW is not None and not _t.cancelled():
                loop.create_task(AFTER_FLOW(session_id))
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


async def review_plan(parent, objective: str, steps: list[str], prev: str = "") -> tuple[bool, str]:
    """prev＝上一版被打回的意見：先看那些改到了沒，再照同一個標準判斷這版過不過關。
    拓展的想法不給審核看：審核看了會把沒查證的猜測要求寫進方案，又變回總規劃。"""
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    goal = _prompt("plan_review") + _prompt("plan_review_body", objective=objective, plan=plan)
    if prev:
        goal += (f"\n\n【上一版被打回的意見】{prev}\n這版是照上面的意見改的。先看這些有沒有改到，"
                 "再照同一個標準判斷這版過不過關。")
    r = await _sub(parent, goal, "方案審核：", "審核模型：方案審核", "review_request")
    if r is None:
        return True, "審核超時，照這版做"
    j = _jobj(r.get("answer") or "", "pass")
    if j is None:
        return False, "方案審核回覆格式不對"
    return bool(j.get("pass")), str(j.get("reason") or "")


async def expand_plan(parent, objective: str, steps: list[str]) -> tuple[dict, str]:
    """「方案・拓展」：拓展思路——做完整、做得好還可能包括哪些（A→A/B/C/D），動手前該先查什麼。
    拓展模型沒有查證，回的只是參考（不改步驟、不當規格），回（{ideas, checks}, 一句話說明）。"""
    plan = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    # 只給使用者說的（含前文）：拓展照使用者的話想，不從規劃員的步驟（模型的理解）出發
    goal = _prompt("expand") + _prompt("expand_body", objective=objective, plan=plan)
    r = await _sub(parent, goal, "思維拓展：", "拓展模型：思維拓展", "expand_request")
    if r is None:
        return {"ideas": [], "checks": []}, ""
    j = _jobj(r.get("answer") or "", "ideas") or {}
    ref = _ref_of(j)
    if ref["ideas"] or ref["checks"]:
        return ref, str(j.get("notes") or "")[:300]
    if parent is not None:
        await parent.emit({"type": "notice", "session_id": parent.session_id,
                           "text": "拓展模型（子代理）的回覆讀不出想法，照原本的方案做"})
    return ref, ""


async def _keep_steer(session_id: str, coro, on_steer):
    """跑拓展、審核、驗收這類子代理時盯著插話：你一開口就交給主模型處理（on_steer），子代理照樣跑完、不收掉。
    要不要停子代理由主模型判斷。回傳（結果, 途中有沒有處理過插話）。"""
    task = asyncio.ensure_future(coro)
    handled = False
    try:
        while True:
            if _user_steer_waiting(session_id):
                await on_steer()
                handled = True
                continue
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result(), handled
    except asyncio.CancelledError:
        task.cancel()
        raise


def _user_steer_waiting(session_id: str) -> bool:
    from woof.agent.loop import peek_steer

    return any(not (it.get("meta") or {}).get("internal") for it in peek_steer(session_id))


async def _take_steer(session_id: str, emit) -> str:
    """把排隊的插話寫進對話，回傳使用者打的字（逐則一行），併進規劃目標。"""
    from woof.agent.loop import pop_steer

    said = []
    for it in pop_steer(session_id):
        m = store.add_message(session_id, "user", it["blocks"], it.get("meta"))
        await emit({"type": "message", "session_id": session_id, "message": m})
        if not (it.get("meta") or {}).get("internal"):
            t = "".join(b.get("text", "") for b in it["blocks"] if b.get("type") == "text").strip()
            if t:
                said.append("- " + t)
    await emit({"type": "queue", "session_id": session_id, "items": []})
    return "\n".join(said)


async def _model_todo(ctx, session_id: str, said: str, emit, ref: dict | None = None, notes: str = "",
                      feedback: str = "") -> list[str]:
    """執行的模型自己整理 TODO：它有完整的對話與工具，先盤點現況，列出從現在到成品實際要做的事。
    feedback＝審核或你給的意見：照意見調整已經列的 TODO。回還沒做的項目；沒寫 TODO（只是問答）回空。"""
    from woof.agent.loop import run_model_loop

    await _stage(session_id, emit, "plan", "整理 TODO：執行的模型在盤點現況、列到成品要做的事（可以直接插話）")
    if feedback:
        text = _prompt("plan_todo_fix", feedback=feedback)
        kind = "plan_feedback"
    else:
        set_todos(session_id, [])
        await _push_todos(emit, session_id, [])
        ideas = ""
        if _ref_text(ref) or notes:
            ideas = ("\n\n【拓展模型想到的方向（沒查證，只當參考；做哪些由你判斷）】" + (_ref_text(ref) or "")
                     + (f"\n{notes}" if notes else ""))
        text = _prompt("plan_todo", objective=said, max_steps=flow_settings()["max_steps"]) + ideas
        kind = "plan_request"
    m = store.add_message(session_id, "user", [{"type": "text", "text": text}], meta=internal.meta(kind))
    await emit({"type": "message", "session_id": session_id, "message": m})
    await run_model_loop(ctx)
    items = _state(session_id).get("todos") or []
    return [t["text"] for t in items if t.get("status") in ("pending", "doing")][:flow_settings()["max_steps"]]


async def run_gated(session_id: str, objective: str, emit, gate: str, auto: bool = False, extra: str = "",
                    resume: dict | None = None) -> None:
    """方案模式：主模型先判斷要不要動手、自己列 TODO → 拓展／審核同時看一次、只給意見 → 主模型調一次 → 照 TODO 做 → 驗收。

    gate：human（人工核准 TODO）、model（審核給意見）、expand（拓展＋審核給意見）。問答、閒聊直接回答，不拓展、不審核。
    每段開始前把目前在哪一段（phase）存進 goal：程式重開、停止後 resume 帶著這份紀錄接回那一段，
    已經列好的 TODO、已經拿到的意見照用，不從頭重來。
    """
    from woof.agent import live
    from woof.agent.loop import TurnContext
    from woof.loader import REG

    session = store.get_session(session_id)
    model_cfg = REG.model_cfg((session or {}).get("model"))
    if model_cfg is None or model_cfg.get("provider") not in REG.providers:
        await emit({"type": "notice", "session_id": session_id, "text": "方案模式需要 API 型模型，請換一個模型"})
        return
    ctx = TurnContext(session, model_cfg, (session or {}).get("effort") or model_cfg.get("default_effort"), emit)
    rounds = flow_settings()["plan_rounds"]
    raw_objective = objective   # 存檔用原句：接回時會再附一次前文，不能疊兩層
    objective = _with_context(session_id, objective) + extra
    notes, ref = "", None
    old_todos = [] if resume else list(_state(session_id).get("todos") or [])
    phase = (resume or {}).get("phase") or "todo"
    fb_text = (resume or {}).get("fb") or ""
    since = int((resume or {}).get("since") or 0) or _last_mid(session_id)

    def save_planning(ph: str, **kw):
        # 現在在哪一段存著：被打斷（程式重開、停止）時接回這一段，不會從頭重來，也不會變成沒有審核的一般接續
        _save(session_id, goal={"objective": raw_objective, "status": "planning", "at": time.time(),
                                "gate": gate, "auto": auto, "since": since, "extra": extra, "phase": ph, **kw})

    def pending() -> list[str]:
        items = _state(session_id).get("todos") or []
        return [t["text"] for t in items if t.get("status") in ("pending", "doing")][:flow_settings()["max_steps"]]

    try:
        said = raw_objective + extra
        steps = pending() if resume else []
        if resume:
            await emit({"type": "notice", "session_id": session_id,
                        "text": {"todo": "接回方案：TODO 已經列好，接著給拓展／審核看" if steps else "接回方案：接著整理 TODO",
                                 "opinions": "接回方案：拓展／審核接著看 TODO",
                                 "fix_todo": "接回方案：主模型接著照意見調 TODO",
                                 "approve": "接回方案：TODO 等你核可"}.get(phase, "接回方案")})
        if phase == "todo":
            if not steps:
                save_planning("todo")
                # 先由主模型判斷要不要動手：問答、閒聊直接回答，不拓展、不審核；要動手才列 TODO
                steps = await _model_todo(ctx, session_id, said, emit)
                if not steps:
                    if old_todos and not (_state(session_id).get("todos") or []):
                        set_todos(session_id, old_todos)   # 問答：原本的 TODO 放回去
                        await _push_todos(emit, session_id, old_todos)
                    _save(session_id, goal={"objective": raw_objective, "status": "chat", "at": time.time()})
                    return
            live.progress(session_id)   # TODO 列好了：算進度
            phase = "approve" if gate == "human" else "opinions"
        if gate in ("model", "expand") and phase == "opinions":
            save_planning("opinions")
            who = "拓展、審核模型（子代理）" if gate == "expand" else "審核模型（子代理）"
            await _stage(session_id, emit, "plan_review", f"{who}在看 TODO（{len(steps)} 項）（可以直接插話）")

            async def opinions():
                jobs = [review_plan(ctx, objective, steps, "")]
                if gate == "expand":
                    jobs.append(expand_plan(ctx, objective, steps))
                return await asyncio.gather(*jobs, return_exceptions=True)

            fb = []

            async def on_steer():
                # 你插話了：主模型馬上照你說的調 TODO；拓展、審核照樣看完，意見之後再補給主模型
                nonlocal steps
                await _take_steer(session_id, emit)
                await emit({"type": "notice", "session_id": session_id,
                            "text": "收到插話：主模型先照你說的調整 TODO，拓展／審核照樣看完再補意見"})
                steps = await _model_todo(ctx, session_id, said, emit,
                                          feedback="【使用者剛插話（在對話裡，以他說的為準）】") or steps

            res, _ = await _keep_steer(session_id, opinions(), on_steer)
            rv = res[0]
            if isinstance(rv, BaseException):
                log.warning("審核 TODO 失敗：%s", rv)
            else:
                ok, reason = rv
                if reason and not ok:
                    fb.append("【審核】" + reason)
                await emit({"type": "notice", "session_id": session_id,
                            "text": f"審核模型（子代理）看過 TODO（{len(steps)} 項）："
                                    + (f"意見：{reason}" if reason and not ok else "沒意見" + (f"。{reason}" if reason else ""))})
            if gate == "expand" and len(res) > 1:
                ex = res[1]
                if isinstance(ex, BaseException):
                    log.warning("拓展失敗：%s", ex)
                else:
                    r_ref, r_notes = ex
                    idea = _ref_text(r_ref) + (f"\n{r_notes}" if r_notes else "")
                    if idea.strip():
                        fb.append("【拓展（沒查證，只當參考）】" + idea.strip())
                    if r_ref.get("ideas") or r_ref.get("checks"):
                        await emit({"type": "notice", "session_id": session_id,
                                    "text": f"拓展模型（子代理）想到 {len(r_ref['ideas'])} 個方向、{len(r_ref['checks'])} 個該先查的"
                                            + (f"：{r_notes}" if r_notes else "") + "（只當參考，交給主模型自己判斷）"})
            fb_text = "\n\n".join(fb)
            live.progress(session_id)   # 意見拿到了：算進度
            phase = "fix_todo" if fb_text else "go"
        if phase == "fix_todo" and fb_text:
            save_planning("fix_todo", fb=fb_text)
            steps = await _model_todo(ctx, session_id, said, emit, feedback=fb_text) or steps
        if gate == "human" and phase == "approve":
            rnd = 1
            while len(steps) > 1:
                save_planning("approve")
                plan_txt = "\n".join(f"{k + 1}. {s}" for k, s in enumerate(steps))
                ans = await ask(ctx, f"主模型列的 TODO（{len(steps)} 項），你核可才動手：\n{plan_txt}", ["照做", "換個做法", "取消"])
                if "照做" in ans or "核准" in ans or "略過" in ans:
                    break
                if timed_out(ans):
                    await emit({"type": "notice", "session_id": session_id, "text": "你沒回應，照這版 TODO 做"})
                    notes = (notes + "\n" if notes else "") + NO_REPLY_PLAN
                    break
                if "取消" in ans:
                    _save(session_id, goal={"objective": raw_objective, "status": "cancelled", "at": time.time(),
                                            "since": _last_mid(session_id)})
                    await emit({"type": "notice", "session_id": session_id, "text": "已取消，沒有動手"})
                    return
                fb = "使用者要換一個做法" if "換個做法" in ans else f"使用者的意見：{ans}"
                rnd += 1
                if rnd > rounds:
                    notes = (notes + "\n" if notes else "") + fb
                    await emit({"type": "notice", "session_id": session_id,
                                "text": f"已經換了 {rounds} 版，照這版做，你最後的意見交給主模型"})
                    break
                steps = await _model_todo(ctx, session_id, said, emit, feedback=fb) or steps
        if not steps:
            steps = [t["text"] for t in _state(session_id).get("todos") or []]   # 都做完了：直接進驗收
        # 照 TODO 做時只帶目標與前文；「繼續」當下的進度摘要只給列 TODO 用，不跟著每一步送（過時了會誤導主模型）
        await run_goal(session_id, _with_context(session_id, raw_objective), emit, steps=steps, notes=notes, ref=ref,
                       keep_todos=True)
    except asyncio.CancelledError:
        g = dict(_state(session_id).get("goal") or {})   # 已存的段落、步驟與進度留著，接回時從這裡接
        if g.get("steps"):
            g.update(status="stopped", at=time.time())
        else:
            g.update(objective=raw_objective, status="planning", at=time.time())
        _save(session_id, goal=g)
        raise
    except Exception as e:
        _save(session_id, goal={"objective": raw_objective, "status": "error", "error": str(e)[:300]})
        await emit({"type": "notice", "session_id": session_id, "text": f"方案流程失敗：{e}"})


def is_running(session_id: str) -> bool:
    """這個對話有沒有目標／方案流程在跑（不含一般回合）。"""
    t = GOAL_TASKS.get(session_id)
    return bool(t and not t.done())


def start_gated(session_id: str, objective: str, emit, gate: str, extra: str = "") -> None:
    t = GOAL_TASKS.get(session_id)
    if t and not t.done():
        raise RuntimeError("這個對話已經有目標或方案在執行，先按停止")
    GOAL_TASKS[session_id] = _track(session_id, run_gated(session_id, objective, emit, gate, extra=extra), emit)


def continue_gated(session_id: str, emit, gate: str, reason: str) -> str:
    """方案模式下按「繼續」，但沒有做到一半的方案可接（流程已結束、或這段是一般回合做的）：
    整個重走方案流程（規劃 → 拓展／審核 → 照做 → 驗收），規劃時附上已經做過的事，只排還沒做完的。
    回 started＝已開始；cancelled＝你按過取消、之後沒再下指令，不自動做；空字串＝找不到要做的事，照一般接續。"""
    g = _state(session_id).get("goal") or {}
    msgs = store.get_messages(session_id)
    last = next((m for m in reversed(msgs)
                 if m["role"] == "user" and not (m.get("meta") or {}).get("internal")), None)
    if g.get("status") == "cancelled" and (last is None or int(last["id"]) <= int(g.get("since") or 0)):
        return "cancelled"
    ask_text = "".join(b.get("text", "") for b in (last or {}).get("content", []) if b.get("type") == "text").strip()
    if not ask_text:
        return ""
    from woof.agent.loop import clear_interrupted

    s2 = clear_interrupted(session_id)
    if s2:
        asyncio.get_running_loop().create_task(emit({"type": "session_updated", "session": s2}))
    start_gated(session_id, ask_text, emit, gate, extra=progress_note(session_id, reason))
    return "started"


def progress_note(session_id: str, reason: str) -> str:
    """接在方案目標後面的現況：上一回合中途停下前已經做過的事（只排還沒做完的）。前文與這次的要求由方案流程自己附。"""
    digest = resume_digest(session_id, ask=False)
    if not digest:
        return ""
    return (f"\n\n【現況】上一回合中途停下（{reason}），之前已經做了一部分。"
            "規劃前先看這段，做完的不要重做；這次的要求和之前不同時，以這次為準。\n" + digest)


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
    ctx = TurnContext(session, model_cfg, session.get("effort") or model_cfg.get("default_effort"), emit)
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
    ctx = TurnContext(session, model_cfg, session.get("effort") or model_cfg.get("default_effort"), emit)
    ok, reason = await review(ctx, f"上一則使用者要求：{request[:600]}", report)
    text = f"【子代理審核】{'通過' if ok else '未通過'}：{reason}"
    m = store.add_message(session_id, "assistant", [{"type": "text", "text": text}])
    await emit({"type": "message", "session_id": session_id, "message": m})
    return {"ok": ok, "reason": reason}
