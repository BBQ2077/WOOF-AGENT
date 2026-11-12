"""啟動 WOOF 後端。

用法：python -m woof [--port 0] [--token TOKEN] [--parent-pid PID]
啟動完成後在 stdout 印出一行：WOOF_READY <port>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import threading

import uvicorn

from . import server
from .config import DATA
from .loader import REG


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _watch_parent(pid: int) -> None:
    """父程序（Electron）結束時，後端跟著結束。"""
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            os._exit(0)
        k32.WaitForSingleObject(ctypes.c_void_p(h), 0xFFFFFFFF)
        os._exit(0)
    else:
        import time
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                os._exit(0)
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token", default=os.environ.get("WOOF_TOKEN", ""))
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(DATA / "woof.log", encoding="utf-8"), logging.StreamHandler(sys.stderr)],
    )

    server.TOKEN = a.token
    REG.load_all()
    for p in REG.plugins.values():
        logging.info("插件 %s：%s", p.name, "已載入" if p.loaded else (p.error or "停用"))
    server.mount_static()

    if a.parent_pid:
        threading.Thread(target=_watch_parent, args=(a.parent_pid,), daemon=True).start()

    port = a.port or _free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", ws="websockets")
    srv = uvicorn.Server(config)

    async def run():
        task = asyncio.create_task(srv.serve())
        while not srv.started:
            if task.done():
                return
            await asyncio.sleep(0.05)
        print(f"WOOF_READY {port}", flush=True)
        await task

    asyncio.run(run())


if __name__ == "__main__":
    main()
