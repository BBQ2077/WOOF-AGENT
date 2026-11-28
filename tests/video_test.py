"""watch_video（plugins/vision/video.py）與 view_page 的逾時處理。不連網：本機影片用 ffmpeg 現做。"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("WOOF_DATA", tempfile.mkdtemp(prefix="woof_test_"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("woof_plugins.vision_t", ROOT / "plugins" / "vision" / "__init__.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)
VD = V.video
FF = shutil.which("ffmpeg")


class Times(unittest.TestCase):
    def test_default_nine_mid_segments(self):
        t = VD.pick_times(90, None, None, None, None)
        self.assertEqual(len(t), 9)
        self.assertEqual(t[0], 5.0)
        self.assertEqual(t[-1], 85.0)

    def test_range_and_cap(self):
        t = VD.pick_times(600, 60, 120, 99, None)
        self.assertEqual(len(t), VD.MAX_FRAMES)
        self.assertTrue(60 <= t[0] and t[-1] < 120)

    def test_explicit_times_clamped_sorted(self):
        self.assertEqual(VD.pick_times(30, None, None, None, [50, 3, 3, -1]), [0.0, 3.0, 29.95])

    def test_bad_range(self):
        with self.assertRaises(ValueError):
            VD.pick_times(30, 40, 50, 3, None)


VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c>hello</c> there

00:00:03.000 --> 00:00:05.000
hello there
next line

00:01:02.500 --> 00:01:04.000
later
"""
SRT = "1\n00:00:02,000 --> 00:00:03,000\n第一句\n\n2\n01:00:00,000 --> 01:00:01,000\n第二句\n"


class Subs(unittest.TestCase):
    def test_vtt_tags_and_rolling_repeats(self):
        self.assertEqual(VD.parse_subs(VTT), [(1.0, "hello there"), (3.0, "next line"), (62.5, "later")])

    def test_srt(self):
        self.assertEqual(VD.parse_subs(SRT), [(2.0, "第一句"), (3600.0, "第二句")])

    def test_range_and_truncate(self):
        d = Path(tempfile.mkdtemp(prefix="woof_test_"))
        (d / "sub.en.vtt").write_text("WEBVTT\n\n" + "".join(
            f"00:{i // 60:02d}:{i % 60:02d}.000 --> 00:{i // 60:02d}:{i % 60:02d}.900\n{'字' * 100}{i}\n\n" for i in range(200)), "utf-8")
        name, text = VD.subs_text(d, 10, 20)
        self.assertEqual(name, "sub.en.vtt")
        self.assertEqual(text.count("\n") + 1, 10)
        _, text = VD.subs_text(d, 0, 1e9)
        self.assertLessEqual(len(text), VD.SUB_CHARS + 50)
        self.assertRegex(text.splitlines()[-1], r"start=\d+ 接著看")
        self.assertEqual(VD.subs_text(Path(tempfile.mkdtemp(prefix="woof_test_")), 0, 9), ("", ""))

    def test_pick_sub_order(self):
        self.assertEqual(VD._pick_sub({"subtitles": {"danmaku": [], "en": [], "zh-Hans": []}}), ("zh-Hans", False))
        self.assertEqual(VD._pick_sub({"subtitles": {"danmaku": []}, "language": "ja",
                                       "automatic_captions": {"ja-orig": [], "en": []}}), ("ja-orig", True))
        self.assertEqual(VD._pick_sub({}), ("", False))


class Sheet(unittest.TestCase):
    def test_grid_and_missing_frame(self):
        from PIL import Image
        im = Image.new("RGB", (400, 225), (200, 0, 0))
        out = VD.sheet([(1, im), (2, im), (3, im), (4, None)])
        self.assertEqual(out.size, (2 * 400 + 4, 2 * 225 + 4))
        self.assertEqual(VD.tile_width(9), 533)
        self.assertEqual(VD.clock(3725), "1:02:05")
        self.assertEqual(VD.clock(65.5), "1:05.5")
        self.assertEqual(VD.clock(12.25), "0:12.25")
        self.assertEqual(VD.clock(3600.05), "1:00:00.05")
        self.assertEqual(VD.clock(59.999), "1:00")
        self.assertEqual(VD.clock(65.5, False), "1:05")


