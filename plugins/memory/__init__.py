"""提示詞檔外掛：把設定檔、人物檔、使用者檔、記憶檔放進系統提示，並提供 memory 工具讓模型自己記下長期有用的事。

內容在「設定 → 提示詞與記憶」直接編輯，檔案存在 data/prompts/。
memory 工具寫進記憶檔或使用者檔（§ 分隔條目），新對話起生效；進行中的對話系統提示詞已凍結，不會讓快取失效。
"""
from __future__ import annotations


def register(woof):
    from woof import promptfiles

    def build_prompt(ctx):
        return promptfiles.build_prompt()

    async def memory(args: dict, tctx) -> dict:
        act = str(args.get("action") or "").strip()
        target = "user" if str(args.get("target") or "") == "user" else "memory"
        text = " ".join(str(args.get("text") or "").split())
        old = str(args.get("old") or "").strip()
        label = promptfiles.FILES[target]["label"]
        ents = promptfiles._entries(promptfiles.read(target))
        if act == "list":
            if not ents:
                return {"output": f"{label}是空的"}
            return {"output": "\n".join(f"{i + 1}. {e}" for i, e in enumerate(ents))}
        if act in ("replace", "remove"):
            hit = [i for i, e in enumerate(ents) if old and old in e]
            if len(hit) != 1:
                return {"output": f"old 要剛好對到一條{label}條目（現在對到 {len(hit)} 條）；先用 action=list 看內容", "is_error": True}
            if act == "remove":
                gone = ents.pop(hit[0])
                promptfiles.write(target, "\n§\n".join(ents) + ("\n" if ents else ""))
                return {"output": f"已從{label}刪除：{gone}"}
            if not text:
                return {"output": "replace 要給 text", "is_error": True}
            ents[hit[0]] = text
        elif act == "add":
            if not text:
                return {"output": "add 要給 text", "is_error": True}
            if any(text == e for e in ents):
                return {"output": f"{label}已經有這條了"}
            ents.append(text)
        else:
            return {"output": "action 要是 add、replace、remove、list 其中一個", "is_error": True}
        body = "\n§\n".join(ents) + "\n"
        lim = promptfiles.LIMITS.get(target) or 0
        if lim and len(body) > lim:
            return {"output": f"{label}會變成 {len(body)} 字，超過上限 {lim}。先用 replace 合併或 remove 刪掉過時的條目", "is_error": True}
        promptfiles.write(target, body)
        return {"output": f"已寫入{label}（共 {len(ents)} 條，{len(body)} 字），新對話起生效"}

    woof.on("build_prompt", build_prompt, order=10)
    woof.add_tool(
        "memory",
        "長期記憶：把之後的對話也用得到的事記下來（新對話起放進系統提示）。"
        "target=memory 記環境與經驗（路徑、工具怪癖、做過的決定）；target=user 記使用者的偏好與習慣。"
        "只記穩定、會重複用到的事；一次性的任務進度、對話內容不要記。使用者說「記住」時一定要記。"
        "action：add 新增、replace 用 old 對到一條改成 text、remove 用 old 對到一條刪掉、list 列出。",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove", "list"]},
            "target": {"type": "string", "enum": ["memory", "user"], "description": "預設 memory"},
            "text": {"type": "string", "description": "要記的內容，一條講一件事，簡短"},
            "old": {"type": "string", "description": "replace／remove 時，要對到的那條裡面的一段文字"},
        }, "required": ["action"]},
        memory,
    )
