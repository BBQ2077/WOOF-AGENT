"""任務工具：todo（寫 TODO 清單）與 ask_user（向使用者提問）。實作在 woof/agent/tasks.py。"""
from __future__ import annotations


def register(woof):
    async def todo(args: dict, tctx) -> dict:
        from woof.agent import tasks

        turn = getattr(tctx, "turn", None)
        if turn is None:
            return {"output": "這個環境不能更新 TODO", "is_error": True}
        items = args.get("items") or []
        if not isinstance(items, list):
            return {"output": "items 要是陣列：[{text, status}]", "is_error": True}
        clean = tasks.set_todos(turn.session_id, items)
        await tasks._push_todos(turn.emit, turn.session_id, clean)
        done = sum(1 for t in clean if t["status"] == "done")
        lines = [f"- [{t['status']}] {t['text']}" for t in clean]
        return {"output": f"已更新 TODO（完成 {done}／{len(clean)}）：\n" + "\n".join(lines)}

    async def ask_user(args: dict, tctx) -> dict:
        from woof.agent import tasks

        turn = getattr(tctx, "turn", None)
        q = str(args.get("question") or "").strip()
        if turn is None or not q:
            return {"output": "需要 question，且只能在對話中使用", "is_error": True}
        answer = await tasks.ask(turn, q, args.get("options") or [])
        return {"output": f"使用者回答：{answer}"}

    todo_schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "status": {"type": "string"}},
                    "required": ["text"],
                },
            },
        },
        "required": ["items"],
    }
    woof.add_tool(
        "todo",
        "更新這個對話的 TODO 清單（會顯示在聊天區上方）。每次都傳完整清單。"
        "status 只能是 pending、doing、done、skipped、failed。多步驟工作開始時先列清單，做完一步就更新。",
        todo_schema,
        todo,
    )
    woof.add_tool(
        "ask_user",
        "向使用者提問並等待回答。遇到需要使用者決定的事（方向、偏好、有風險的操作）就用這個，不要自己猜。"
        "可以給 options 讓使用者點選；使用者也可以自己輸入。",
        {"type": "object", "properties": {
            "question": {"type": "string", "description": "要問的問題，說清楚為什麼要問"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "可選的答案，最多 6 個"}},
         "required": ["question"]},
        ask_user,
    )
