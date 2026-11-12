"""終端機面板：每個分頁是一個真的 PTY（Windows 用 ConPTY，經 pywinpty）。

- 用 /ws/pty/{id} 傳輸：收到的是鍵盤輸入，送出的是終端輸出；JSON 控制訊息只有 resize。
- 輸出另存一份最近 256 KB，介面重新整理、切換對話後再接上，畫面不會空白。
- 程序放進後端的 Job：後端結束時一起收掉，不會留下孤兒 shell。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import procs

BACKLOG = 256_000
MAX_TERMS = 12

SHELLS = {
    "bash": "Git Bash",
    "powershell": "PowerShell",
    "pwsh": "PowerShell 7",
    "cmd": "命令提示字元",
}


def available_shells() -> dict[str, str]:
    out = {}
    if Path(procs.BASH).exists():
        out["bash"] = SHELLS["bash"]
    if shutil.which("pwsh"):
        out["pwsh"] = SHELLS["pwsh"]
    out["powershell"] = SHELLS["powershell"]
    out["cmd"] = SHELLS["cmd"]
    return out


def _argv(shell: str) -> list[str]:
    if shell == "auto":
        shell = "bash" if Path(procs.BASH).exists() else "powershell"
    if shell == "bash":
        return [procs.BASH, "--login", "-i"]
    if shell == "pwsh":
        return [shutil.which("pwsh") or "pwsh", "-NoLogo"]
    if shell == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe")]
    return ["powershell.exe", "-NoLogo"]


@dataclass
class Term:
    id: str
    session_id: str
    cwd: str
    shell: str
    title: str
    proc: object
    created: float = field(default_factory=time.time)
    buf: str = ""
    alive: bool = True
    exit_code: int | None = None
    listeners: set = field(default_factory=set)

    def info(self) -> dict:
        return {"id": self.id, "session_id": self.session_id, "cwd": self.cwd, "shell": self.shell,
                "title": self.title, "alive": self.alive, "created": self.created}


TERMS: dict[str, Term] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _push(t: Term, data: str) -> None:
    t.buf = (t.buf + data)[-BACKLOG:]
    for q in list(t.listeners):
        q.put_nowait(data)


def _reader(t: Term) -> None:
    p = t.proc
    while True:
        try:
            s = p.read(8192)  # type: ignore[attr-defined]
        except EOFError:
            break
        except Exception:
            break
        if s and _loop:
            _loop.call_soon_threadsafe(_push, t, s)
        elif not s and not p.isalive():  # type: ignore[attr-defined]
            break
    t.alive = False
    try:
        t.exit_code = p.exitstatus  # type: ignore[attr-defined]
    except Exception:
        t.exit_code = None
    if _loop:
        _loop.call_soon_threadsafe(_push, t, f"\r\n\x1b[2m[程序已結束{'' if t.exit_code is None else f'，代碼 {t.exit_code}'}]\x1b[0m\r\n")


def create(session_id: str, cwd: str, shell: str = "auto", cols: int = 100, rows: int = 28) -> Term:
    global _loop
    import winpty

    _loop = asyncio.get_running_loop()
    alive = [t for t in TERMS.values() if t.alive]
    if len(alive) >= MAX_TERMS:
        raise RuntimeError(f"終端機最多同時開 {MAX_TERMS} 個，先關掉幾個")
    if not Path(cwd).is_dir():
        cwd = str(Path.home())
    argv = _argv(shell)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "CHERE_INVOKING": "1"})
    p = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(max(5, rows), max(20, cols)))
    procs.assign_pid(p.pid)
    sh = shell if shell != "auto" else ("bash" if argv[0] == procs.BASH else "powershell")
    n = 1 + sum(1 for t in TERMS.values() if t.session_id == session_id)
    t = Term(uuid.uuid4().hex[:10], session_id, cwd, sh, f"{SHELLS.get(sh, sh)} {n}", p)
    TERMS[t.id] = t
    threading.Thread(target=_reader, args=(t,), daemon=True, name=f"pty-{t.id}").start()
    return t


def write(tid: str, data: str) -> None:
    t = TERMS.get(tid)
    if t and t.alive:
        t.proc.write(data)  # type: ignore[attr-defined]


def resize(tid: str, cols: int, rows: int) -> None:
    t = TERMS.get(tid)
    if t and t.alive:
        try:
            t.proc.setwinsize(max(5, int(rows)), max(20, int(cols)))  # type: ignore[attr-defined]
        except Exception:
            pass


def close(tid: str) -> bool:
    t = TERMS.pop(tid, None)
    if not t:
        return False
    if t.alive:
        try:
            procs.kill_tree(t.proc.pid)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            t.proc.terminate(force=True)  # type: ignore[attr-defined]
        except Exception:
            pass
    t.alive = False
    for q in list(t.listeners):
        q.put_nowait(None)
    return True


def close_session(session_id: str) -> int:
    ids = [k for k, t in TERMS.items() if t.session_id == session_id]
    for k in ids:
        close(k)
    return len(ids)


def close_all() -> None:
    for k in list(TERMS):
        close(k)
