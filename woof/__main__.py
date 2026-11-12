"""啟動 WOOF 後端。

用法：python -m woof [--port 0] [--token TOKEN] [--parent-pid PID] [--bind local|lan|public]
設定登入密碼：python -m woof set-password
啟動完成後在 stdout 印出一行：WOOF_READY <port>；不能開時印 WOOF_ERROR <原因> 並結束。
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

from . import remote, server
from .config import DATA
from .loader import REG


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
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
    ap.add_argument("--host", default="", help="綁定位址，預設照 [server] bind")
    ap.add_argument("--cert", default="", help="HTTPS 憑證（PEM）")
    ap.add_argument("--key", default="", help="HTTPS 私鑰（PEM）")
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

    st = remote.configure(https_cert=a.cert, https_key=a.key)
    if a.host:
        st["bind"] = "lan" if a.host not in ("127.0.0.1", "localhost", "::1") else "local"
        st["open_mode"] = st["bind"] != "local"
    problem = remote.startup_error(st)
    if problem:
        print(f"WOOF_ERROR {problem}", flush=True)
        logging.error("拒絕啟動：%s", problem)
        raise SystemExit(2)

    server.TOKEN = a.token
    REG.load_all()
    for p in REG.plugins.values():
        logging.info("插件 %s：%s", p.name, "已載入" if p.loaded else (p.error or "停用"))
    server.mount_static()

    if a.parent_pid:
        threading.Thread(target=_watch_parent, args=(a.parent_pid,), daemon=True).start()

    host = a.host or remote.bind_host(st["bind"])
    port = a.port or _free_port(host)
    kwargs = {}
    if st["https_cert"] and st["https_key"]:
        kwargs = {"ssl_certfile": st["https_cert"], "ssl_keyfile": st["https_key"]}
    config = uvicorn.Config(server.app, host=host, port=port, log_level="warning", ws="websockets", **kwargs)
    srv = uvicorn.Server(config)

    async def run():
        task = asyncio.create_task(srv.serve())
        while not srv.started:
            if task.done():
                return
            await asyncio.sleep(0.05)
        scheme = "https" if kwargs else "http"
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        print(f"WOOF_READY {port} {scheme}://{shown}:{port}", flush=True)
        await task

    asyncio.run(run())


def set_password() -> None:
    """python -m woof set-password：設定／改掉登入密碼（輸入時不顯示）。"""
    import getpass

    from . import remote as r

    pw = getpass.getpass("新的登入密碼（留空取消）：")
    if not pw:
        print("沒有設定密碼")
        return
    if len(pw) < 8:
        print("密碼太短：至少 8 個字")
        raise SystemExit(1)
    if pw != getpass.getpass("再輸入一次："):
        print("兩次輸入不一樣")
        raise SystemExit(1)
    r.save_password_hash(r.hash_password(pw))
    print("密碼已存進 config.toml 的 [server] password_hash（只存雜湊）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except Exception:
                pass
        set_password()
    else:
        main()
