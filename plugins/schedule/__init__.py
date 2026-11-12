"""排程工具：模型可以替使用者建立定時工作，例如「每天早上 9 點整理新聞」。

實際的排程器在 woof/cron.py（核心），這裡只是把它開放給模型用。關掉這個插件，排程仍會照常執行。
"""
from __future__ import annotations

from datetime import datetime

from woof import cron


def _fmt(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "—"


def register(woof):
    async def schedule(args: dict, tctx) -> dict:
        act = args.get("action") or "list"
        try:
            if act == "list":
                jobs = cron.load()
                if not jobs:
                    return {"output": "目前沒有排程工作。"}
                rows = [f"- [{j['id']}] {j['name']}｜{cron.describe(j['schedule'])}（{j['schedule']}）｜"
                        f"{'啟用' if j.get('enabled') else '暫停'}｜下次 {_fmt(j.get('next_run'))}｜上次 {_fmt(j.get('last_run'))} {j.get('last_status') or ''}"
                        for j in jobs]
                return {"output": "\n".join(rows)}
            if act == "create":
                body = {k: args.get(k) for k in ("name", "prompt", "schedule", "model", "catch_up") if args.get(k)}
                if not body.get("model"):
                    body["model"] = (getattr(getattr(tctx, "turn", tctx), "model_cfg", None) or {}).get("id", "")
                j = await cron.add(body)
                return {"output": f"已建立排程 [{j['id']}] {j['name']}：{cron.describe(j['schedule'])}，下次 {_fmt(j['next_run'])}。"}
            jid = str(args.get("id") or "")
            if act == "delete":
                ok = await cron.remove(jid)
                return {"output": "已刪除。" if ok else f"找不到排程 {jid}", "is_error": not ok}
            if act in ("pause", "resume"):
                j = await cron.edit(jid, {"enabled": act == "resume"})
                if not j:
                    return {"output": f"找不到排程 {jid}", "is_error": True}
                return {"output": f"已{'恢復' if act == 'resume' else '暫停'} {j['name']}。下次 {_fmt(j['next_run']) if j['enabled'] else '—'}"}
            return {"output": f"不支援的 action：{act}", "is_error": True}
        except ValueError as e:
            return {"output": str(e), "is_error": True}

    woof.add_tool(
        "schedule",
        "管理定時執行的提示詞（排程工作）。每次到時間會開一個新對話執行 prompt。"
        "schedule 用 5 欄 cron（分 時 日 月 週，本機時間），例如每天 9 點＝0 9 * * *、工作日 8:30＝30 8 * * 1-5、每 15 分鐘＝*/15 * * * *。"
        "建立前先跟使用者確認時間與內容。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "delete", "pause", "resume"]},
                "name": {"type": "string", "description": "名稱（create）"},
                "prompt": {"type": "string", "description": "每次執行時要做的事，寫成完整指令（create）"},
                "schedule": {"type": "string", "description": "5 欄 cron 表達式（create）"},
                "model": {"type": "string", "description": "模型 id，省略就用目前對話的模型（create）"},
                "id": {"type": "string", "description": "排程 id（delete / pause / resume）"},
            },
            "required": ["action"],
        },
        schedule,
    )
