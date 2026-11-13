"""內部指令：WOOF 自己發給模型的流程指令（目標步驟、審核、方案、子代理任務）。

存進對話時 meta 帶 {"internal": 種類}。
- 送給模型時加上明確標頭，模型知道這不是使用者本人說的。
- 前端收合顯示，不當成使用者氣泡。
"""
from __future__ import annotations

LABEL = {
    "goal_step": "目標・執行步驟",
    "review_feedback": "審核回饋",
    "review_request": "審核請求",
    "plan_request": "方案請求",
    "plan_feedback": "方案回饋",
    "subagent_task": "子代理任務",
    "subagent_followup": "子代理追加要求",
    "expand_request": "思維拓展請求",
    "nudge": "空回覆補問",
    "truncated": "截斷接續",
    "stall": "停下補推",
    "todo_left": "待辦未完成",
    "resume": "中斷後接續",
    "bg_done": "背景指令結束",
    "steps_low": "步數快用完",
}


def meta(kind: str) -> dict:
    return {"internal": kind}


def label(kind: str) -> str:
    return LABEL.get(kind, "內部指令")


def for_model(kind: str, content: list[dict]) -> list[dict]:
    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    from woof.prompts import get as _prompt
    head = _prompt("internal_head", label=label(kind))
    rest = [b for b in content if b.get("type") != "text"]
    return [{"type": "text", "text": head + text}, *rest]
