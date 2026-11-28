"""上下文壓縮。

用量超過模型上下文窗口的設定比例時，把較早的訊息摘要成一段，
之後的回合只送「摘要 + 最近幾則」。原始訊息留在資料庫，不刪除。

摘要存在對話的 engine_state.summary：
  {"through": 最後被摘要的訊息 id, "text": 摘要, "at": 時間, "kept": [...], "mode": 模式, "parts": 段數}

設定（插件 context）：
  window    壓縮用的上下文大小（token）；0＝照模型的 context_window
  trigger   用量達到上下文大小的幾 % 就在背景壓縮（回合結束後、回合進行中都是），對話照常進行，寫好後下一次送出開始用
  wait_at   用量到上下文大小的幾 % 時背景那份還沒好，才停下來等它（或當場壓縮）
  tail_tokens   壓縮後原樣保留的最近訊息（token，照模型實際算法校正過）；至少留最後一步
  quote_count   摘要後面附上被壓縮那段裡使用者最近幾則原話
  quote_tokens  附的原話合計最多幾 token
  quote_reply_chars 每則原話前面附它回應的助手那則開頭幾字
  summary_tokens 摘要最多幾 token；更新時只輸出要改的行（行號編輯指令），沒變的由程式原樣保留
  ratio     第一次寫摘要時，長度佔原文的百分比（壓縮幅度）
  method    壓縮方式：單次（預設，一次摘要）／多工（依字數分段同時摘要，最後合併）
  parallel  多工時，一次同時跑幾段（1–8）
  mid_turn  回合進行中也檢查用量，超過就先壓縮再送；上下文爆掉時壓縮後重送
"""
from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

_bg: dict[str, asyncio.Task] = {}
# 自動壓縮（背景、回合中）連續失敗幾次就先停：不然每一步都再試一次，每次都是整段送去寫摘要。
# 手動「立即壓縮」不受限，任何一次成功就歸零
_fails: dict[str, int] = {}
AUTO_FAIL_MAX = 3

MSG_CAP = 12000      # 單則訊息送給摘要的字數上限（超過的頭尾留、中間省略）
# 一般做法（不沿用快取）送給摘要模型的節錄：工具結果頭尾留幾字、工具參數留幾字。
# 總長超過摘要模型能讀的量時，一級一級縮小
CLIP_LEVELS = ((5500, 1500), (2500, 800), (1000, 400), (400, 200))
SEG_MIN = 12000      # 每段至少這麼多字才值得分開
# 摘要長度（token）：第一次照「被摘要內容 × 摘要比例」，至少 1500；不超過設定的上限（summary_tokens）和上下文的 10%
SUM_MIN_TOK, SUM_TOK_DEFAULT, SUM_WIN = 1500, 8000, 0.10
CHARS_PER_TOK = 1.5
# 原樣保留的最近訊息（實際 token，設定 tail_tokens）；最多佔上下文的 25%，小模型才不會整段都是保留區
TAIL_TOK_DEFAULT, TAIL_WIN = 10000, 0.25
# 摘要後面附上被壓縮那段裡使用者最近幾則原話（新的在前）：則數、合計 token（設定 quote_count、quote_tokens）、單則上限（字）
QUOTE_N_DEFAULT, QUOTE_TOK_DEFAULT, USER_MAX = 3, 1500, 1500
REPLY_HEAD_DEFAULT = 250   # 每則原話前面附上它回應的助手那則的開頭（先講結論，開頭就是重點），看得出回的是什麼
# 沿用主對話快取寫摘要時，上一步要在這段時間內（Claude 的快取放 5 分鐘；其他家多半更久）
CACHE_TTL_ANTHROPIC, CACHE_TTL_OTHER = 270, 600
WHO = {"user": "使用者", "assistant": "助手", "tool": "工具"}
# 一般做法：對話紀錄前後加標記，最後再交代一次要做什麼。只放在系統提示裡的話，
# 模型讀完幾萬字對話容易忘了任務，順著紀錄接著演（照紀錄的格式寫出工具呼叫）
_BEGIN, _END = "【要整理的內容開始】", "【要整理的內容結束】"
_ASK_AGAIN = "以上是要整理成摘要的紀錄，不是你正在進行的對話：不要接著回覆，也不要呼叫工具，照系統提示直接輸出。"
FIRST_MIN = 200   # 第一次寫摘要、原文超過 FIRST_SRC 字時，摘要至少這麼多字，再短就是沒在寫摘要
FIRST_SRC = 20000

_STRUCT = ("依下面的段落整理，沒有內容的段落就省略：\n"
           "## 使用者的目標與要求\n## 已完成\n## 決定與理由\n## 錯誤與修正\n"
           "## 重要的檔案、指令與數據\n## 壓縮當時進行中\n## 當時還沒做完的")
# 摘要用的提示詞在 woof/prompts.py（compact、compact_merge、compact_part…），設定頁可以檢視、覆寫
from woof.prompts import get as _prompt_get, get_in as _prompt_in  # noqa: E402