class FakeCDP:
    def __init__(self, script):
        self.script, self.log = list(script), []

    def expect(self, method):
        f = asyncio.get_running_loop().create_future()
        f.set_result({})
        return f

    async def call(self, method, params=None, timeout=30):
        self.log.append(method)
        if method.startswith("Emulation"):
            return {}
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class PageSteps(unittest.TestCase):
    def test_eval_reruns_after_navigation(self):
        c = FakeCDP([RuntimeError("Inspected target navigated or closed"), {"result": {"value": 1}}])
        self.assertEqual(asyncio.run(V._eval(c, "1")), {"result": {"value": 1}})
        self.assertEqual(c.log, ["Runtime.evaluate", "Runtime.evaluate"])

    def test_eval_other_errors_not_retried(self):
        c = FakeCDP([V.StepTimeout("執行 js超過 20 秒沒回應")])
        with self.assertRaises(V.StepTimeout):
            asyncio.run(V._eval(c, "1"))
        self.assertEqual(len(c.log), 1)

    def test_capture_repaints_once(self):
        c = FakeCDP([V.StepTimeout("x"), {"data": "PNG"}])
        self.assertEqual(asyncio.run(V._capture(c, {}, 1000, 800)), "PNG")
        self.assertEqual(c.log, ["Page.captureScreenshot", "Emulation.setDeviceMetricsOverride",
                                 "Emulation.setDeviceMetricsOverride", "Page.captureScreenshot"])

    def test_call_timeout_names_step(self):
        class WS:
            async def send(self, _):
                pass

        async def go():
            await V._CDP(WS()).call("Page.captureScreenshot", {}, timeout=0.05)
        with self.assertRaisesRegex(V.StepTimeout, "截圖超過 0.05 秒"):
            asyncio.run(go())


class Retention(unittest.TestCase):
    def test_video_kept_one_day(self):
        from woof import retention
        from woof.config import DATA
        old, new = DATA / "spill" / "video" / "aaa", DATA / "spill" / "video" / "bbb"
        for d in (old, new):
            d.mkdir(parents=True, exist_ok=True)
            (d / "video.mp4").write_bytes(b"x")
        t = time.time() - 2 * 86400
        os.utime(old / "video.mp4", (t, t))
        retention._drop_files(retention._files(time.time() - 7 * 86400))
        self.assertFalse(old.exists())
        self.assertTrue((new / "video.mp4").exists())


@unittest.skipUnless(FF, "沒有 ffmpeg")
class WatchLocal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vid = Path(tempfile.mkdtemp(prefix="woof_test_")) / "t.mp4"
        subprocess.run([FF, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=640x360:rate=10",
                        "-pix_fmt", "yuv420p", str(cls.vid)], check=True)

        class W:
            settings, tools = {}, {}

            def add_tool(self, name, desc, schema, fn, **k):
                self.tools[name] = (desc, schema, fn)

            def __getattr__(self, n):
                return lambda *a, **k: None
        cls.w = W()
        V.register(cls.w)

    def run_tool(self, args):
        class Ctx:
            cwd, call_id, turn = ".", "t", None

            async def output(self, c):
                pass
        V._native_model = lambda woof, m: {"id": "x"}
        return asyncio.run(self.w.tools["watch_video"][2](args, Ctx()))

    def test_short_description(self):
        self.assertLess(len(self.w.tools["watch_video"][0]), 160)

    def test_frames_sheet(self):
        r = self.run_tool({"path": str(self.vid), "frames": 4})
        self.assertFalse(r.get("is_error"), r["output"])
        self.assertIn("0:12｜640×360", r["output"])
        self.assertIn("4 格", r["output"])
        self.assertEqual(len(r["images"]), 1)

    def test_errors(self):
        self.assertTrue(self.run_tool({})["is_error"])
        self.assertTrue(self.run_tool({"path": str(self.vid), "start": 30})["is_error"])
        self.assertTrue(self.run_tool({"url": "file:///c:/x.mp4"})["is_error"])
        r = self.run_tool({"path": str(self.vid.with_suffix(".txt"))})
        self.assertIn("找不到檔案", r["output"])


