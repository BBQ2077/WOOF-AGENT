"""web_search 插件的 web_extract 抓取流程：換家、失敗原因、限流暫停、存檔重新整理（假網路，不連外）。"""
import asyncio
import hashlib
import importlib.util
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path

os.environ["WOOF_DATA"] = os.environ.get("WOOF_DATA") or tempfile.mkdtemp(prefix="woof_test_")
import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "web_search"
PAGE = "# 標題\n\n" + "\n\n".join(f"第{i}段內文，說明一些事情。" for i in range(5))


def load(order, handler):
    spec = importlib.util.spec_from_file_location("ws_t" + str(time.time_ns()), ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = {}
    woof = types.SimpleNamespace(settings={"order": order}, keys=lambda name: types.SimpleNamespace(get=lambda: None),
                                 add_tool=lambda name, desc, schema, fn: tools.__setitem__(name, fn))
    mod.register(woof)
    real = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        real(self, *a, **k)

    async def run(args):
        httpx.AsyncClient.__init__ = init
        try:
            return await tools["web_extract"](args, None)
        finally:
            httpx.AsyncClient.__init__ = real
    return mod, lambda args: asyncio.run(run(args))


def fc_ok(req):
    body = json.loads(req.content)
    return httpx.Response(200, json={"data": {"markdown": PAGE + body["url"], "metadata": {"title": "T"}}})


class Extract(unittest.TestCase):
    def test_per_url_failure_goes_to_next_service(self):
        seen = []

        def h(req):
            body = json.loads(req.content)
            seen.append((req.url.host, body.get("url") or body.get("urls")))
            if "firecrawl" in req.url.host:
                if "blocked" in body["url"]:
                    return httpx.Response(403, json={"success": False, "error": "We do not support this site."})
                return fc_ok(req)
            return httpx.Response(200, json={"results": [{"url": u, "raw_content": PAGE} for u in body["urls"]]})
        mod, run = load("firecrawl_keyless,tavily_keyless", h)
        out = run({"urls": ["https://a.example/1", "https://blocked.example/2"]})
        self.assertFalse(out.get("is_error"))
        self.assertEqual(out["output"].count("第1段內文"), 2)
        self.assertIn(("api.tavily.com", ["https://blocked.example/2"]), seen)   # 只有抓不到的那頁交給下一家
        self.assertNotIn(("firecrawl_keyless", "extract"), mod._COOL)          # 單頁被擋不暫停整個服務

    def test_failure_reasons_and_rate_limit(self):
        def h(req):
            if "firecrawl" in req.url.host:
                return httpx.Response(408, json={"success": False, "code": "SCRAPE_TIMEOUT", "error": "timed out"})
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})
        mod, run = load("firecrawl_keyless,tavily_keyless", h)
        out = run({"urls": ["https://slow.example/"]})
        self.assertTrue(out["is_error"])
        self.assertIn("firecrawl 免 key：逾時", out["output"])
        self.assertIn("tavily 免 key：HTTP 429", out["output"])
        self.assertIn("web_search", out["output"])
        self.assertGreater(mod._COOL[("tavily_keyless", "extract")], time.time() + 100)
        out = run({"urls": ["https://slow2.example/"]})
        self.assertIn("限流中", out["output"])

    def test_pages_fetched_in_parallel_with_short_timeout(self):
        bodies = []

        async def h(req):
            bodies.append(json.loads(req.content))
            await asyncio.sleep(0.3)
            return fc_ok(req)
        mod, run = load("firecrawl_keyless", h)
        t = time.time()
        run({"urls": [f"https://p{i}.example/" for i in range(4)]})
        self.assertLess(time.time() - t, 1.0)
        self.assertLessEqual(bodies[0]["timeout"], 15000)
        self.assertLessEqual(mod._TIMEOUT.read, 20)

    def test_cache_recleaned_when_cleaner_changes(self):
        n = []

        def h(req):
            n.append(1)
            return fc_ok(req)
        mod, run = load("firecrawl_keyless", h)
        u = "https://cache.example/x"
        run({"urls": [u]})
        from woof.config import DATA
        md = DATA / "web" / f"cache.example-{hashlib.sha1(u.encode()).hexdigest()[:10]}.md"
        meta = md.with_suffix(".json")
        m = json.loads(meta.read_text(encoding="utf-8"))
        m["ver"] = "old"
        meta.write_text(json.dumps(m), encoding="utf-8")
        md.write_text("舊的清理結果", encoding="utf-8")
        out = run({"urls": [u]})
        self.assertEqual(len(n), 1)                 # 沒有重抓
        self.assertIn("第1段內文", out["output"])   # 用原始內容重新整理


if __name__ == "__main__":
    unittest.main()