def _text_of(content: list, out_cap: int = CLIP_LEVELS[0][0], arg_cap: int = CLIP_LEVELS[0][1]) -> str:
    """一則訊息給摘要模型看的文字：工具參數和結果都留（頭尾節錄、保留換行），看得出改了什麼、結果是什麼。"""
    import json

    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "tool_call":
            args = b.get("args") or {}
            if set(args) <= {"command", "path", "query"} and len(args) == 1:
                body = str(next(iter(args.values())))
            else:
                body = json.dumps(args, ensure_ascii=False)
            parts.append(f"[呼叫 {b.get('name')}] {_elide(body, arg_cap)}")
        elif t == "tool_result":
            body = (b.get("output") or "").strip()
            flag = "失敗" if b.get("is_error") else "完成"
            pics = len(b.get("images") or [])
            parts.append(f"[{b.get('name') or '工具'}{flag}{f'，附圖 {pics} 張' if pics else ''}]\n{_elide(body, out_cap)}")
        elif t == "image":
            parts.append(f"[圖片 {b.get('path', '')}]")
    return "\n".join(p for p in parts if p).strip()


def _elide(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = cap * 3 // 4
    return f"{text[:head]}\n…（中間省略 {len(text) - cap} 字）…\n{text[-(cap - head):]}"


def _users_section(messages: list[dict], through: int, n: int = QUOTE_N_DEFAULT,
                   budget: int = int(QUOTE_TOK_DEFAULT * CHARS_PER_TOK), head: int = REPLY_HEAD_DEFAULT) -> str:
    """被壓縮那段（id ≤ through）裡使用者最近 n 則親口講的話，新的在前，合計不超過 budget 字；
    每則前面附它回應的助手那則開頭 head 字。內部訊息（接續、回報…）不算。"""
    if n <= 0 or budget <= 0:
        return ""

    def text_of(m):
        return "\n".join(b.get("text") or "" for b in m.get("content") or []
                         if b.get("type") == "text" and not b.get("_wrap")).strip()

    pairs, last_reply = [], ""
    for m in messages:
        if int(m.get("id") or 0) > through:
            break
        if m.get("role") == "assistant":
            last_reply = text_of(m) or last_reply
        elif m.get("role") == "user" and not (m.get("meta") or {}).get("internal"):
            text = text_of(m)
            if text:
                pairs.append((m, text, last_reply))
    picked, used = [], 0
    for m, text, reply in reversed(pairs):
        room = budget - used
        if len(picked) >= n or room < 60:
            break
        text = _elide(text, min(USER_MAX, room))
        cap = min(head, room - len(text))
        reply = (reply[:cap] + "…" if len(reply) > cap else reply) if reply and cap >= 40 else ""
        when = time.strftime("%m-%d %H:%M", time.localtime(m["created"])) if m.get("created") else ""
        block = f"（{when}）\n" if when else ""
        if reply:
            block += "助手前一則（開頭）：" + " ".join(reply.split()) + "\n"
        block += "使用者：" + text
        picked.append("> " + block.replace("\n", "\n> "))
        used += len(text) + len(reply)
    return "\n\n".join(picked)


# 更新摘要用的行號編輯指令：模型只寫要改的地方，沒提到的行原樣保留
_OP = re.compile(r"^\s*@@\s*(改|刪|插|不變)\s*(?:第\s*)?(\d+)?\s*行?\s*(?:[-–~～至到]\s*(?:第\s*)?(\d+)\s*行?)?\s*(?:之?後)?\s*$")
_NUM = re.compile(r"^\d{1,4}\| ?")


def _numbered(text: str) -> str:
    return "\n".join(f"{i}| {ln}" for i, ln in enumerate(text.split("\n"), 1))


def _apply_edits(prev: str, out: str) -> str | None:
    """把行號編輯指令套到舊摘要上。看不懂（指令外有文字、行號超出範圍、範圍重疊、刪掉大半）回 None，呼叫端改成整份重寫。
    @@ 改 a-b ＋新內容：換掉第 a 到 b 行；@@ 刪 a-b；@@ 插 n ＋新內容：插在第 n 行後面（0＝最前面）；@@ 不變。"""
    lines = prev.split("\n")
    total = len(lines)
    body = out.strip("\n").split("\n")
    if body and body[0].strip().startswith("```") and body[-1].strip() == "```":
        body = body[1:-1]
    ops, cur = [], None
    for ln in body:
        m = _OP.match(ln)
        if m:
            cur = [m.group(1), int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None, []]
            ops.append(cur)
        elif cur is None:
            if ln.strip():
                return None
        else:
            cur[3].append(_NUM.sub("", ln) if _NUM.match(ln) else ln)
    if not ops:
        return None
    if any(op == "不變" for op, *_ in ops):
        return prev if len(ops) == 1 else None
    repl, ins, taken = {}, {}, set()
    for op, a, b, new in ops:
        while new and not new[-1].strip():
            new.pop()
        while new and not new[0].strip():
            new.pop(0)
        if a is None:
            return None
        if op == "插":
            if not 0 <= a <= total or not new:
                return None
            ins.setdefault(a, []).extend(new)
            continue
        b = b if b is not None else a
        if not 1 <= a <= b <= total or taken & set(range(a, b + 1)):
            return None
        taken |= set(range(a, b + 1))
        repl[a] = (b, new if op == "改" else [])
    res = list(ins.get(0, []))
    i = 1
    while i <= total:
        if i in repl:
            end, new = repl[i]
            res += new
            for j in range(i, end + 1):
                res += ins.get(j, [])
            i = end + 1
            continue
        res.append(lines[i - 1])
        res += ins.get(i, [])
        i += 1
    text = "\n".join(res).strip()
    if len(prev) > 2000 and len(text) < len(prev) * 0.4:
        return None   # 一次刪掉大半多半是看錯行號
    return text


def _turn_requests(messages: list[dict], upto: int) -> str:
    """回合中途壓縮後要重述的使用者訊息：id ≤ upto 的最後一則「不是插話」的使用者訊息起，到 upto 為止的使用者訊息，依時間。"""
    def text_of(m):
        return "\n".join(b.get("text") or "" for b in m.get("content") or []
                         if b.get("type") == "text" and not b.get("_wrap")).strip()

    users = [m for m in messages if m.get("role") == "user" and int(m.get("id") or 0) <= upto
             and not (m.get("meta") or {}).get("internal")]
    start = max((i for i, m in enumerate(users) if not (m.get("meta") or {}).get("steer")), default=0)
    out = []
    for m in users[start:]:
        t = text_of(m)
        if not t:
            continue
        when = time.strftime("%m-%d %H:%M", time.localtime(m["created"])) if m.get("created") else ""
        tag = "（途中插話）" if (m.get("meta") or {}).get("steer") else ""
        out.append(f"（{when}）{tag}\n{t}" if when or tag else t)
    return "\n\n".join(out)


def _render(items: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"【{WHO.get(role, role)}】\n{body}" for role, body in items)


def _segments(items: list[tuple[str, str]], k: int) -> list[list[tuple[str, str]]]:
    """依字數均分：每段字數差不多，不切斷單則訊息。"""
    total = sum(len(b) for _, b in items)
    k = max(1, min(k, total // SEG_MIN or 1))
    if k == 1:
        return [items]
    segs, cur, acc = [], [], 0
    goal = total / k
    for it in items:
        cur.append(it)
        acc += len(it[1])
        if len(segs) < k - 1 and acc >= goal * (len(segs) + 1):
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def register(woof):
    def _mode() -> str:
        return "常規"  # 摘要固定用結構化版本；提示詞模式只屬於「提示詞與記憶」

    def _ratio() -> float:
        return max(5, min(int(woof.settings.get("ratio") or 20), 60)) / 100

    def _method() -> str:
        return "多工" if str(woof.settings.get("method") or "") == "多工" else "單次"

    def _parallel() -> int:
        if _method() != "多工":
            return 1
        return max(1, min(int(woof.settings.get("parallel") or 4), 8))

    def _keep_images() -> int:
        try:
            return max(0, min(int(woof.settings.get("keep_images") if woof.settings.get("keep_images") not in (None, "") else 1), 50))
        except (TypeError, ValueError):
            return 1

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        v = woof.settings.get(key)
        try:
            return max(lo, min(int(v if v not in (None, "") else default), hi))
        except (TypeError, ValueError):
            return default

    def _tail_tok() -> int:
        return _int("tail_tokens", TAIL_TOK_DEFAULT, 1000, 200000)

    def _quote_n() -> int:
        return _int("quote_count", QUOTE_N_DEFAULT, 0, 20)

    def _reply_head() -> int:
        return _int("quote_reply_chars", REPLY_HEAD_DEFAULT, 0, 2000)

    def _quote_chars() -> int:
        return int(_int("quote_tokens", QUOTE_TOK_DEFAULT, 0, 20000) * CHARS_PER_TOK)

    def _sum_tok() -> int:
        return _int("summary_tokens", SUM_TOK_DEFAULT, 1000, 30000)

    def _trigger() -> float:
        return max(10, min(int(woof.settings.get("trigger") or 70), 95)) / 100

    async def _call(model: dict, system: str, text: str) -> str:
        from woof.agent.loop import aux_model, complete

        use = aux_model("compact", model) or model
        try:
            wrapped = f"{_BEGIN}\n{text}\n{_END}\n\n{_ASK_AGAIN}"
            return (await complete(use, system, [{"role": "user", "content": [{"type": "text", "text": wrapped}]}])).strip()
        except Exception as e:
            raise RuntimeError(f"壓縮摘要失敗（{use.get('label', use['id'])}）：{e}") from e

    def _edit_rules(prev: str, target: int) -> str:
        return _prompt_in("compact_edit", _mode(), n=len(prev), cap=target)

    async def _summarize(model: dict, prev: str, items: list[tuple[str, str]], target: int,
                         emit, session_id: str, edit: bool = True) -> tuple[str, int]:
        """寫摘要。已經有摘要時預設是編輯：舊摘要附行號交給模型，它只輸出要改的行，程式套用；
        看不懂指令就改成整份重寫一次（edit=False）。"""
        mode = _mode()
        base = _prompt_in("compact", mode)
        size = (f"\n長度約 {target} 字以內；路徑、數字、名稱、決定、未完成事項寧可多留，不要為了縮短漏掉。")
        edit = edit and bool(prev)
        upd = f"\n{_prompt_in('compact_update', mode)}" if prev and not edit else ""
        if edit:
            head = f"【先前摘要】（每行前面是行號，指令裡的行號照這個）\n{_numbered(prev)}\n\n【之後新增的對話】\n"
        else:
            head = f"【先前摘要】\n{prev}\n\n【之後新增的對話】\n" if prev else ""
        segs = _segments(items, _parallel())

        async def finish(system: str, text: str) -> str:
            out = await _call(model, system, text)
            if not edit:
                return out
            done = _apply_edits(prev, out)
            if done is not None:
                return done
            await emit({"type": "notice", "session_id": session_id, "text": "摘要的編輯指令看不懂，這次改成整份重寫"})
            return (await _summarize(model, prev, items, target, emit, session_id, edit=False))[0]

        if len(segs) == 1:
            sys_ = f"{base}\n{_edit_rules(prev, target)}" if edit else f"{base}{upd}{size}"
            return await finish(sys_, head + _render(segs[0])), 1
        n = len(segs)
        per = max(200, target // n)
        await emit({"type": "notice", "session_id": session_id,
                    "text": f"字數較多，分成 {n} 段同時摘要（每段約 {per} 字）…"})
        parts = await asyncio.gather(*[
            _call(model, f"{base}\n{_prompt_in('compact_part', mode, i=i + 1, k=n)}\n本段約 {per} 字以內。", _render(seg))
            for i, seg in enumerate(segs)
        ])
        await emit({"type": "notice", "session_id": session_id, "text": f"{n} 段都摘要完成，正在合併…"})
        merged = "\n\n".join(f"【第 {i + 1} 段摘要】\n{p}" for i, p in enumerate(parts))
        if edit:
            return await finish(f"{_prompt_in('compact_merge', mode)}\n{_edit_rules(prev, target)}", head + merged), n
        tail = f"\n{_STRUCT}" if mode == "常規" else ""
        final = await _call(model, f"{_prompt_in('compact_merge', mode)}{tail}{upd}{size}", head + merged)
        return final, n

    def _marker(m: dict) -> str:
        """保留區第一則長什麼樣（給模型認得摘要範圍到哪）。"""
        blocks = m.get("content") or []
        text = " ".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text").strip().replace("\n", " ")
        if m["role"] == "user":
            return f"使用者說「{text[:60]}」那則" if text else "使用者傳來的那則"
        if m["role"] == "assistant":
            calls = [b.get("name") for b in blocks if b.get("type") == "tool_call"]
            if text:
                return f"你回覆「{text[:60]}」那則"
            return f"你呼叫 {'、'.join(calls[:3])} 那一步" if calls else "你回覆的那則"
        names = [b.get("name") for b in blocks if b.get("type") == "tool_result"]
        return f"工具 {'、'.join(n for n in names[:3] if n)} 回傳結果那則"

    async def _summarize_cached(ctx, model: dict, prev: str, kept: list[dict], target: int) -> str:
        """沿用主對話的快取寫摘要：跟上一步送出的請求一字不差（系統提示詞、工具清單、思考強度、歷史），
        之後新增的訊息接在後面，最後加一則壓縮指令。前面整段吃快取，只多付指令和摘要的錢。
        條件不合（沒有上一步的紀錄、快取可能過期、指定了別的壓縮模型、太長、狀態已經變了）回空字串，改用一般做法。"""
        from woof.agent import loop
        from woof import tokens

        sid = ctx.session_id
        snap = loop.LAST_REQ.get(sid)
        if not snap or not kept or snap["model"] != model.get("id"):
            return ""
        use = loop.aux_model("compact", model) or model
        if use.get("id") != model.get("id"):
            return ""      # 指定了別的壓縮模型：快取各模型分開，沿用不到
        ttl = CACHE_TTL_ANTHROPIC if model.get("provider") == "anthropic" else CACHE_TTL_OTHER
        if time.time() - snap["at"] > ttl:
            return ""      # 快取大概過期了：整段原價重送，比一般做法（節錄）還貴
        if getattr(ctx, "mid_turn", False) and getattr(ctx, "force_compact", False):
            return ""      # 已經超出上下文，整段送不出去
        if snap.get("sig") != loop.req_sig(sid):
            return ""
        new = [m for m in loop.history(sid) if int(m.get("id") or 0) > int(snap["through"] or 0)]
        new = loop._drop_hollow_replies(loop._drop_missing_images(new))
        mode = _mode()
        rules = _prompt_in("compact", mode)
        if prev:
            # 編輯：舊摘要附行號放在指令裡，模型只輸出要改的行（呼叫端套用）；前面整段照樣吃快取
            rules += ("\n" + _edit_rules(prev, target)
                      + "（【之後新增的對話】＝最前面那則【上下文摘要】後面到範圍結束為止的對話）"
                      + f"\n\n【先前摘要】（每行前面是行號，指令裡的行號照這個）\n{_numbered(prev)}")
        else:
            rules += f"\n長度約 {target} 字以內；路徑、數字、名稱、決定、未完成事項寧可多留，不要為了縮短漏掉。"
        head = "最前面的【上下文摘要】加上它後面的對話" if prev else "對話開頭"
        scope = (f"{head}，到{_marker(kept[0])}之前為止。從那則開始的最近 {len(kept)} 則訊息壓縮後會原樣保留，"
                 "不用寫進摘要（需要時可以參考，判斷哪些事已經做完）。")
        # 標成內部訊息：只回傳「這一回合」思考的連線，回合起點不會被這則指令往後推（推了前面的思考就會被拿掉、前綴斷掉）
        ask = {"role": "user", "meta": {"internal": "compact_ask"},
               "content": [{"type": "text", "text": _prompt_get("compact_inline", scope=scope, rules=rules)}]}
        msgs = [*snap["messages"], *new, ask]
        window = tokens.window_of(model, sid)
        est = (tokens.estimate_messages(msgs) + tokens.estimate_text(snap["system"])
               + tokens.estimate_tools(snap["specs"]) + int(target / CHARS_PER_TOK))
        if window and est > window * 0.95:
            return ""
        return (await loop.complete(model, snap["system"], msgs, tools=snap["specs"], effort=snap["effort"],
                                    session_id=sid, kind="compact")).strip()

    def _wait_at() -> float:
        return max(0.1, min(0.99, float(woof.settings.get("wait_at", 90) or 90) / 100.0))

    def _used_ratio(sid: str, model: dict) -> float:
        from woof import tokens

        window = tokens.window_of(model, sid)
        return tokens.current(sid)["used"] / window if window else 0.0

    def _start_bg(ctx) -> None:
        """在背景壓縮；同一個對話已經有一個在跑就不再開"""
        sid = ctx.session_id
        t = _bg.get(sid)
        if (t and not t.done()) or _fails.get(sid, 0) >= AUTO_FAIL_MAX:
            return
        _bg[sid] = asyncio.create_task(_precompact(sid, ctx.model_cfg, ctx.registry, ctx.emit,
                                                   mid=bool(getattr(ctx, "mid_turn", False))))

    async def _precompact(session_id: str, model: dict, registry, emit, mid: bool = False) -> None:
        job = SimpleNamespace(session_id=session_id, model_cfg=model, registry=registry, emit=emit, mid_turn=mid,
                              force_compact=False, is_bg=True, threshold=_trigger(), compact_reason="", compact_count=0)
        try:
            await prepare_turn(job)
        except Exception as e:
            try:
                await emit({"type": "notice", "session_id": session_id, "text": f"背景壓縮失敗：{e}"})
            except Exception:
                pass

    async def after_turn(ctx):
        from woof import tokens

        window = tokens.window_of(ctx.model_cfg, ctx.session_id)
        if not window:
            return

        used = tokens.current(ctx.session_id)["used"]
        if used / window < _trigger():
            return
        _start_bg(ctx)

    # 同一個對話一次只壓一個：手動、回合中、背景共用一把鎖。
    # 鎖用 async with，成功、失敗、被叫停都會放掉；手動和背景另有時間上限，卡住也不會永遠鎖死
    _locks: dict[str, asyncio.Lock] = {}
    _last_done: dict[str, float] = {}
    GUARD_TIMEOUT = 1800
    RECENT_SEC = 10

    async def _compact_state(ctx, on: bool):
        from woof.agent.loop import COMPACTING

        if on:
            COMPACTING[ctx.session_id] = time.time()
        elif COMPACTING.pop(ctx.session_id, None) is None:
            return
        try:
            await ctx.emit({"type": "compact_state", "session_id": ctx.session_id, "compacting": on})
        except Exception:
            pass

    async def prepare_turn(ctx):
        from woof.agent.loop import COMPACTING

        sid = ctx.session_id
        pending_bg = _bg.get(sid)
        running = bool(pending_bg and not pending_bg.done())
        force = bool(getattr(ctx, "force_compact", False))
        manual = bool(getattr(ctx, "manual", False))
        if not force and not manual and not getattr(ctx, "is_bg", False):
            # 回合開始、回合中每一步：超過門檻就交給背景寫摘要，這一步照常送（不等）；
            # 背景寫好後，下一次送出自動換成「摘要＋最近幾則」。用量到等待線還沒好，才停下來等
            r = _used_ratio(sid, ctx.model_cfg)
            if r < _trigger() or _fails.get(sid, 0) >= AUTO_FAIL_MAX:
                return None
            if r < _wait_at():
                _start_bg(ctx)
                return None
            if running:
                await pending_bg
                if _used_ratio(sid, ctx.model_cfg) < _wait_at():
                    return True
        elif force and not manual and running:
            await pending_bg   # 上下文已經爆了，背景那份正在寫：等它好就用，不重複壓縮
            return True
        if manual and sid in COMPACTING:
            ctx.compact_reason = "這個對話正在壓縮，等這次做完；不會重複壓縮"
            return
        lk = _locks.setdefault(sid, asyncio.Lock())
        async with lk:
            if manual and time.time() - _last_done.get(sid, 0) < RECENT_SEC:
                ctx.compact_reason = "剛壓縮完，不用再壓一次"
                return
            try:
                if manual or getattr(ctx, "is_bg", False):
                    try:
                        done = await asyncio.wait_for(_compact(ctx), GUARD_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise RuntimeError(f"壓縮超過 {GUARD_TIMEOUT // 60} 分鐘沒做完，這次放棄；可以再壓一次") from None
                else:
                    done = await _compact(ctx)
                if done:
                    _last_done[sid] = time.time()
                    _fails.pop(sid, None)
                return done
            except Exception as e:
                if manual:
                    ctx.compact_reason = f"壓縮失敗：{str(e)[:300]}"
                else:
                    n = _fails[sid] = _fails.get(sid, 0) + 1
                    if n == AUTO_FAIL_MAX:
                        try:
                            await ctx.emit({"type": "notice", "session_id": sid,
                                            "text": f"自動壓縮連續失敗 {n} 次，先停止自動壓縮；按「立即壓縮」可以再試，成功後恢復"})
                        except Exception:
                            pass
                raise
            finally:
                await _compact_state(ctx, False)

    async def _compact(ctx):
        from woof import store
        from woof.agent.loop import aux_model

        from woof import tokens

        model = ctx.model_cfg
        window = tokens.window_of(model, ctx.session_id)
        force = bool(getattr(ctx, "force_compact", False))
        can = model.get("provider") in ctx.registry.providers or aux_model("compact", None)
        if not can:
            if force:
                ctx.compact_reason = "這個模型不能寫摘要，請在「設定 → 輔助模型 → 壓縮」指定一個 API 型模型"
            return
        if not force and not window:
            return

        st = store.get_session(ctx.session_id) or {}
        # 上次模型實際回報的用量＋之後新增訊息的估計（剛貼進來的長文也算得到）
        used = tokens.current(ctx.session_id)["used"]
        trigger = getattr(ctx, "threshold", None) or _trigger()
        if not force and (not used or used / window < trigger):
            return

        mid = bool(getattr(ctx, "mid_turn", False))
        msgs = store.get_messages(ctx.session_id)
        es = dict(st.get("engine_state") or {})
        summary = tokens.summary_of(es)  # 讀取時的摘要，寫回前會重新讀取
        through = int(summary.get("through") or 0)
        pending = [m for m in msgs if m["id"] > through]
        starts = [i for i, m in enumerate(pending)
                  if m["role"] == "user" and not (m.get("meta") or {}).get("internal")]
        req = starts[-1] if starts else None
        # 估計值照這個模型的實際算法校正（上次實際回報 ÷ 同一段的估計）
        k = tokens.calib(ctx.session_id)
        # 原樣保留：從最新往舊收，收到設定的 token 量為止（最多上下文的 25%）。之後不送的思考不算；
        # 開頭是工具結果就往後挪，不拆開呼叫和結果；至少留最後一步
        budget = _tail_tok() / k
        if window:
            budget = min(budget, window * TAIL_WIN / k)

        def est(m):
            return tokens.estimate_blocks([b for b in m.get("content") or []
                                           if b.get("type") not in ("thinking", "redacted_thinking")])
        cut, acc = len(pending), 0
        while cut > 0:
            t = est(pending[cut - 1])
            if acc + t > budget:
                break
            acc += t
            cut -= 1
        while cut < len(pending) and pending[cut]["role"] == "tool":
            cut += 1
        last_step = max((i for i, m in enumerate(pending) if m["role"] != "tool"), default=None)
        if last_step is not None:
            cut = min(cut, last_step)
        if cut <= 0:
            ctx.compact_reason = f"還沒壓縮過的部分都在原樣保留的量（約 {_tail_tok():,} token）以內，不需要壓縮"
            return
        # 回合進行中、使用者這次的要求被切進摘要：在摘要後面原文重述（before_request），模型才知道還在做什麼
        inflight = 0
        if mid and (req is None or cut > req):
            inflight = pending[req]["id"] if req is not None else int(summary.get("inflight") or 0)
        older, recent_ids = pending[:cut], {m["id"] for m in pending[cut:]}

        # 先試只清舊工具輸出：較早、已經用過的工具結果換成一行佔位，夠降到門檻以下就不花模型呼叫寫摘要。
        # 只在本來就要壓縮時做，平常不動，快取照舊。
        # Claude 不做：它的每段思考都綁著前面的內容，改了較早的工具輸出，後面所有思考就失效（照收費、模型讀到過期的推理）
        if not force and model.get("provider") != "anthropic":
            done_through = int((es.get("prune") or {}).get("through") or 0)
            saved, last_id, n_res = 0, 0, 0
            for m in older:
                if m["role"] != "tool" or m["id"] <= done_through:
                    continue
                for b in m["content"] or []:
                    if b.get("type") == "tool_result":
                        before = tokens.estimate_text(b.get("output") or "")
                        after = tokens.estimate_text(tokens.pruned_result(b)["output"])
                        if before > after:
                            saved += before - after
                            n_res += 1
                last_id = m["id"]
            if last_id and n_res and used - saved < window * trigger * 0.8:
                cur = dict((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
                cur["prune"] = {"through": last_id, "at": time.time(), "count": n_res, "saved": saved}
                from woof.agent.loop import mark_rewrite, retire_images_on_compact

                mark_rewrite(cur, pending[-1]["id"])

                retire_images_on_compact(ctx.session_id, cur, _keep_images())   # 前綴反正重算，舊圖一起收
                s2 = store.update_session(ctx.session_id, engine_state=cur)
                await ctx.emit({"type": "session_updated", "session": s2})
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"上下文已用 {used / window:.0%}：先清掉 {n_res} 個較早的工具輸出（約省 {saved:,} token），"
                                        "這次不用寫摘要；全文仍在資料庫"})
                u = tokens.reestimate(ctx.session_id, window or None)
                await ctx.emit({"type": "usage", "session_id": ctx.session_id, **u})
                return True

        # 一般做法的節錄：先用最寬的頭尾長度，總長超過摘要模型讀得下的量（多工時乘段數）再一級一級縮
        use = aux_model("compact", model) or model
        budget = int((int(use.get("context_window") or 0) or 128000) * 0.6 * CHARS_PER_TOK) * _parallel()
        for out_cap, arg_cap in CLIP_LEVELS:
            items = []
            for m in older:
                body = _text_of(m["content"], out_cap, arg_cap)
                if body:
                    items.append((m["role"], _elide(body, MSG_CAP)))
            if sum(len(b) for _, b in items) <= budget:
                break
        if not items:
            ctx.compact_reason = "較早的訊息都是空的，沒有可以摘要的內容"
            return
        prev = summary.get("text") or ""
        # 第一次：長度照「被摘要的內容有多少 token」乘摘要比例（工具輸出全文也算）。更新：以上限為準，
        # 沒變的行原樣保留，超過上限時模型順便刪掉過時、重複的行
        src_tok = tokens.estimate_messages(older) * k
        cap_tok = min(_sum_tok(), int(window * SUM_WIN)) if window else _sum_tok()
        want = max(SUM_MIN_TOK, min(cap_tok, int(src_tok * _ratio())))
        target = int((cap_tok if prev else want) * CHARS_PER_TOK)
        mode = _mode()
        method = _method()

        why = (getattr(ctx, "compact_reason", "") or "手動壓縮") if force else f"上下文已用 {used / window:.0%}"
        if mid:
            why = "回合進行中，" + why
        if getattr(ctx, "is_bg", False):
            why += "，在背景寫摘要（對話照常進行，寫好後下一次送出開始用）"
        ctx.compact_count = len(older)
        await _compact_state(ctx, True)
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": f"{why}，正在壓縮較早的 {len(older)} 則訊息（{method}・{mode}模式，目標約 {target} 字）…"})
        text, parts, how = "", 1, ""
        if method == "單次":
            try:
                text = await _summarize_cached(ctx, model, prev, pending[cut:], target)
                how = "cached" if text else ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 沿用快取那次失敗（模型去呼叫工具、服務商出錯）：改用一般做法再寫一次
                text = ""
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"沿用快取寫摘要沒成功（{str(e)[:120]}），改用一般做法"})
        if text and prev:
            edited = _apply_edits(prev, text)
            if edited is None:
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": "摘要的編輯指令看不懂，這次改成整份重寫"})
                text, parts = await _summarize(model, prev, items, target, ctx.emit, ctx.session_id, edit=False)
                how = ""
            else:
                text = edited
        if not text:
            text, parts = await _summarize(model, prev, items, target, ctx.emit, ctx.session_id)
        src = sum(len(b) for _, b in items)

        def not_summary(t: str) -> str:
            t = t.strip()
            if not t:
                return "回了空白"
            if tokens.fake_call(t):
                return "寫成了工具呼叫"
            if not prev and src > FIRST_SRC and len(t) < FIRST_MIN:
                return f"只寫了 {len(t)} 字"
            return ""
        bad = not_summary(text)
        if bad:
            await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                            "text": f"摘要模型沒寫出摘要（{bad}），重寫一次"})
            text, parts = await _summarize(model, prev, items, target, ctx.emit, ctx.session_id, edit=False)
            how = ""
            bad = not_summary(text)
            if bad:
                raise RuntimeError(f"摘要模型兩次都沒寫出摘要（{bad}），這次不壓縮；原文都還在")
        text = text.strip()
        cur = dict((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
        cur["compress_count"] = int(cur.get("compress_count") or 0) + 1
        # 「壓縮時一起關／壓縮時正式加入」的工具在這裡換：壓縮後本來就整段重算，工具清單一起換不多花
        drop = set(cur.get("tools_drop") or [])
        add = [n for n in (cur.get("tools_add") or []) if n not in drop]
        if drop or add:
            from woof.agent.loop import disabled_tools

            on = list(cur.get("tools_on") or [])
            off = list(cur.get("tools_off") or [])
            for n in drop:
                if n in on:
                    on.remove(n)  # 這個對話開的 → 拿掉
                elif n not in off:
                    off.append(n)  # 全域開著的 → 這個對話關
            for n in add:
                if n in off:
                    off.remove(n)  # 這個對話關過的 → 打開
                if n in disabled_tools() and n not in on:
                    on.append(n)   # 全域停用的 → 這個對話開
            cur["tools_on"], cur["tools_off"], cur["tools_drop"], cur["tools_add"] = on, off, [], []
            if add:   # 已經正式加入了：之後同一個工具再「先用 tool_call」加一次，要再告訴模型
                cur["told"] = {k: v for k, v in (cur.get("told") or {}).items() if k != "tools_add"}
        # 附的原話在這裡算好存起來：之後每次送出都一樣，不重算、快取不斷
        quotes = _users_section(msgs, older[-1]["id"], _quote_n(), _quote_chars(), _reply_head())
        cur["summary"] = {"through": older[-1]["id"], "text": text, "quotes": quotes, "at": time.time(),
                          "kept": sorted(recent_ids), "mode": mode, "method": method, "parts": parts,
                          "inflight": inflight, **({"how": how} if how else {})}
        cur["tok_k"] = round(k, 3)
        from woof.agent.loop import mark_rewrite, retire_images_on_compact

        retire_images_on_compact(ctx.session_id, cur, _keep_images())   # 前綴反正重算，舊圖一起收，只留最新幾張
        mark_rewrite(cur, pending[-1]["id"])   # 原樣保留的那幾則，前面已經換成摘要：它們的思考（Claude）之後不送
        s2 = store.update_session(ctx.session_id, engine_state=cur)
        # 對話畫面據此在被摘要的訊息後面畫一條分隔線（可展開看摘要）
        await ctx.emit({"type": "session_updated", "session": s2})
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": (f"已把這一回合較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {len(recent_ids)} 則保持原樣，"
                                 "使用者這次的要求會在摘要後面重述，模型接著做") if inflight else
                                f"已把較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {len(recent_ids)} 則保持原樣"})
        # 舊的用量回報不算數了：先換成估計值，下一次模型回報再更新（避免剛壓縮完又被判定要壓縮）
        u = tokens.reestimate(ctx.session_id, window or None)
        await ctx.emit({"type": "usage", "session_id": ctx.session_id, **u})
        return True

    def before_request(messages, ctx):
        from woof import store

        from woof import tokens

        st = store.get_session(ctx.session_id) or {}
        summary = tokens.summary_of(st.get("engine_state") or {})
        if not summary or not summary.get("text"):
            if (st.get("engine_state") or {}).get("prune"):
                return tokens.apply_prune(messages, st.get("engine_state"))
            return None
        through = int(summary.get("through") or 0)
        kept = [m for m in messages if int(m.get("id") or 0) > through]
        # history() 目前不帶 id，退回用序號：訊息是按 id 排序的
        if not any("id" in m for m in messages):
            return None
        kept = tokens.apply_prune(kept, st.get("engine_state"))
        body = _prompt_get("compact_head", summary=summary["text"])
        # 使用者的原話照抄在摘要後面（不經摘要模型），跟摘要有出入以原話為準；同一份摘要每次都一樣，快取不斷
        users = summary["quotes"] if "quotes" in summary else _users_section(messages, through, _quote_n(), _quote_chars(), _reply_head())
        if users:
            body += _prompt_get("compact_users", users=users)
        if "session_search" in getattr(getattr(ctx, "registry", None), "tools", {}):
            body += _prompt_get("compact_recall", sid=ctx.session_id)
        # 回合中途壓縮時，使用者這一回合的訊息被摘要進去了：在摘要後面原文重述，模型才知道還在做什麼。
        # 一回合＝最後一則不是插話的使用者訊息起，加上之後的插話；全部依時間貼出，不只最後一則（插話常是補充，單貼會被當成整個任務）
        inf = int(summary.get("inflight") or 0)
        newer_user = any(m["role"] == "user" and not (m.get("meta") or {}).get("internal") for m in kept)
        if inf and inf <= through and not newer_user:
            req_text = _turn_requests(messages, inf)
            if req_text:
                body += _prompt_get("compact_inflight", request=req_text)
        body += _prompt_get("compact_end")
        head = {"role": "user", "content": [{"type": "text", "text": body}]}
        return [head, *kept]

    async def force_compact(ctx):
        ctx.force_compact = True
        ctx.manual = True
        return bool(await prepare_turn(ctx))

    async def mid_turn(ctx):
        if str(woof.settings.get("mid_turn", True)).lower() in ("false", "0", "off"):
            return None
        # 斷路器：這一回合壓縮連續失敗 2 次（例如摘要模型一直 429）就不再每步重試，等上下文真的爆了（force）才再試
        fails = getattr(ctx, "compact_fails", 0)
        if fails >= 2 and not getattr(ctx, "force_compact", False):
            return None
        try:
            done = bool(await prepare_turn(ctx))
        except Exception as e:
            ctx.compact_fails = fails + 1
            if ctx.compact_fails == 2:
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"壓縮連續失敗兩次，這一回合先不再自動壓縮（{str(e)[:160]}）"})
            raise
        ctx.compact_fails = 0
        return done

    woof.on("prepare_turn", prepare_turn, order=20)
    woof.on("after_turn", after_turn, order=20)
    woof.on("force_compact", force_compact, order=20)
    woof.on("mid_turn", mid_turn, order=20)
    woof.on("before_request", before_request, order=20)
