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
os.environ.setdefault("WOOF_DATA", tempfile.mkdtemp(prefix="woof_test_"))  # 不碰真正的設定和對話資料庫

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


def free():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpSwitch(unittest.TestCase):
    """設定 → 進階 → MCP 伺服器：預設關閉，開關當場生效（另開後端，不在測試程序裡跑啟動流程）。"""

    def test_default_off_then_toggle(self):
        import json
        import socket
        import urllib.request

        d = Path(tempfile.mkdtemp())
        port, mport = free(), free()
        (d / "config.toml").write_text(f"[mcp]\nport = {mport}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(WOOF_DATA=str(d), PYTHONIOENCODING="utf-8")
        p = subprocess.Popen([sys.executable, "-m", "woof", "--port", str(port), "--token", "t-1", "--host", "127.0.0.1"],
                             cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def call(method, path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"x-woof-token": "t-1", "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())

        def mcp_enabled_in_config():
            import tomllib

            return tomllib.loads((d / "config.toml").read_text(encoding="utf-8")).get("mcp", {}).get("enabled")

        try:
            for line in p.stdout:
                if b"WOOF_READY" in line:
                    break
            i = call("GET", "/api/mcp/info")
            self.assertFalse(i["enabled"])
            self.assertFalse(i["running"])
            self.assertTrue(i["url"].endswith(f":{mport}/mcp"))

            i = call("PUT", "/api/mcp/enabled", {"enabled": True})
            self.assertTrue(i["enabled"])
            self.assertTrue(i["running"], i)
            self.assertIs(mcp_enabled_in_config(), True)
            with socket.socket() as s:                           # 埠在聽
                self.assertEqual(s.connect_ex(("127.0.0.1", mport)), 0)

            i = call("PUT", "/api/mcp/enabled", {"enabled": False})
            self.assertFalse(i["enabled"])
            self.assertFalse(i["running"])
            self.assertIs(mcp_enabled_in_config(), False)
            with socket.socket() as s:                           # 埠放開了
                s.bind(("127.0.0.1", mport))

            i = call("PUT", "/api/mcp/enabled", {"enabled": True})   # 關了再開也行
            self.assertTrue(i["running"], i)
        finally:
            p.terminate()
            p.wait(10)


class DefaultOffNoImport(unittest.TestCase):
    def test_mcp_package_not_loaded(self):
        d = Path(tempfile.mkdtemp())
        code = ("import sys,runpy,threading,time,os\n"
                "def chk():\n"
                "    time.sleep(4); print('MCPLOADED', 'mcp.server' in sys.modules, flush=True); os._exit(0)\n"
                "threading.Thread(target=chk, daemon=True).start()\n"
                "sys.argv=['woof','--port',sys.argv[1],'--token','t-1','--host','127.0.0.1']\n"
                "runpy.run_module('woof', run_name='__main__')\n")
        (d / "chk.py").write_text(code, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(WOOF_DATA=str(d), PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
        out = subprocess.run([sys.executable, str(d / "chk.py"), str(free())], cwd=ROOT, env=env,
                             capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        self.assertIn("WOOF_READY", out)
        self.assertIn("MCPLOADED False", out)


class SlowMcpImport(unittest.TestCase):
    def test_ready_before_mcp(self):
        import json
        import urllib.request

        d = Path(tempfile.mkdtemp())
        port, mport = free(), free()
        (d / "config.toml").write_text(f"[mcp]\nenabled = true\nport = {mport}\n", encoding="utf-8")
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
