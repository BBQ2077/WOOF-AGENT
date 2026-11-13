"""終端機工具：在 Git Bash 執行指令，支援背景任務。"""
from __future__ import annotations

import asyncio
import re
import time

from woof import procs


_PY_HINT_RE = re.compile(r"ModuleNotFoundError|No module named|command not found|不是內部或外部命令", re.I)


def _py_cmd() -> str:
    """給 bash 用的 Python 路徑：正斜線、加引號（資料夾名稱有空白也能直接貼）。"""
    import sys as _sys

    return '"' + _sys.executable.replace("\\", "/") + '"'


def _python_hint(out: str) -> str:
    """缺套件／缺指令時直接給正確的做法（用提示，不去改 PATH）。"""
    if not _PY_HINT_RE.search(out or ""):
        return ""
    import sys as _sys

    return (f"\n（提示：要用 Python 請直接呼叫 {_py_cmd()}（整串連引號照貼）—— 已裝 pandas、openpyxl、numpy；"
            "系統 PATH 上的 python 沒有這些，改叫這一顆就好，不要重裝重試同一支指令。）")


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"…（前面省略 {len(text) - limit} 字）…\n" + text[-limit:]


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


def _notify_on(args: dict) -> bool:
    """背景指令結束要不要送回對話：接續插件的 bg_notify（預設開），模型也可以用 notify=false 關掉這一筆。"""
    if args.get("notify") is False:
        return False
    from woof.loader import REG
    info = REG.plugins.get("continuation")
    if info is None or not info.enabled:
        return False
    v = (info.settings or {}).get("bg_notify")
    return v is None or v is True or str(v).lower() in ("true", "1", "on")


def _arm_notify(task, args: dict, loop) -> None:
    if task.notify or not task.session_id or not _notify_on(args):
        return
    task.notify = True

    async def later():
        await asyncio.sleep(2)  # 模型正在 process wait 的話，它自己會看到結果，就不另外通知
        if task.seen_end:
            return
        task.seen_end = True
        from woof.loader import REG
        inject = getattr(REG, "inject", None)
        if not inject:
            return
        out = _tail(procs.read_log(task), 3000).strip() or "(無輸出)"
        state = "已被停止" if task.killed else f"exit {task.exit_code}"
        from woof.prompts import get as _prompt
        text = _prompt("bg_done", id=task.id, state=state, command=task.command[:300], output=out,
                       elapsed=_fmt_secs((task.ended or time.time()) - task.started))
        await inject(task.session_id, "bg_done", text)

    task.on_exit.append(lambda t: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(later())))


