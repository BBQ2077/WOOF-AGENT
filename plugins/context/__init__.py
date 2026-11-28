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
# 自動壓縮連續沒成功的次數：失敗過就不在回合中每一步重試（每次都要付錢），等這一步結束後再試一次；
# 那一次模型還是寫不出來，就用程式整理的摘要（_fallback_text），上下文不會一直堆。成功就歸零
_fails: dict[str, int] = {}
# 程式整理的摘要只是暫用：存下後在背景再請模型照原本的內容重寫（REDO_DELAY 秒後，失敗就隔 REDO_GAP 秒），
# 最多 REDO_MAX 次；寫好就取代。_redo_snap＝寫不出來那次送的請求（前面還在快取裡，重寫照樣吃快取）
_redo_snap: dict[str, dict] = {}
_redo_t: dict[str, asyncio.Task] = {}
_override: dict[str, dict] = {}   # 重組請求時改用的摘要（重寫：用程式整理之前的那份）
REDO_MAX, REDO_DELAY, REDO_GAP = 2, 15, 60

# 摘要長度（token）：第一次照「被摘要內容 × 摘要比例」，至少 1500；不超過設定的上限（summary_tokens）和上下文的 10%
SUM_MIN_TOK, SUM_TOK_DEFAULT, SUM_WIN = 1500, 8000, 0.10
CHARS_PER_TOK = 1.5
# 原樣保留的最近訊息（實際 token，設定 tail_tokens）；最多佔上下文的 25%，小模型才不會整段都是保留區
TAIL_TOK_DEFAULT, TAIL_WIN = 10000, 0.25
# 摘要後面附上被壓縮那段裡使用者最近幾則原話（新的在前）：則數、合計 token（設定 quote_count、quote_tokens）、單則上限（字）
# 單則超過 USER_MAX 字（例如直接貼文件）只附開頭和結尾，註明用 session_search 查全文
QUOTE_N_DEFAULT, QUOTE_TOK_DEFAULT, USER_MAX = 3, 1500, 750
REPLY_HEAD_DEFAULT = 250   # 每則原話前面附上它回應的助手那則的開頭（先講結論，開頭就是重點），看得出回的是什麼
# 快取大概還在的時間（Claude 放 5 分鐘；其他家多半更久）：超過就先讓這一步照常送（順便把快取建回來），下一步再壓縮
CACHE_TTL_ANTHROPIC, CACHE_TTL_OTHER = 270, 600
# 被摘要的部分超過這麼多 token，寫出來的摘要（編輯後的整份）卻不到 MIN_CHARS 字：沒在寫摘要
SHORT_SRC_TOK, MIN_CHARS = 10000, 200
FIT = 0.95           # 請求估計超過模型窗口的這個比例：從最舊的訊息往後拿掉，直到送得出去
# 摘要用的提示詞在 woof/prompts.py（compact、compact_inline、compact_edit…），設定頁可以檢視、覆寫
from woof.prompts import get as _prompt_get, get_in as _prompt_in  # noqa: E402


def _elide(text: str, cap: int, more: str = "") -> str:
    if len(text) <= cap:
        return text
    head = cap * 3 // 4
    return f"{text[:head]}\n…（中間省略 {len(text) - cap} 字{more}）…\n{text[-(cap - head):]}"


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
        text = _elide(text, min(USER_MAX, room), f"，全文用 session_search around={m['id']} 查" if m.get("id") else "")
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


_CONTENT = re.compile(r"^\s*(?:[-#*|>]|\d+[.、)]|[（(]?\d)")


def _parse_ops(out: str):
    """抓出 @@ 指令：第一個指令前面的說明、``` 、行號前綴略過；@@ 完 後面的都不算。回傳 (指令, 有沒有寫 @@ 完)"""
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
    for o in ops:
        while o[3] and not o[3][-1].strip():
            o[3].pop()
        while o[3] and not o[3][0].strip():
            o[3].pop(0)
    return ops, ended


