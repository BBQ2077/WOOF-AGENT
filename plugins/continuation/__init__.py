"""做到一半停下時，讓模型接著做完。

涵蓋：輸出被截斷、宣告下一步卻停下、背景指令結束通知、壓縮後重述進行中的請求：
  - 回覆被輸出長度上限截斷（stop_reason=max_tokens）：請模型從斷掉的地方接著寫
  - 回覆很短、結尾是「接下來我會／讓我…」卻沒呼叫工具：推它直接動手
  - 待辦清單還有沒完成的項目就結束：提醒一次（在問使用者問題時不提醒）
  - 背景指令結束：把結果送回對話（閒置就開新的一步，執行中就排隊）
  - 回合中途出錯：自動接續（次數見設定），其餘情況前端顯示「繼續」按鈕
壓縮後重述進行中的請求在 context 插件（mid_turn）。
"""
from __future__ import annotations

import re

# 結尾宣告「下一步要做什麼」卻停下。只用窄規則判斷：
#   只看 400 字以內的短回覆、只看最後一句、一定要有「現在／接下來／下一步」這類時間詞
#   （或「讓我＋動作」），所以「我會建議用 A」「我會再確認」這種一般句子不會中。
_ACT = r"(?:看|查|檢查|讀|打開|開|執行|跑|運行|修改|改|修|寫|建立|新增|刪|搜尋|搜|找|測試|測|確認|試|安裝|下載|整理|處理|分析|比對|列出|實作|更新|重新|動手|開始)"
_TAIL_INTENT = re.compile(
    r"(?:^|[。！？!?\n：:；;，,…]\s*)(?:"
    r"(?:接下來|接著|現在|下一步|馬上|立刻|這就|那我就)\s*(?:我|我們)?\s*(?:就|先|再|來|會|要|將|開始|去){1,2}\s*" + _ACT +
    r"|(?:我|我們)\s*(?:現在|接下來|這就|馬上|立刻)\s*(?:就|先|再|來|會|要|將|開始|去){0,2}\s*" + _ACT +
    r"|讓我(?:們)?\s*(?:現在|先|再|來|接著)?\s*" + _ACT +
    r")[^。！？!?\n]{0,80}[：:…\.。]?\s*$"
    r"|(?:\blet me now\b|\bi(?:'|’)?ll now\b|\bi will now\b|\bnow i(?:'|’)ll\b|\bnow i will\b|\bnext[,:] i\b)[^.!?\n]{0,100}[.:…]?\s*$",
    re.IGNORECASE,
)
_INTENT_MAX = 400
_QUESTION_END = re.compile(r"[?？]\s*[)）」』\]]*\s*$")
# 最後一句是條件或客套（「如有問題我會再調整」）就不算停下
_POLITE_TAIL = re.compile(r"(?:如果|如有|若|需要的話|有需要|有問題|隨時|之後|改天|下次|if you|feel free|anytime)[^。！？!?\n]{0,40}$", re.I)

# 推它的那句話在 woof/prompts.py（truncated、stall、todo_left），設定頁可以檢視、覆寫
from woof.prompts import get as _prompt


def _text(msg: dict) -> str:
    return "\n".join(str(b.get("text") or "") for b in msg.get("content") or [] if b.get("type") == "text").strip()


def _n(woof, key: str, dflt: int) -> int:
    try:
        v = woof.settings.get(key)
        return max(0, int(dflt if v is None or v == "" else v))
    except (TypeError, ValueError):
        return dflt


def register(woof):
    async def continue_turn(ctx, msg: dict, specs: list):
        counts = ctx.cont_counts
        meta = msg.get("meta") or {}
        text = _text(msg)
        # 1. 輸出被截斷
        if meta.get("stop_reason") in ("max_tokens", "length"):
            if counts.get("truncated", 0) < _n(woof, "truncated", 3):
                counts["truncated"] = counts.get("truncated", 0) + 1
                return ("truncated", _prompt("truncated"))
            return None
        # 2. 說要動手卻停下（只在有工具可用時）
        if specs and text and len(text) <= _INTENT_MAX and _TAIL_INTENT.search(text[-160:]) \
                and not _QUESTION_END.search(text) and not _POLITE_TAIL.search(text.rstrip("。.！!…")):
            if counts.get("stall", 0) < _n(woof, "stall", 2):
                counts["stall"] = counts.get("stall", 0) + 1
                return ("stall", _prompt("stall"))
        # 3. 待辦沒做完
        if specs and not _QUESTION_END.search(text) and counts.get("todo", 0) < _n(woof, "todo", 1):
            from woof import store
            st = store.get_session(ctx.session_id) or {}
            todos = (st.get("engine_state") or {}).get("todos") or []
            left = [t for t in todos if t.get("status") in ("pending", "doing")]
            goal = (st.get("engine_state") or {}).get("goal") or {}
            if left and goal.get("status") not in ("running", "planning"):  # /goal 自己會一步一步推
                counts["todo"] = counts.get("todo", 0) + 1
                items = "\n".join(f"- {t['text']}" for t in left[:12])
                return ("todo_left", _prompt("todo_left", n=len(left), items=items))
        return None

    def max_steps(ctx):
        return _n(woof, "max_steps", 0)  # 0＝不限（步數上限造成的半途中斷比它擋下的問題多）

    woof.on("continue_turn", continue_turn, order=50)
    woof.on("max_steps", max_steps, order=50)
