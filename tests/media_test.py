"""生成圖片、影片：金鑰池輪換（假的 HTTP，不花錢）"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("WOOF_DATA", tempfile.mkdtemp(prefix="woof_test_media_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from woof import keypool, media  # noqa: E402

M = {"id": "agnes-image-2.5-flash", "label": "agnes-image-2.5-flash", "base_url": "https://api.agnes-ai.com/v1",
     "key_pool": "agnes_t"}
PNG = b"\x89PNG\r\n\x1a\nfake"


class Woof:
    def __init__(self, n):
        self.pool = keypool.KeyPool("agnes_t", [keypool.KeyEntry(f"k{i}", f"#{i + 1}") for i in range(n)])

    def keys(self, name):
        return self.pool


class Rotate(unittest.TestCase):
    def setUp(self):
        self.seen = []
        real = httpx.AsyncClient

        def client(**kw):
            kw.pop("transport", None)
            return real(transport=httpx.MockTransport(self.handle), **kw)
        self._real, media.httpx.AsyncClient = real, client

    def tearDown(self):
        media.httpx.AsyncClient = self._real

    def handle(self, req):
        if req.url.path.endswith("/img.png"):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        key = req.headers["authorization"].split()[-1]
        self.seen.append(key)
        if key in self.bad:
            return httpx.Response(429, json={"error": {"message": "You've reached the API rate limit for free users."}})
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/img.png"}]})

    def test_rate_limited_key_skipped(self):
        self.bad = {"k0", "k1"}
        w = Woof(11)
        r = asyncio.run(media.gen_image(w, M, "一隻狗", session_id="t"))
        self.assertEqual(self.seen, ["k0", "k1", "k2"])          # 前兩把限流，換到第三把就生成
        self.assertEqual(Path(r["path"]).read_bytes(), PNG)
        self.assertEqual([e.status for e in w.pool.entries[:3]], ["rate_limited", "rate_limited", "ok"])
        self.seen.clear()
        asyncio.run(media.gen_image(w, M, "再一張", session_id="t"))
        self.assertEqual(self.seen, ["k2"])                        # 冷卻中的不再送

    def test_all_limited_says_so(self):
        self.bad = {f"k{i}" for i in range(3)}
        w = Woof(3)
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(media.gen_image(w, M, "一隻狗", session_id="t"))
        msg = str(cm.exception)
        self.assertEqual(len(self.seen), 3)
        self.assertIn("3 把 key 這次試了 3 把都不能用", msg)
        self.assertIn("恢復", msg)
        self.assertIn("HTTP 429", msg)

    def test_cooldown_expires(self):
        self.bad = set()
        w = Woof(2)
        w.pool.mark(w.pool.entries[0], "rate_limited", time.time() - 1)
        asyncio.run(media.gen_image(w, M, "一隻狗", session_id="t"))
        self.assertEqual(self.seen, ["k0"])                        # 冷卻過了回到第一把


if __name__ == "__main__":
    unittest.main()
