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


def load(order, handler, keys=None):
    spec = importlib.util.spec_from_file_location("ws_t" + str(time.time_ns()), ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = {}
    from woof.keypool import KeyEntry, KeyPool
    pools = {n: KeyPool(n, [KeyEntry(k, f"#{i}") for i, k in enumerate(ks)]) for n, ks in (keys or {}).items()}
    woof = types.SimpleNamespace(settings={"order": order}, keys=lambda name: pools.get(name) or KeyPool(name),
                                 add_tool=lambda name, desc, schema, fn: tools.__setitem__(name, fn))
    mod.register(woof)
    real = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        real(self, *a, **k)

    async def run(args):
        httpx.AsyncClient.__init__ = init
        try:
            return await tools[args.pop("_tool", "web_extract")](args, None)
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

    def test_failure_message_is_about_the_page(self):
        def h(req):
            if "firecrawl" in req.url.host:
                return httpx.Response(408, json={"success": False, "code": "SCRAPE_TIMEOUT", "error": "timed out"})
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})
        mod, run = load("firecrawl_keyless,tavily_keyless", h)
        out = run({"urls": ["https://slow.example/"]})
        self.assertFalse(out.get("is_error"))
        self.assertIn("回應太慢或連不上", out["output"])
        for word in ("429", "限流", "firecrawl", "tavily", "key"):
            self.assertNotIn(word, out["output"])
        self.assertGreater(mod._COOL[("tavily_keyless", "extract")], time.time() + 100)

    def test_blocked_site(self):
        def h(req):
            if "firecrawl" in req.url.host:
                return httpx.Response(403, json={"success": False, "error": "We do not support this site."})
            return httpx.Response(200, json={"results": [], "failed_results": [{"url": "https://r.example/", "error": "blocked"}]})
        mod, run = load("firecrawl_keyless,tavily_keyless", h)
        self.assertIn("不讓擷取", run({"urls": ["https://r.example/"]})["output"])

    def test_search_falls_through_and_stays_neutral(self):
        hits = []

        def h(req):
            hits.append(req.url.host)
            if "firecrawl" in req.url.host:
                return httpx.Response(429, json={})
            return httpx.Response(200, json={"results": [{"title": "R", "url": "https://r.example/", "content": "摘要"}]})
        mod, run = load("firecrawl_keyless,tavily_keyless", h)
        self.assertIn("https://r.example/", run({"_tool": "web_search", "query": "x"})["output"])
        mod, run = load("firecrawl_keyless", lambda req: httpx.Response(429, json={}))
        out = run({"_tool": "web_search", "query": "x"})
        self.assertFalse(out.get("is_error"))
        for word in ("429", "key", "額度", "firecrawl"):
            self.assertNotIn(word, out["output"])

    def test_rate_limit_rotates_keys_and_skips_vendor_that_failed_page(self):
        seen = []

        def h(req):
            auth = req.headers.get("authorization", "")
            body = json.loads(req.content)
            seen.append((req.url.host, auth))
            if "firecrawl" in req.url.host:
                return httpx.Response(403, json={"success": False, "error": "We do not support this site."})
            if auth in ("", "Bearer t1"):
                return httpx.Response(429, json={})
            return httpx.Response(200, json={"results": [{"url": u, "raw_content": PAGE} for u in body["urls"]]})
        mod, run = load("firecrawl_keyless,tavily_keyless,firecrawl,tavily", h,
                        {"firecrawl": ["f1"], "tavily": ["t1", "t2"]})
        out = run({"urls": ["https://r.example/"]})
        self.assertIn("第1段內文", out["output"])
        self.assertNotIn(("api.firecrawl.dev", "Bearer f1"), seen)    # Firecrawl 已判定擋站，不再花 key
        self.assertEqual([a for h_, a in seen if "tavily" in h_], ["", "Bearer t1", "Bearer t2"])

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