def _apply_edits(prev: str, out: str, allow_big: bool = False) -> tuple[str | None, list[dict], str]:
    """把行號編輯指令套到舊摘要上，回傳 (新摘要, 猜著套的地方, 完全沒有指令時的原因)。
    @@ 改 a-b ＋新內容：換掉第 a 到 b 行；@@ 刪 a-b；@@ 插 n ＋新內容：插在第 n 行後面（0＝最前面，超過最後一行＝接在最後）；
    @@ 不變；最後一行 @@ 完。
    每一條都會套上、模型寫的內容一定收進去：照字面套不上的，用不會掉內容的方式處理（行號超出＝接在最後、
    範圍重疊＝插在那段後面、刪的下面有內容＝當成改、改了沒附內容＝舊的留著、一次刪掉大半＝先不刪、
    沒寫 @@ 完而結尾像說明＝先收進去），這些地方記在第二個回傳值（新摘要裡的行號範圍＋怎麼處理的），
    交給模型補正一次。沒寫指令、但寫出了整份摘要（段落標題齊、長度跟舊的差不多）：直接當新摘要用。"""
    lines = prev.split("\n")
    total = len(lines)
    ops, ended = _parse_ops(out)
    if not ops:
        body = out.strip()
        heads = [ln for ln in body.split("\n") if ln.startswith("## ")]
        if not ended and len(heads) >= 3 and len(body) >= len(prev) * 0.6:
            return body, [], ""
        return None, [], "沒有寫出 @@ 編輯指令"
    ops = [o for o in ops if o[0] != "不變"]
    if not ops:
        return prev, [], ""
    why: dict[int, str] = {}          # 指令序號 → 怎麼處理的
    loose: list[str] = []             # 沒有對應行的（例如沒寫行號的刪）
    rep: dict[int, tuple[int, list[str], int]] = {}
    dels: dict[int, int] = {}
    ins: dict[int, list[tuple[list[str], int]]] = {}
    keep: dict[int, int] = {}         # 照字面該刪或換、先留著的舊行 → 指令序號
    taken: set[int] = set()

    def label(op, a, b):
        return f"@@ {op} {a if a is not None else ''}{f'-{b}' if b is not None else ''}".rstrip()

    for idx, (op, a, b, new) in enumerate(ops):
        lab = label(op, a, b)
        if op == "插":
            if not new:
                continue                              # 沒附內容的插：本來就沒東西
            ins.setdefault(total if a is None else max(0, min(a, total)), []).append((new, idx))
            if a is None:
                why[idx] = f"{lab} 沒寫行號，接在最後面"
            continue
        if a is None or a > total:
            if new:
                ins.setdefault(total, []).append((new, idx))
                why[idx] = f"{lab} " + ("沒寫行號" if a is None else f"超過最後一行（{total}）") + "，內容接在最後面"
            else:
                loose.append(f"{lab} " + ("沒寫行號" if a is None else f"超過最後一行（{total}）") + "，沒有刪任何行")
            continue
        b = a if b is None else b
        if a > b:
            a, b = b, a
        if a < 1 or b > total:
            why[idx] = f"{lab} 超出 1 到 {total} 行，照 {max(a, 1)}-{min(b, total)} 處理"
            a, b = max(a, 1), min(b, total)
        if op == "刪" and new:
            op = "改"
            why[idx] = f"{lab} 下面寫了內容，當成改"
        if op == "改" and not new:
            for j in range(a, b + 1):
                if j not in taken:
                    keep[j] = idx
            why[idx] = f"{lab} 下面沒有內容，這幾行先留著（要刪請用 @@ 刪）"
            continue
        rng = set(range(a, b + 1))
        if rng & taken:
            if op == "刪":
                for j in sorted(rng - taken):
                    dels[j] = idx
                taken |= rng
            else:
                ins.setdefault(b, []).append((new, idx))
                why[idx] = f"{lab} 範圍跟前面的指令重疊，內容插在第 {b} 行後面"
            continue
        taken |= rng
        if op == "刪":
            for j in rng:
                dels[j] = idx
        else:
            rep[a] = (b, new, idx)
    if ops and not ended and ops[-1][3] and not _CONTENT.match(ops[-1][3][-1]):
        why.setdefault(len(ops) - 1, f"最後沒寫 @@ 完，最後一行「{ops[-1][3][-1].strip()[:30]}」也收進去了（是說明就刪掉）")

    def build():
        res = [(ln, idx) for new, idx in ins.get(0, []) for ln in new]
        i = 1
        while i <= total:
            if i in rep:
                end, new, idx = rep[i]
                res += [(ln, idx) for ln in new]
                for j in range(i, end + 1):
                    res += [(ln, ix) for new2, ix in ins.get(j, []) for ln in new2]
                i = end + 1
                continue
            if i not in dels:
                res.append((lines[i - 1], keep.get(i)))
            res += [(ln, ix) for new2, ix in ins.get(i, []) for ln in new2]
            i += 1
        return res

    res = build()
    size = len("\n".join(r[0] for r in res))
    if not allow_big and len(prev) > 2000 and size < len(prev) * 0.4:
        # 一次刪掉大半，多半是看錯行號：先不刪，舊的留著，請模型確認
        for j, idx in list(dels.items()):
            keep[j] = idx
            why[idx] = f"{label(*ops[idx][:3])} 一次刪掉大半，這幾行先留著（確定要刪就再寫一次 @@ 刪）"
        dels.clear()
        res = build()
        if len("\n".join(r[0] for r in res)) < len(prev) * 0.4:
            for a0, (b0, new, idx) in list(rep.items()):
                if b0 - a0 >= 4:
                    del rep[a0]
                    for j in range(a0, b0 + 1):
                        keep[j] = idx
                    ins.setdefault(b0, []).append((new, idx))
                    why[idx] = f"{label(*ops[idx][:3])} 一次換掉大半，舊的先留著、新的接在後面（確定就刪掉舊的）"
            res = build()
    flags = []
    for idx, w in sorted(why.items()):
        pos = [n for n, r in enumerate(res, 1) if r[1] == idx]
        spans = []
        for n in pos:
            if spans and spans[-1][1] == n - 1:
                spans[-1][1] = n
            else:
                spans.append([n, n])
        flags.append({"why": w, "lines": [tuple(x) for x in spans]})
    flags += [{"why": w, "lines": []} for w in loose]
    return "\n".join(r[0] for r in res).strip("\n"), flags, ""


