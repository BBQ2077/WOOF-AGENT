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
        if turn is None:
            return {"output": "只能在對話中使用", "is_error": True}
        raw = args.get("questions")
        if raw:
            qs, err = tasks.ask_questions(raw)
            if err:
                return {"output": err, "is_error": True}
            if len(qs) > 1 or qs[0]["multi"]:
                return {"output": await tasks.ask_many(turn, qs)}
            q, opts = qs[0]["question"], qs[0]["options"]
        else:   # 舊寫法：單一 question＋options
            q, opts = str(args.get("question") or "").strip(), args.get("options") or []
        if not q:
            return {"output": "需要 questions（或 question）", "is_error": True}
        answer = await tasks.ask(turn, q, opts, record=False)
        if tasks.timed_out(answer):
            return {"output": answer}
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
        "更新這個對話的 TODO 清單（顯示在聊天區上方），每次傳完整清單。status：pending、doing、done、skipped、fa"
        "iled。多步驟工作先列清單，做完一步就更新。cancelled 是使用者取消的：不要做、不能改回，照原樣保留。",
        todo_schema,
        todo,
    )
    woof.add_tool(
        "ask_user",
        "問使用者並等回答。方向、偏好、有風險的操作要使用者決定時用，不要自己猜；小事自己決定。互不相關的問題一次問（1～5 題同一張卡），某題答案會"
        "改變另一題時才分開問。選項只放 options（最多 4 個，推薦的放第一個），不給就是自由回答；使用者每題都能改成自己打字。回傳依序寫每題"
        "回答，沒答標「略過」；逾時沒回應也會略過並寫明人不在，這時自己判斷繼續，收不回來的事留到最後再問。",
        {"type": "object", "properties": {
            "questions": {
                "type": "array", "minItems": 1, "maxItems": 5,
                
                "items": {"type": "object", "properties": {
                    "question": {"type": "string", "description": "題目，說明為什麼問；選項不要寫在這裡"},
                    "options": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                                "description": "推薦的放第一個；不給＝自由回答"},
                    "multi_select": {"type": "boolean", "description": "可複選"}},
                    "required": ["question"]}}},
         "required": ["questions"]},
        ask_user,
    )
