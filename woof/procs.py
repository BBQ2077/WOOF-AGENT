"""程序管理：Windows Job Object、整棵程序樹結束、背景任務。

所有由 WOOF 啟動的子程序都放進同一個 Job Object，並設定
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：後端程序一結束（不論正常或被殺），
Windows 會自動結束 Job 內所有程序，不會留下孤兒。
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA

IS_WIN = sys.platform == "win32"
LOGS = DATA / "logs"
LOGS.mkdir(exist_ok=True)

# ---------------------------------------------------------------- Job Object
_job = None

if IS_WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXT_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _make_job():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        return job if ok else None

    _job = _make_job()


def _assign(proc: subprocess.Popen) -> None:
    if IS_WIN and _job is not None:
        try:
            _k32.AssignProcessToJobObject(_job, int(proc._handle))  # type: ignore[attr-defined]
        except Exception:
            pass


def assign_pid(pid: int) -> None:
    """把不是用 Popen 開的程序（例如終端機面板的 PTY）也放進同一個 Job，後端結束時一起收掉。"""
    if IS_WIN and _job is not None and pid:
        h = _k32.OpenProcess(0x0100 | 0x0001, False, int(pid))  # SET_QUOTA | TERMINATE
        if h:
            try:
                _k32.AssignProcessToJobObject(_job, h)
            finally:
                _k32.CloseHandle(h)


def kill_tree(pid: int) -> None:
    """結束整棵程序樹，不只外層 shell。"""
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass


def find_bash() -> str:
    for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(p).exists():
            return p
    return "bash"


BASH = find_bash()


def spawn(argv: list[str], cwd: str | None = None, env: dict | None = None,
          stdin: bool = False) -> subprocess.Popen:
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    e = dict(os.environ)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e["PYTHONUNBUFFERED"] = "1"
    if env:
        e.update(env)
    proc = subprocess.Popen(
        argv, cwd=cwd, env=e,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw,
    )
    _assign(proc)
    return proc


# ---------------------------------------------------------------- 執行指令
@dataclass
class Task:
    id: str
    command: str
    cwd: str
    session_id: str | None
    proc: subprocess.Popen
    log_path: Path
    started: float = field(default_factory=time.time)
    ended: float | None = None
    exit_code: int | None = None
    background: bool = False
    killed: bool = False
    tail: list[str] = field(default_factory=list)
    # 背景指令結束時通知對話（terminal 插件設定）；seen_end＝模型已經看過結束結果，不用再通知
    notify: bool = False
    seen_end: bool = False
    on_exit: list = field(default_factory=list)

    def info(self) -> dict:
        return {
            "id": self.id, "command": self.command, "cwd": self.cwd, "pid": self.proc.pid,
            "session_id": self.session_id, "started": self.started, "ended": self.ended,
            "exit_code": self.exit_code, "background": self.background, "killed": self.killed,
            "running": self.ended is None, "tail": "".join(self.tail[-20:]),
        }


TASKS: dict[str, Task] = {}


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp950"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def start_command(command: str, cwd: str, session_id: str | None = None,
                  on_line=None) -> Task:
    tid = uuid.uuid4().hex[:8]
    log_path = LOGS / f"task_{tid}.log"
    proc = spawn([BASH, "-c", command], cwd=cwd)
    task = Task(id=tid, command=command, cwd=cwd, session_id=session_id, proc=proc, log_path=log_path)
    TASKS[tid] = task

    def reader():
        with log_path.open("w", encoding="utf-8") as lf:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = _decode(raw)
                lf.write(line)
                lf.flush()
                task.tail.append(line)
                if len(task.tail) > 400:
                    del task.tail[:200]
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        task.exit_code = proc.wait()
        task.ended = time.time()
        for cb in list(task.on_exit):
            try:
                cb(task)
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()
    return task


async def wait_task(task: Task, timeout: float) -> bool:
    """等到結束或逾時；回傳是否已結束。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.ended is not None:
            return True
        await asyncio.sleep(0.1)
    return task.ended is not None


def stop_task(tid: str) -> bool:
    t = TASKS.get(tid)
    if not t or t.ended is not None:
        return False
    t.killed = True
    kill_tree(t.proc.pid)
    return True


def stop_session_tasks(session_id: str, include_background: bool = False) -> None:
    for t in list(TASKS.values()):
        if t.session_id == session_id and t.ended is None and (include_background or not t.background):
            stop_task(t.id)


def read_log(task: Task) -> str:
    try:
        return task.log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