def _fix_items(text: str, flags: list[dict], cap: int = 4000) -> str:
    """要模型補正的那幾處：怎麼處理的＋新摘要裡那幾行（附新行號）"""
    lines = text.split("\n")
    out, used = [], 0
    for f in flags:
        block = [f"- {f['why']}"]
        for a, b in f["lines"]:
            for n in range(max(1, a), min(len(lines), b) + 1)[:20]:
                block.append(f"  {n}| {lines[n - 1]}")
        piece = "\n".join(block)
        if used + len(piece) > cap:
            piece = piece[:max(0, cap - used)]
        out.append(piece)
        used += len(piece)
        if used >= cap:
            break
    return "\n".join(out)


def _fallback_text(prev: str, older: list[dict]) -> str:
    """摘要模型寫不出來時，程式自己整理的摘要（不用模型）：先前摘要原樣保留，接上這段的重點——
    使用者講過的話、用過的工具、提到的檔案、助手最後的回覆開頭。下次壓縮時模型會在這份上面編輯整理"""
    users, tools, files, last_reply = [], {}, [], ""
    for m in older:
        blocks = m.get("content") or []
        if m["role"] == "user" and not (m.get("meta") or {}).get("internal"):
            t = " ".join(" ".join(b.get("text") or "" for b in blocks if b.get("type") == "text" and not b.get("_wrap")).split())
            if t:
                users.append(t[:300] + ("…" if len(t) > 300 else ""))
        elif m["role"] == "assistant":
            for b in blocks:
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    last_reply = b["text"].strip()
                elif b.get("type") == "tool_call":
                    tools[b.get("name") or "?"] = tools.get(b.get("name") or "?", 0) + 1
                    for k, v in (b.get("args") or {}).items():
                        if ("path" in k or "file" in k) and isinstance(v, str) and 0 < len(v) <= 160 and v not in files:
                            files.append(v)
    a, b = older[0]["id"], older[-1]["id"]
    sec = [f"## 第 {a} 到 {b} 則訊息的重點（程式整理，細節用 session_search 查原文）"]
    if users:
        picked, used = [], 0
        for t in reversed(users):
            if used + len(t) > 3000:
                break
            picked.append(t)
            used += len(t)
        sec.append("使用者說過：")
        sec += [f"- {t}" for t in reversed(picked)]
    if tools:
        sec.append("用過的工具：" + "、".join(f"{n}×{c}" for n, c in sorted(tools.items(), key=lambda x: -x[1])[:20]))
    if files:
        sec.append("提到的檔案：" + "、".join(files[-20:]))
    if last_reply:
        sec.append("助手最後的回覆開頭：" + " ".join(last_reply.split())[:400])
    sec = "\n".join(sec)
    return f"{prev.rstrip()}\n{sec}" if prev else sec


