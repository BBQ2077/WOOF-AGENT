"""上下文壓縮。

用量超過模型上下文窗口的設定比例時，把較早的訊息摘要成一段，
之後的回合只送「摘要 + 最近幾則」。原始訊息留在資料庫，不刪除。

摘要存在對話的 engine_state.summary：
  {"through": 最後被摘要的訊息 id, "text": 摘要, "at": 時間, "kept": [...], "quotes": 附的原話, "inflight": ..., "cache": [輸入, 快取讀取]}

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
  mid_turn  回合進行中也檢查用量，超過就先壓縮再送；上下文爆掉時壓縮後重送

寫摘要只有一種做法：送出「下一步本來就要送的內容」（系統提示詞、工具清單、思考強度、歷史跟主對話一字不差），
最後加一則壓縮指令，前面整段吃主對話的快取。第一次寫整份摘要，之後只輸出要改的行。
"""
from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

_bg: dict[str, asyncio.Task] = {}
# 自動壓縮（背景、回合中）失敗一次就先停：不然每一步都再試一次，每次都要付寫摘要的錢。
# 手動「立即壓縮」不受限，成功一次就恢復
_fails: dict[str, int] = {}
AUTO_FAIL_MAX = 1

# 摘要長度（token）：第一次照「被摘要內容 × 摘要比例」，至少 1500；不超過設定的上限（summary_tokens）和上下文的 10%
SUM_MIN_TOK, SUM_TOK_DEFAULT, SUM_WIN = 1500, 8000, 0.10
CHARS_PER_TOK = 1.5
# 原樣保留的最近訊息（實際 token，設定 tail_tokens）；最多佔上下文的 25%，小模型才不會整段都是保留區
TAIL_TOK_DEFAULT, TAIL_WIN = 10000, 0.25
# 摘要後面附上被壓縮那段裡使用者最近幾則原話（新的在前）：則數、合計 token（設定 quote_count、quote_tokens）、單則上限（字）
QUOTE_N_DEFAULT, QUOTE_TOK_DEFAULT, USER_MAX = 3, 1500, 1500
REPLY_HEAD_DEFAULT = 250   # 每則原話前面附上它回應的助手那則的開頭（先講結論，開頭就是重點），看得出回的是什麼
# 快取大概還在的時間（Claude 放 5 分鐘；其他家多半更久）：超過就先讓這一步照常送（順便把快取建回來），下一步再壓縮
CACHE_TTL_ANTHROPIC, CACHE_TTL_OTHER = 270, 600
# 被摘要的部分超過這麼多 token，寫出來的摘要（編輯後的整份）卻不到 MIN_CHARS 字：沒在寫摘要
SHORT_SRC_TOK, MIN_CHARS = 10000, 200
FIT = 0.95           # 請求估計超過模型窗口的這個比例：從最舊的訊息往後拿掉，直到送得出去
# 摘要用的提示詞在 woof/prompts.py（compact、compact_inline、compact_edit…），設定頁可以檢視、覆寫
from woof.prompts import get as _prompt_get, get_in as _prompt_in  # noqa: E402


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
_OP = re.compile(r"^\s*@@\s*(改|刪|插|不變|完)\s*(?:第\s*)?(\d+)?\s*行?\s*(?:[-–~～至到]\s*(?:第\s*)?(\d+)\s*行?)?\s*(?:之?後)?\s*$")
_NUM = re.compile(r"^\d{1,4}\| ?")


def _numbered(text: str) -> str:
    return "\n".join(f"{i}| {ln}" for i, ln in enumerate(text.split("\n"), 1))