def register(woof):
    def settings_num(key: str, default: int) -> int:
        try:
            return int(woof.settings.get(key) or default)
        except (TypeError, ValueError):
            return default

    async def terminal(args: dict, ctx) -> dict:
        command = (args.get("command") or "").strip()
        if not command:
            return {"output": "command 不能是空的", "is_error": True}
        cwd = args.get("workdir") or ctx.cwd
        # 寫入子代理：工作目錄鎖在主代理給的範圍內，不能開背景常駐（子代理結束後沒人收）
        turn = getattr(ctx, "turn", None)
        sub = bool(turn is not None and (getattr(turn, "session", None) or {}).get("parent"))
        if sub:
            from pathlib import Path as _P
            scope = getattr(turn, "write_scope", None) or []
            if not scope:
                return {"output": "主代理沒有指定寫入範圍，子代理不能執行指令；請回報主代理", "is_error": True}
            if args.get("background"):
                return {"output": "子代理不能開背景常駐的指令；需要長時間執行的請回報主代理處理", "is_error": True}
            wd = _P(cwd).resolve()
            if not any(wd == s or s in wd.parents for s in scope):
                if args.get("workdir"):
                    return {"output": f"工作目錄 {wd} 在範圍外，只能在：" + "、".join(map(str, scope)), "is_error": True}
                first = scope[0]
                wd = first if first.is_dir() else first.parent
            cwd = str(wd)
        timeout = min(int(args.get("timeout") or settings_num("default_timeout", 180)), 600)
        limit = settings_num("max_output", 30000)

        loop = asyncio.get_running_loop()
        pending: list[str] = []

        def on_line(line: str) -> None:
            loop.call_soon_threadsafe(pending.append, line)

        try:
            task = procs.start_command(command, cwd, ctx.session_id, on_line=on_line)
        except Exception as e:
            return {"output": f"無法啟動：{e}", "is_error": True}

        if args.get("background"):
            task.background = True
            _arm_notify(task, args, loop)
            await asyncio.sleep(1.0)
            head = "".join(task.tail[:30])
            tip = "結束時會自動通知你，不用一直查。" if task.notify else "用 process 工具查看進度或結束。"
            return {"output": f"已在背景執行，任務 id={task.id}，PID={task.proc.pid}。\n"
                              f"{tip}\n目前輸出：\n{head}"}

        deadline = time.time() + timeout
        try:
            while task.ended is None and time.time() < deadline:
                await asyncio.sleep(0.25)
                if pending:
                    chunk = "".join(pending)
                    pending.clear()
                    await ctx.output(chunk)
        except asyncio.CancelledError:
            procs.stop_task(task.id)
            raise
        if pending:
            await ctx.output("".join(pending))

        out = procs.read_log(task)
        if task.ended is None and sub:
            procs.stop_task(task.id)
            return {"output": f"超過 {timeout} 秒還沒結束；子代理不能轉背景，已停止。\n最近輸出：\n{_tail(out, 4000)}",
                    "is_error": True}
        if task.ended is None:
            task.background = True
            _arm_notify(task, args, loop)
            tip = "結束時會自動通知你" if task.notify else "用 process 工具查看或結束"
            return {"output": f"超過 {timeout} 秒仍在執行，已轉為背景任務 id={task.id}（PID {task.proc.pid}），{tip}。"
                              f"\n最近輸出：\n{_tail(out, 4000)}"}
        code = task.exit_code
        body = _tail(out, limit) or "(無輸出)"
        return {"output": f"{body}\n[exit {code}]{_python_hint(out)}", "is_error": code != 0}

    async def process(args: dict, ctx) -> dict:
        action = args.get("action", "list")
        tid = args.get("task_id")
        if action == "list":
            for t in procs.TASKS.values():
                if t.background and t.ended is not None:
                    t.seen_end = True
            rows = [t.info() for t in procs.TASKS.values() if t.background]
            if not rows:
                return {"output": "沒有背景任務"}
            return {"output": "\n".join(
                f"{r['id']}  {'執行中' if r['running'] else f'結束 exit={r['exit_code']}'}  PID {r['pid']}  {r['command'][:80]}"
                for r in rows)}
        task = procs.TASKS.get(tid or "")
        if task is None:
            return {"output": f"找不到任務 {tid}", "is_error": True}
        if task.ended is not None and action in ("log", "wait", "stop"):
            task.seen_end = True
        if action == "log":
            return {"output": _tail(procs.read_log(task), int(args.get("limit") or 8000)) or "(無輸出)"}
        if action == "stop":
            task.seen_end = True  # 模型自己停的，不用再通知
            ok = procs.stop_task(task.id)
            return {"output": "已結束整棵程序樹" if ok else "任務早已結束"}
        if action == "wait":
            secs = min(int(args.get("timeout") or 30), 60)
            # 等的時候把新輸出即時送到介面，不然卡片一直空白、看不出在幹嘛
            cmd = str(getattr(task, "command", "") or "")[:120]
            await ctx.output(f"等待 {task.id}（{cmd}）最多 {secs} 秒…\n")
            seen = len(procs.read_log(task))
            done = False
            for _ in range(secs):
                done = await procs.wait_task(task, 1)
                log = procs.read_log(task)
                if len(log) > seen:
                    await ctx.output(log[seen:][-4000:].replace("\r\n", "\n"))
                    seen = len(log)
                if done:
                    break
            if done:
                task.seen_end = True
            state = f"已結束 exit={task.exit_code}" if done else f"{secs} 秒內尚未結束"
            return {"output": f"{state}\n{_tail(procs.read_log(task), 4000)}"}
        return {"output": f"不支援的 action：{action}", "is_error": True}

    import sys as _sys

    _py = _py_cmd()
    woof.add_tool(
        "terminal",
        "在 Git Bash 執行指令（POSIX 語法）。長時間任務設 background=true，或超過 timeout 會自動轉背景；"
        "背景指令結束時會自動通知，不必反覆查。"
        "停止時會結束整棵程序樹。"
        f"要用 Python 時直接呼叫這一顆（已裝 pandas、openpyxl、numpy，整串連引號照貼）：{_py}；"
        "系統 PATH 上的 python 不一定有這些套件，不要為此重複安裝。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要執行的指令"},
                "workdir": {"type": "string", "description": "工作目錄，預設為 WOOF 工作目錄"},
                "timeout": {"type": "integer", "description": "前景等待秒數，上限 600"},
                "background": {"type": "boolean", "description": "直接放到背景執行"},
                "notify": {"type": "boolean", "description": "背景指令結束時自動把結果送回對話（預設 true；不需要時設 false）"},
            },
            "required": ["command"],
        },
        terminal,
    )
    woof.add_tool(
        "process",
        "管理背景任務：list 列出、log 看輸出、wait 等待（最多 60 秒）、stop 結束整棵程序樹。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "log", "wait", "stop"]},
                "task_id": {"type": "string"},
                "timeout": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["action"],
        },
        process,
    )