class _RedoTooBig(RuntimeError):
    """原本的內容已經超過模型上限，重寫送不出完整的那段"""


class _NoSummary(RuntimeError):
    """摘要模型沒寫出能用的東西（去呼叫工具、空白、寫成工具呼叫、沒有指令、太短）"""


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

    async def _request(ctx, model: dict, prev: str, kept: list[dict], target: int, fit: float = FIT,
                       redo: dict | None = None, tidy: bool = False) -> dict:
        """寫摘要的請求：下一步本來就要送的內容（跟主對話一字不差），最後加一則壓縮指令。
        記憶體裡有上一步就接著用；WOOF 重開過或對話狀態變了，照現在的狀態重組（同一套流程）。
        超過模型窗口（上下文已經爆了）就從最舊的訊息往後拿掉，直到送得出去。"""
        from woof.agent import loop
        from woof import tokens

        sid = ctx.session_id
        use = loop.aux_model("compact", model) or model
        note = ""
        if redo is not None:
            # 重寫程式整理的摘要：用寫不出來那次的請求（前面是程式整理之前的摘要＋原本的對話）
            snap = _redo_snap.get(sid)
            fresh = bool(snap) and snap.get("for") == redo["at"] and snap["model"] == model.get("id")
        else:
            snap = loop.LAST_REQ.get(sid)
            fresh = bool(snap) and snap["model"] == model.get("id") and snap.get("sig") == loop.req_sig(sid)
        if fresh:
            new = [m for m in loop.history(sid) if int(m.get("id") or 0) > int(snap["through"] or 0)]
            new = loop._drop_hollow_replies(loop._drop_missing_images(new))
            system, msgs, specs, effort = snap["system"], [*snap["messages"], *new], snap["specs"], snap["effort"]
            age = time.time() - snap["at"]
            if age > _ttl(model):
                note = f"上一步在 {int(age // 60)} 分鐘前，快取大概過期了，這次整段照原價算"
        else:
            if redo is not None:
                _override[sid] = redo["base"]
            try:
                r = await loop.rebuild_request(sid, model, ctx)
            finally:
                _override.pop(sid, None)
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
            # 編輯：舊摘要附行號放在指令裡，模型只輸出要改的行（程式套用）；手動壓縮另外要它整份檢查清理一次（一樣只寫指令）
            n_lines = len(prev.split("\n"))
            rules += ("\n" + _prompt_in("compact_edit", mode, n=len(prev), cap=target, last=n_lines)
                      + ("\n" + _prompt_in("compact_tidy", mode) if tidy else "")
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
        ids = [int(m["id"]) for m in msgs if m.get("id")]
        return {"use": use, "system": system, "messages": [*msgs, ask], "specs": specs, "effort": effort,
                "note": note, "dropped": dropped,
                "snap": {"model": model.get("id"), "through": max(ids) if ids else 0, "system": system,
                         "messages": msgs, "specs": specs, "effort": effort}}

    def _check(out: str | None, prev: str, src_tok: float, allow_big: bool = False) -> tuple[str, list[dict], str]:
        """模型的回覆能不能當摘要：回傳 (摘要全文, 猜著套的地方, 不能用的原因)。每次壓縮都檢查"""
        from woof import tokens

        if out is None:
            return "", [], "去呼叫了工具"
        out = out.strip()
        if not out:
            return "", [], "回了空白"
        if tokens.fake_call(out):
            return "", [], "寫成了工具呼叫"
        text, flags = out, []
        if prev:
            text, flags, why = _apply_edits(prev, out, allow_big)
            if text is None:
                return "", [], why
        if src_tok > SHORT_SRC_TOK and len(text) < MIN_CHARS:
            return "", [], f"只寫了 {len(text)} 字"
        return text, flags, ""

    async def _call(ctx, req: dict, msgs: list[dict], stat: list[int]):
        from woof.agent import loop

        n = 0
        while True:
            try:
                return await loop.complete(req["use"], req["system"], msgs, tools=req["specs"], effort=req["effort"],
                                           session_id=ctx.session_id, kind="compact")
            except loop.ToolCallInstead:
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 連線、限流、伺服器暫時出錯：跟主對話一樣等一下重送同一份（設定 → 一般的重試次數）
                if n >= loop.api_retries() or not loop.is_transient_error(e):
                    raise
                n += 1
                wait = loop.retry_wait(e, n)
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"摘要模型暫時失敗（{str(e)[:120]}），{wait} 秒後重送（{n}/{loop.api_retries()}）"})
                await asyncio.sleep(wait)
            finally:
                u = loop.AUX_USAGE.pop(ctx.session_id, None) or {}
                stat[0] += int(u.get("input_tokens") or 0)
                stat[1] += int(u.get("cache_read") or 0)

    async def _write(ctx, model: dict, prev: str, kept: list[dict], target: int, src_tok: float, why: str,
                     redo: dict | None = None, box: dict | None = None, tidy: bool = False):
        """寫摘要，回傳 (摘要全文, [輸入, 快取讀取])。都接在同一份請求後面，前面吃快取：
        - 編輯指令有幾處只能猜著套：內容都已經收進去，再接一則請模型只補正那幾處（不重寫整份）；補正寫壞了就用補正前的
        - 完全沒寫出能用的東西：在指令後面註明哪裡不對再要一次；還是沒有就丟 _NoSummary（交給呼叫端決定要不要用程式整理的摘要）"""
        from woof.agent import loop

        sid = ctx.session_id
        req = await _request(ctx, model, prev, kept, target, redo=redo, tidy=tidy)
        if box is not None:
            box["req"] = req
        if redo is not None and req["dropped"] and not getattr(ctx, "manual", False):
            raise _RedoTooBig("原本那段已經超過模型上限，送不出完整的內容")
        await ctx.emit({"type": "notice", "session_id": sid,
                        "text": f"{why}，正在壓縮較早的 {ctx.compact_count} 則訊息"
                                f"（{'只寫要改的行，順便整份清理' if tidy else '只寫要改的行' if prev else f'目標約 {target} 字'}）…" + (f"（{req['note']}）" if req["note"] else "")})
        problem, stat = "", [0, 0]
        for attempt in range(2):
            msgs = req["messages"]
            if problem and problem != "超出模型上限":
                ask = msgs[-1]
                again = ask["content"][0]["text"] + _prompt_get(
                    "compact_retry", problem=problem,
                    what="@@ 編輯指令（行號照【先前摘要】，最後一行 @@ 完）" if prev else "摘要正文")
                msgs = [*msgs[:-1], {**ask, "content": [{"type": "text", "text": again}]}]
                await ctx.emit({"type": "notice", "session_id": sid,
                                "text": f"摘要模型這次{problem}，用同一份內容再寫一次（前面照樣吃快取）"})
            try:
                out = await _call(ctx, req, msgs, stat)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 0 and loop.is_overflow_error(e):
                    # 估計比實際少：再多拿掉一些舊訊息重送
                    req = await _request(ctx, model, prev, kept, target, fit=FIT * 0.8, redo=redo, tidy=tidy)
                    if box is not None:
                        box["req"] = req
                    problem = "超出模型上限"
                    await ctx.emit({"type": "notice", "session_id": sid,
                                    "text": "摘要請求超出模型上限，多拿掉一些最舊的訊息再送一次"
                                            + (f"（{req['note']}）" if req["note"] else "")})
                    continue
                raise RuntimeError(f"壓縮摘要失敗（{req['use'].get('label', req['use']['id'])}）：{str(e)[:300]}") from e
            text, flags, problem = _check(out, prev, src_tok)
            if problem:
                continue
            if flags:
                text = await _fix(ctx, req, msgs, out, text, flags, src_tok, stat)
            return text, stat
        raise _NoSummary(f"摘要模型兩次都沒寫出能用的摘要（{problem}）")

    async def _fix(ctx, req, msgs, out, text, flags, src_tok, stat) -> str:
        """編輯指令有幾處是猜著套的：接著同一份請求（加上模型剛才的回覆），請它只補正那幾處"""
        sid = ctx.session_id
        await ctx.emit({"type": "notice", "session_id": sid,
                        "text": f"編輯指令有 {len(flags)} 處沒辦法照字面套（{'；'.join(f['why'] for f in flags[:3])}），"
                                "內容都先收進去了，請模型補正這幾處（接著同一份請求，吃快取）"})
        follow = [*msgs, {"role": "assistant", "content": [{"type": "text", "text": out}]},
                  {"role": "user", "meta": {"internal": "compact_ask"},
                   "content": [{"type": "text", "text": _prompt_get("compact_fix", items=_fix_items(text, flags))}]}]
        try:
            out2 = await _call(ctx, req, follow, stat)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await ctx.emit({"type": "notice", "session_id": sid,
                            "text": f"補正沒送成（{str(e)[:120]}），用補正前的摘要（內容都在，下次壓縮時再整理）"})
            return text
        t2, f2, problem = _check(out2, text, src_tok, allow_big=True)
        if problem:
            await ctx.emit({"type": "notice", "session_id": sid,
                            "text": f"補正的回覆{problem}，用補正前的摘要（內容都在，下次壓縮時再整理）"})
            return text
        if f2:
            await ctx.emit({"type": "notice", "session_id": sid,
                            "text": f"補正後還有 {len(f2)} 處是猜著套的，內容都保留了，下次壓縮時再整理"})
        return t2

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
        if t and not t.done():
            return
        _bg[sid] = asyncio.create_task(_precompact(sid, ctx.model_cfg, ctx.registry, ctx.emit,
                                                   mid=bool(getattr(ctx, "mid_turn", False))))

    async def _precompact(session_id: str, model: dict, registry, emit, mid: bool = False) -> None:
        # 上一次自動壓縮沒成功：這次模型再寫不出來，就用程式整理的摘要，上下文不會一直堆
        job = SimpleNamespace(session_id=session_id, model_cfg=model, registry=registry, emit=emit, mid_turn=mid,
                              force_compact=False, is_bg=True, threshold=_trigger(), compact_reason="", compact_count=0,
                              last_chance=_fails.get(session_id, 0) >= 1)
        try:
            await prepare_turn(job)
        except Exception:
            pass   # 失敗的提示在 prepare_turn 裡送

    def _schedule_redo(ctx, delay: float) -> None:
        """在背景請模型重寫程式整理的摘要（不擋對話）；同一個對話已經排了就不再排"""
        sid = ctx.session_id
        t = _redo_t.get(sid)
        if t and not t.done() and t is not asyncio.current_task():
            return
        model, registry, emit = ctx.model_cfg, ctx.registry, ctx.emit

        async def run():
            await asyncio.sleep(delay)
            job = SimpleNamespace(session_id=sid, model_cfg=model, registry=registry, emit=emit, mid_turn=False,
                                  force_compact=False, is_bg=True, threshold=_trigger(), compact_reason="",
                                  compact_count=0)
            try:
                await prepare_turn(job)
            except Exception:
                pass
        _redo_t[sid] = asyncio.create_task(run())

    async def after_turn(ctx):
        from woof import store
        from woof import tokens

        # 用的是程式整理的摘要、還有重寫次數、背景沒在排（例如 WOOF 重開過）：這一步結束後排一次重寫
        s = tokens.summary_of((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
        if s.get("fallback") and int((s.get("redo") or {}).get("n") or 0) < REDO_MAX:
            t = _redo_t.get(ctx.session_id)
            if not t or t.done():
                _schedule_redo(ctx, 0)
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
            if r < _trigger():
                return None
            if r < _wait_at():
                # 剛失敗過：回合中每一步不重試，這一步結束後（after_turn）再試一次
                if _warm(sid, ctx.model_cfg) and not _fails.get(sid):
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
                    _fails[sid] = _fails.get(sid, 0) + 1
                    try:
                        await ctx.emit({"type": "notice", "session_id": sid,
                                        "text": f"自動壓縮這次沒成功（{str(e)[:200]}），原文先照送；這一步結束後再試一次，"
                                                "模型還是寫不出來就改用程式整理的摘要，上下文不會一直堆"})
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

        mid = bool(getattr(ctx, "mid_turn", False))
        msgs = store.get_messages(ctx.session_id)
        es = dict(st.get("engine_state") or {})
        summary = tokens.summary_of(es)  # 讀取時的摘要，寫回前會重新讀取
        manual = bool(getattr(ctx, "manual", False))
        # 現在用的是程式整理的摘要：從整理之前的那份摘要＋原本的對話，請模型重寫（還有次數、或手動壓縮）
        redo = summary.get("redo") if summary.get("fallback") else None
        if redo and int(redo.get("n") or 0) >= REDO_MAX and not manual:
            redo = None                       # 自動重寫的次數用完：照一般的壓縮，在程式整理的這份上面編輯
        fb = summary if redo else None
        if redo:
            redo = {**redo, "at": summary.get("at")}
            summary = redo["base"]
        redo_only = bool(redo) and not force and not manual and (not used or used / window < trigger)
        if not force and not redo and (not used or used / window < trigger):
            return
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
        if fb:
            # 重寫至少要涵蓋程式整理那份涵蓋到的地方
            cut = max(cut, sum(1 for m in pending if m["id"] <= int(fb.get("through") or 0)))
        if cut <= 0:
            ctx.compact_reason = f"還沒壓縮過的部分都在原樣保留的量（約 {_tail_tok():,} token）以內，不需要壓縮"
            return
        # 回合進行中、使用者這次的要求被切進摘要：在摘要後面原文重述（before_request），模型才知道還在做什麼
        inflight = 0
        if mid and (req is None or cut > req):
            inflight = pending[req]["id"] if req is not None else int(summary.get("inflight") or 0)
        if fb:
            inflight = inflight or int(fb.get("inflight") or 0)
        older, recent_ids = pending[:cut], {m["id"] for m in pending[cut:]}

        # 先試只清舊工具輸出：較早、已經用過的工具結果換成一行佔位，夠降到門檻以下就不花模型呼叫寫摘要。
        # 只在本來就要壓縮時做，平常不動，快取照舊。
        # Claude 不做：它的每段思考都綁著前面的內容，改了較早的工具輸出，後面所有思考就失效（照收費、模型讀到過期的推理）
        if not force and not redo and model.get("provider") != "anthropic":
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
        if redo_only:
            why = "先前用的是程式整理的摘要，請模型照原本的內容重寫"
        if mid:
            why = "回合進行中，" + why
        if getattr(ctx, "is_bg", False):
            why += "，在背景寫摘要（對話照常進行，寫好後下一次送出開始用）"
        ctx.compact_count = len(older)
        await _compact_state(ctx, True)
        # 一定要壓下去的時候（手動、上下文爆了、到等待線、上次自動壓縮已經失敗過）：模型寫不出來就用程式整理的摘要
        must = force or manual or bool(getattr(ctx, "last_chance", False)) \
            or bool(window and used / window >= _wait_at())
        fallback, box = False, {}
        try:
            text, stat = await _write(ctx, model, prev, pending[cut:], target, src_tok, why, redo=redo, box=box,
                                      tidy=bool(prev) and bool(getattr(ctx, "tidy", False)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if redo_only:
                # 重寫沒成功：程式整理的那份照用，隔一下再試，次數用完就停
                n = REDO_MAX if isinstance(e, _RedoTooBig) else int(redo.get("n") or 0) + 1
                cur = dict((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
                cs = cur.get("summary") or {}
                if cs.get("fallback") and cs.get("at") == redo["at"]:
                    cur["summary"] = {**cs, "redo": {**cs["redo"], "n": n}}
                    store.update_session(ctx.session_id, engine_state=cur)
                more = n < REDO_MAX
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"模型重寫摘要沒成功（{str(e)[:200]}），程式整理的那份先用著；"
                                        + (f"{REDO_GAP} 秒後再試一次" if more else
                                           "自動重寫先停，按「立即壓縮」可以再請模型重寫；上下文再到門檻時照常壓縮")})
                if more:
                    _schedule_redo(ctx, REDO_GAP)
                return False
            if not must:
                raise
            text, stat, fallback = _fallback_text(prev, older), [0, 0], True
            await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                            "text": f"{str(e)[:200]}；先用程式整理的摘要頂著（先前摘要原樣保留，接上這段使用者講過的話、"
                                    "用過的工具和檔案），上下文照樣降下來；"
                                    + (f"{REDO_DELAY} 秒後在背景請模型照原本的內容重寫，寫好就換掉" if not box.get("req", {}).get("dropped")
                                       else "原本那段已經超過模型上限，不自動重寫，下次壓縮時模型會在這份上面整理")})
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
        at = time.time()
        cur["summary"] = {"through": older[-1]["id"], "text": text, "quotes": quotes, "at": at,
                          "kept": sorted(recent_ids), "mode": mode, "inflight": inflight, "cache": stat}
        if fallback:
            # 記下整理之前的那份摘要，之後請模型照原本的內容重寫
            base = {k: v for k, v in summary.items() if k not in ("fallback", "redo")}
            r = box.get("req")
            cur["summary"].update(fallback=True, redo={"base": base, "n": REDO_MAX if r and r["dropped"] else 0})
            if r and not r["dropped"]:
                _redo_snap[ctx.session_id] = {**r["snap"], "at": at, "for": at}
        cur["tok_k"] = round(k, 3)
        from woof.agent.loop import mark_rewrite, retire_images_on_compact

        retire_images_on_compact(ctx.session_id, cur, _keep_images())   # 前綴反正重算，舊圖一起收，只留最新幾張
        mark_rewrite(cur, pending[-1]["id"])   # 原樣保留的那幾則，前面已經換成摘要：它們的思考（Claude）之後不送
        s2 = store.update_session(ctx.session_id, engine_state=cur)
        # 對話畫面據此在被摘要的訊息後面畫一條分隔線（可展開看摘要）
        await ctx.emit({"type": "session_updated", "session": s2})
        if fallback and not cur["summary"]["redo"]["n"]:
            _schedule_redo(ctx, REDO_DELAY)
        elif not fallback:
            _redo_snap.pop(ctx.session_id, None)
        hit = f"，送出 {stat[0]:,} token、快取命中 {stat[1] / stat[0]:.0%}" if stat[0] else ""
        if fb and not fallback:
            hit += "（取代先前程式整理的摘要）"
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
        summary = _override[ctx.session_id] if ctx.session_id in _override else tokens.summary_of(st.get("engine_state") or {})
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
        return bool(await prepare_turn(ctx))   # 自動壓縮失敗過（_fails）：回合中每一步不重試，回合結束再試

    woof.on("prepare_turn", prepare_turn, order=20)
    woof.on("after_turn", after_turn, order=20)
    woof.on("force_compact", force_compact, order=20)
    woof.on("mid_turn", mid_turn, order=20)
    woof.on("before_request", before_request, order=20)