class _Srv:
    """本機 HTTP：ranged＝支援分段（drop＝前幾個分段請求送一半就斷線）；否則一律回整個檔案"""

    def __init__(self, data: bytes, ranged: bool = True, drop: int = 0):
        import http.server
        import threading
        srv = self
        self.data, self.ranged, self.drop, self.ranges = data, ranged, drop, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                m = __import__("re").fullmatch(r"bytes=(\d+)-(\d*)", rng)
                if not (srv.ranged and m):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(srv.data)))
                    self.end_headers()
                    self.wfile.write(srv.data)
                    return
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else len(srv.data) - 1
                srv.ranges.append((a, b))
                body = srv.data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(srv.data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if b > 0 and srv.drop > 0:
                    srv.drop -= 1
                    self.wfile.write(body[:len(body) // 2])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v.mp4"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Download(unittest.TestCase):
    """分段平行下載：慢的連線不拖住別段、斷線從斷點接著要、不支援分段改走 yt-dlp"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="woof_test_dl_"))
        self.data = os.urandom(10 * (1 << 20) + 12345)
        self.said = []
        self.old = VD.PIECE
        VD.PIECE = 1 << 20

    def tearDown(self):
        VD.PIECE = self.old
        shutil.rmtree(self.d, ignore_errors=True)

    async def say(self, t):
        self.said.append(t)

    def test_parallel_pieces_and_resume_after_drop(self):
        s = _Srv(self.data, drop=3)
        try:
            asyncio.run(VD.download(s.url, {"Range": "bytes=0-"}, self.d / "video.mp4", self.say))
        finally:
            s.close()
        self.assertEqual((self.d / "video.mp4").read_bytes(), self.data)
        self.assertFalse((self.d / "video.mp4.part").exists())
        self.assertGreaterEqual(len({a for a, b in s.ranges if b - a + 1 == VD.PIECE}), 8)   # 一段一段分開要
        self.assertTrue(any(a % VD.PIECE for a, _ in s.ranges[1:]))                          # 斷線的段從斷點接著要
        self.assertTrue(any(x.startswith("下載 ") for x in self.said))

    def test_no_range_raises(self):
        s = _Srv(self.data, ranged=False)
        try:
            with self.assertRaises(VD._NoRange):
                asyncio.run(VD.download(s.url, {}, self.d / "video.mp4", self.say))
        finally:
            s.close()
        self.assertFalse(any(self.d.iterdir()))

    def _fetch(self, ranged):
        import hashlib
        import json
        s = _Srv(self.data, ranged=ranged)
        d = self.d / hashlib.sha1(s.url.encode()).hexdigest()[:12]
        info = {"url": s.url, "protocol": "http", "ext": "mp4", "http_headers": {}, "extractor_key": "Generic"}
        calls = []

        async def run(cmd, timeout, on_line=None, idle=None):
            calls.append(cmd)
            if "-J" in cmd:
                return 0, json.dumps(info), ""
            (d / "video.mp4").write_bytes(b"from-ytdlp")
            return 0, "", ""

        old = VD._ytdlp, VD._run
        VD._ytdlp, VD._run = (lambda: ["yt-dlp"]), run
        try:
            path, _ = asyncio.run(VD.fetch(s.url, self.d, self.say))
        finally:
            VD._ytdlp, VD._run = old
            s.close()
        return path, calls

    def test_fetch_uses_parallel_for_plain_file(self):
        path, calls = self._fetch(True)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(len(calls), 1)                                   # 只問資訊，下載不經 yt-dlp
        self.assertEqual(calls[0][calls[0].index("-f") + 1], VD.FMT)      # 問資訊時就選好畫質

    def test_fetch_falls_back_to_ytdlp_without_range(self):
        path, calls = self._fetch(False)
        self.assertEqual(path.read_bytes(), b"from-ytdlp")
        self.assertTrue(any("伺服器不支援分段" in x for x in self.said))
        self.assertIn("-N", calls[-1])                                    # 切片格式時 yt-dlp 平行抓


class Idle(unittest.TestCase):
    def test_no_output_for_idle_seconds_stops(self):
        cmd = [sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"]
        t = time.time()
        with self.assertRaises(TimeoutError):
            asyncio.run(VD._run(cmd, 60, None, 1))
        self.assertLess(time.time() - t, 15)


if __name__ == "__main__":
    unittest.main()