def _apply_edits(prev: str, out: str) -> tuple[str | None, str]:
    """把行號編輯指令套到舊摘要上，回傳 (新摘要, 不能用的原因)。全部指令都對才算數，有一條不對整份不用（不存半套的摘要）。
    @@ 改 a-b ＋新內容：換掉第 a 到 b 行；@@ 刪 a-b；@@ 插 n ＋新內容：插在第 n 行後面（0＝最前面，超過最後一行＝接在最後）；
    @@ 不變；最後一行 @@ 完，後面寫的都不算。第一個指令前面的說明、``` 、行號前綴都略過。
    沒寫指令、但寫出了整份摘要（段落標題齊、長度跟舊的差不多）：直接當新摘要用，不用再寫一次。"""
    lines = prev.split("\n")
    total = len(lines)
    ops, cur, ended, fence = [], None, False, False
    for ln in out.strip("\n").split("\n"):
        if ln.strip().startswith("```"):
            fence = not fence
            if ops and not fence:
                cur = None     # 指令寫完收尾的 ```：後面的說明不算進最後一個指令
            continue
        m = _OP.match(ln)
        if m and m.group(1) == "完":
            ended = True
            break
        if m:
            cur = [m.group(1), int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None, []]
            ops.append(cur)
        elif cur is not None:
            cur[3].append(_NUM.sub("", ln) if _NUM.match(ln) else ln)
    if not ops:
        body = out.strip()
        heads = [ln for ln in body.split("\n") if ln.startswith("## ")]
        if not ended and len(heads) >= 3 and len(body) >= len(prev) * 0.6:
            return body, ""
        return None, "沒有寫出 @@ 編輯指令"
    if any(o[0] == "不變" for o in ops):
        if len(ops) > 1:
            return None, "寫了 @@ 不變 又寫了其他指令"
        return prev, ""
    if not ended and ops[-1][3] and not ops[-1][3][-1].strip().startswith(("-", "#", "*", "|")) \
            and not re.match(r"^\s*\d+[.、)]", ops[-1][3][-1]):
        # 沒寫 @@ 完：最後一條的最後一行看起來不像摘要內容（不是條列、標題），多半是收尾的說明
        return None, f"最後沒有寫 @@ 完（最後一行「{ops[-1][3][-1].strip()[:30]}」分不出是內容還是說明）"
    repl, ins, taken, bad = {}, {}, set(), []
    for op, a, b, new in ops:
        while new and not new[-1].strip():
            new.pop()
        while new and not new[0].strip():
            new.pop(0)
        label = f"@@ {op} {a if a is not None else ''}{f'-{b}' if b is not None else ''}".rstrip()
        if a is None:
            bad.append(f"{label}：沒寫行號")
            continue
        if op == "插":
            if not new:
                bad.append(f"{label}：下面沒有內容")
                continue
            ins.setdefault(min(a, total), []).extend(new)
            continue
        if op == "刪" and new:
            bad.append(f"{label}：刪的下面又寫了內容（要換掉請用 @@ 改）")
            continue
        if op == "改" and not new:
            bad.append(f"{label}：下面沒有內容（要刪掉請用 @@ 刪）")
            continue
        b = b if b is not None else a
        if not 1 <= a <= b <= total:
            bad.append(f"{label}：先前摘要只有 1 到 {total} 行")
            continue
        if taken & set(range(a, b + 1)):
            bad.append(f"{label}：範圍跟別的指令重疊")
            continue
        taken |= set(range(a, b + 1))
        repl[a] = (b, new)
    if bad:
        return None, "編輯指令有錯：" + "；".join(bad[:6])
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
        return None, f"一次刪掉大半（{len(prev)} 字剩 {len(text)} 字），多半是看錯行號"
    return text, ""


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


