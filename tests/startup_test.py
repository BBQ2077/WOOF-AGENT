"""開程式：MCP 套件匯入慢（剛開機硬碟冷）也不擋啟動完成；紀錄檔超過上限就換新的。"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import __main__ as wmain  # noqa: E402

SLOW = r'''
import sys, time, runpy, importlib.abc, importlib.machinery
class Slow(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "mcp":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        real = spec.loader
        class L(importlib.abc.Loader):
            def create_module(self, s): return real.create_module(s)
            def exec_module(self, m):
                time.sleep(6); real.exec_module(m)
        spec.loader = L()
        return spec
sys.meta_path.insert(0, Slow())
sys.argv = ["woof"] + sys.argv[1:]
runpy.run_module("woof", run_name="__main__")
'''


class RotateLog(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_small_kept(self):
        p = self.d / "woof.log"
        p.write_bytes(b"x" * 100)
        wmain._rotate_log(p)
        self.assertEqual(p.read_bytes(), b"x" * 100)
        self.assertFalse((self.d / "woof.old.log").exists())

    def test_big_moved_over_old(self):
        p = self.d / "desktop.log"
        p.write_bytes(b"n" * (wmain.LOG_MAX + 1))
        (self.d / "desktop.old.log").write_bytes(b"older")
        wmain._rotate_log(p)
        self.assertFalse(p.exists())
        self.assertEqual((self.d / "desktop.old.log").stat().st_size, wmain.LOG_MAX + 1)

    def test_missing_or_locked_no_error(self):
        wmain._rotate_log(self.d / "nope.log")
        p = self.d / "woof.log"
        p.write_bytes(b"x" * (wmain.LOG_MAX + 1))
        with open(p, "ab"):                     # Windows：別人開著就改不了名，下次再說
            wmain._rotate_log(p)
        self.assertTrue(p.exists() or (self.d / "woof.old.log").exists())


class SlowMcpImport(unittest.TestCase):
    def test_ready_before_mcp(self):
        import json
        import socket
        import urllib.request

        def free():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        d = Path(tempfile.mkdtemp())
        port, mport = free(), free()
        (d / "config.toml").write_text(f"[mcp]\nport = {mport}\n", encoding="utf-8")
        (d / "slow.py").write_text(SLOW, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(WOOF_DATA=str(d), PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
        t0 = time.time()
        p = subprocess.Popen([sys.executable, str(d / "slow.py"), "--port", str(port), "--token", "t-1",
                              "--host", "127.0.0.1"], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            for line in p.stdout:
                if b"WOOF_READY" in line:
                    break
            self.assertLess(time.time() - t0, 5)          # MCP 匯入要 6 秒，啟動完成不等它

            def info():
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/mcp/info", headers={"x-woof-token": "t-1"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())

            self.assertTrue(info()["url"].endswith(f":{mport}/mcp"))
            end = time.time() + 30
            while not info()["running"] and time.time() < end:
                time.sleep(0.5)
            self.assertTrue(info()["running"])          # 之後在背景起來
        finally:
            p.terminate()
            p.wait(10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