def register(woof):
    def _mode() -> str:
        return "常規"  # 摘要固定用結構化版本；提示詞模式只屬於「提示詞與記憶」

    def _ratio() -> float:
        return max(5, min(int(woof.settings.get("ratio") or 20), 60)) / 100

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

    def _ttl(model: dict) -> int:
        return CACHE_TTL_ANTHROPIC if model.get("provider") == "anthropic" else CACHE_TTL_OTHER

    def _warm(sid: str, model: dict) -> bool:
        """上一步剛送過（快取大概還在）"""
        from woof.agent import loop

        snap = loop.LAST_REQ.get(sid)
        return bool(snap) and time.time() - snap["at"] <= _ttl(model)

    async def _request(ctx, model: dict, prev: str, kept: list[dict], target: int, fit: float = FIT) -> dict:
        """寫摘要的請求：下一步本來就要送的內容（跟主對話一字不差），最後加一則壓縮指令。
        記憶體裡有上一步就接著用；WOOF 重開過或對話狀態變了，照現在的狀態重組（同一套流程）。
        超過模型窗口（上下文已經爆了）就從最舊的訊息往後拿掉，直到送得出去。"""
        from woof.agent import loop
        from woof import tokens

        sid = ctx.session_id
        use = loop.aux_model("compact", model) or model
        snap = loop.LAST_REQ.get(sid)
        note = ""
        if snap and snap["model"] == model.get("id") and snap.get("sig") == loop.req_sig(sid):
            new = [m for m in loop.history(sid) if int(m.get("id") or 0) > int(snap["through"] or 0)]
            new = loop._drop_hollow_replies(loop._drop_missing_images(new))
            system, msgs, specs, effort = snap["system"], [*snap["messages"], *new], snap["specs"], snap["effort"]
            age = time.time() - snap["at"]
            if age > _ttl(model):
                note = f"上一步在 {int(age // 60)} 分鐘前，快取大概過期了，這次整段照原價算"
        else:
            r = await loop.rebuild_request(sid, model, ctx)
            if not r:
                raise RuntimeError("這個對話還沒有可以送出的內容")
            system, msgs, specs, effort = r["system"], list(r["messages"]), r["specs"], r["effort"]
            note = "上一步的請求不在記憶體裡（WOOF 重開過或對話狀態變了），照現在的狀態重組；快取不一定還在"
        if use.get("id") != model.get("id"):
            effort = use.get("default_effort")
            note = f"「設定 → 輔助模型 → 壓縮」指定了 {use.get('label', use['id'])}，跟主對話不同模型，吃不到主對話的快取"
        mode = _mode()
        rules = _prompt_in("compact", mode)
        if prev:
            # 編輯：舊摘要附行號放在指令裡，模型只輸出要改的行（程式套用）
            n_lines = len(prev.split("\n"))
            rules += ("\n" + _prompt_in("compact_edit", mode, n=len(prev), cap=target, last=n_lines)
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
        # 上限看模型本身的窗口（壓縮設定的「上下文大小」是什麼時候壓，不是送得出去的上限）；估計值乘上實測校正
        window = int(use.get("context_window") or 0) or tokens.window_of(use, sid)
        k = tokens.calib(sid)
        fixed = (tokens.estimate_text(system) + tokens.estimate_tools(specs) + tokens.estimate_messages([ask])) * k \
            + target / CHARS_PER_TOK
        sizes = [tokens.estimate_messages([m]) * k for m in msgs]
        total = fixed + sum(sizes)
        dropped = 0
        if window and total > window * fit:
            first_kept = int(kept[0]["id"])
            start = 1 if msgs and "id" not in msgs[0] else 0     # 最前面的【上下文摘要】留著
            while total > window * fit:
                if start >= len(msgs) or int(msgs[start].get("id") or 0) >= first_kept:
                    raise RuntimeError(f"最近要保留的訊息加起來就超過 {use.get('label', use['id'])} 的上限，送不出去")
                msgs.pop(start)
                total -= sizes.pop(start)
                dropped += 1
            # 開頭不能是助手或工具結果（呼叫和結果不能拆開）：拿到下一則使用者訊息為止
            while start < len(msgs) and msgs[start]["role"] != "user" and int(msgs[start].get("id") or 0) < first_kept:
                msgs.pop(start)
                sizes.pop(start)
                dropped += 1
            note = (note + "；" if note else "") + f"上下文超過模型上限，最舊的 {dropped} 則這次沒送（摘要裡不會有它們的細節，原文還在資料庫）"
        return {"use": use, "system": system, "messages": [*msgs, ask], "specs": specs, "effort": effort,
                "note": note, "dropped": dropped}

    def _check(out: str | None, prev: str, src_tok: float) -> tuple[str, str]:
        """模型的回覆能不能當摘要：回傳 (摘要全文, 不能用的原因)。每次壓縮都檢查"""
        from woof import tokens

        if out is None:
            return "", "去呼叫了工具"
        out = out.strip()
        if not out:
            return "", "回了空白"
        if tokens.fake_call(out):
            return "", "寫成了工具呼叫"
        text = out
        if prev:
            text, why = _apply_edits(prev, out)
            if text is None:
                return "", why
        if src_tok > SHORT_SRC_TOK and len(text) < MIN_CHARS:
            return "", f"只寫了 {len(text)} 字"
        return text, ""

    async def _write(ctx, model: dict, prev: str, kept: list[dict], target: int, src_tok: float, why: str):
        """寫摘要，最多送兩次：第一次不能用，同一份請求在指令後面註明哪裡不對再寫一次（前面照樣吃快取）。
        回傳 (摘要全文, [輸入, 快取讀取])；兩次都不能用就丟錯，不存（原文照送）"""
        from woof.agent import loop

        sid = ctx.session_id
        req = await _request(ctx, model, prev, kept, target)
        await ctx.emit({"type": "notice", "session_id": sid,
                        "text": f"{why}，正在壓縮較早的 {ctx.compact_count} 則訊息"
                                f"（{'只寫要改的行' if prev else f'目標約 {target} 字'}）…" + (f"（{req['note']}）" if req["note"] else "")})
        problem, stat = "", [0, 0]
        for attempt in range(2):
            msgs = req["messages"]
            if problem and problem != "超出模型上限":
                ask = msgs[-1]
                again = ask["content"][0]["text"] + _prompt_get("compact_retry", problem=problem,
                                                                 what="整套 @@ 編輯指令（全部重寫一次，行號照【先前摘要】，最後一行 @@ 完）"
                                                                 if prev else "摘要正文")
                msgs = [*msgs[:-1], {**ask, "content": [{"type": "text", "text": again}]}]
                await ctx.emit({"type": "notice", "session_id": sid,
                                "text": f"摘要模型這次{problem}，用同一份內容再寫一次（前面照樣吃快取）"})
            try:
                out = await loop.complete(req["use"], req["system"], msgs, tools=req["specs"], effort=req["effort"],
                                          session_id=sid, kind="compact")
            except loop.ToolCallInstead:
                out = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 0 and loop.is_overflow_error(e):
                    # 估計比實際少：再多拿掉一些舊訊息重送
                    req = await _request(ctx, model, prev, kept, target, fit=FIT * 0.8)
                    problem = "超出模型上限"
                    await ctx.emit({"type": "notice", "session_id": sid,
                                    "text": "摘要請求超出模型上限，多拿掉一些最舊的訊息再送一次"
                                            + (f"（{req['note']}）" if req["note"] else "")})
                    continue
                raise RuntimeError(f"壓縮摘要失敗（{req['use'].get('label', req['use']['id'])}）：{str(e)[:300]}") from e
            u = loop.AUX_USAGE.pop(sid, None) or {}
            stat[0] += int(u.get("input_tokens") or 0)
            stat[1] += int(u.get("cache_read") or 0)
            text, problem = _check(out, prev, src_tok)
            if not problem:
                return text, stat
        raise RuntimeError(f"摘要模型兩次都沒寫出能用的摘要（{problem}），這次不壓縮；原文都還在")

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
        except Exception:
            pass   # 失敗的提示在 prepare_turn 裡送

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
                if _warm(sid, ctx.model_cfg):
                    _start_bg(ctx)
                # 快取大概過期了（隔了一陣子才傳訊息、WOOF 重開過）：這一步先照常送，順便把快取建回來；
                # 這一步結束後（after_turn）或下一步再在背景壓縮，吃得到快取，同一份內容不會付兩次原價
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
                elif not getattr(ctx, "force_compact", False):
                    n = _fails[sid] = _fails.get(sid, 0) + 1
                    if n == AUTO_FAIL_MAX:
                        try:
                            await ctx.emit({"type": "notice", "session_id": sid,
                                            "text": f"自動壓縮失敗（{str(e)[:200]}），先停止自動壓縮；按「立即壓縮」可以再試，成功後恢復"})
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

        prev = summary.get("text") or ""
        # 第一次：長度照「被摘要的內容有多少 token」乘摘要比例（工具輸出全文也算）。更新：以上限為準，
        # 沒變的行原樣保留，超過上限時模型順便刪掉過時、重複的行
        src_tok = tokens.estimate_messages(older) * k
        cap_tok = min(_sum_tok(), int(window * SUM_WIN)) if window else _sum_tok()
        want = max(SUM_MIN_TOK, min(cap_tok, int(src_tok * _ratio())))
        target = int((cap_tok if prev else want) * CHARS_PER_TOK)
        mode = _mode()

        why = (getattr(ctx, "compact_reason", "") or "手動壓縮") if force else f"上下文已用 {used / window:.0%}"
        if mid:
            why = "回合進行中，" + why
        if getattr(ctx, "is_bg", False):
            why += "，在背景寫摘要（對話照常進行，寫好後下一次送出開始用）"
        ctx.compact_count = len(older)
        await _compact_state(ctx, True)
        text, stat = await _write(ctx, model, prev, pending[cut:], target, src_tok, why)
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
                          "kept": sorted(recent_ids), "mode": mode, "inflight": inflight, "cache": stat}
        cur["tok_k"] = round(k, 3)
        from woof.agent.loop import mark_rewrite, retire_images_on_compact

        retire_images_on_compact(ctx.session_id, cur, _keep_images())   # 前綴反正重算，舊圖一起收，只留最新幾張
        mark_rewrite(cur, pending[-1]["id"])   # 原樣保留的那幾則，前面已經換成摘要：它們的思考（Claude）之後不送
        s2 = store.update_session(ctx.session_id, engine_state=cur)
        # 對話畫面據此在被摘要的訊息後面畫一條分隔線（可展開看摘要）
        await ctx.emit({"type": "session_updated", "session": s2})
        hit = f"，送出 {stat[0]:,} token、快取命中 {stat[1] / stat[0]:.0%}" if stat[0] else ""
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": (f"已把這一回合較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {len(recent_ids)} 則保持原樣，"
                                 f"使用者這次的要求會在摘要後面重述，模型接著做{hit}") if inflight else
                                f"已把較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {len(recent_ids)} 則保持原樣{hit}"})
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
        return bool(await prepare_turn(ctx))   # 失敗一次自動壓縮就先停（_fails），不會每一步都再試

    woof.on("prepare_turn", prepare_turn, order=20)
    woof.on("after_turn", after_turn, order=20)
    woof.on("force_compact", force_compact, order=20)
    woof.on("mid_turn", mid_turn, order=20)
    woof.on("before_request", before_request, order=20)
