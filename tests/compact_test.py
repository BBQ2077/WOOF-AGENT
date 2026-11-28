"""壓縮插件：行號編輯指令、附的原話、按 token 切保留區、整段壓縮流程（假模型，不連外）。"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

TMP = os.environ.get("WOOF_DATA") or tempfile.mkdtemp(prefix="woof_test_")
os.environ["WOOF_DATA"] = TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import store  # noqa: E402
from woof.agent import loop  # noqa: E402

spec = importlib.util.spec_from_file_location("ctx_plugin_t", ROOT / "plugins" / "context" / "__init__.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

PREV = "\n".join([
    "## 使用者的目標與要求",
    "- 做 A",
    "## 已完成",
    "1. 改了 a.py",
    "2. 跑測試全過",
    "## 壓縮當時進行中",
    "- 在改 b.py",
])


class Edits(unittest.TestCase):
    def test_replace_delete_insert(self):
        out = "@@ 插 5\n3. 改好 b.py\n@@ 改 7\n- 在寫 README\n@@ 刪 2"
        self.assertEqual(C._apply_edits(PREV, out), "\n".join([
            "## 使用者的目標與要求", "## 已完成", "1. 改了 a.py", "2. 跑測試全過", "3. 改好 b.py",
            "## 壓縮當時進行中", "- 在寫 README"]))

    def test_insert_top_unchanged_and_prefix(self):
        self.assertEqual(C._apply_edits(PREV, "@@ 不變"), PREV)
        out = C._apply_edits(PREV, "```\n@@ 插 0\n1| # 交接\n```")
        self.assertTrue(out.startswith("# 交接\n## 使用者的目標與要求"))

    def test_insert_after_replaced_range(self):
        out = C._apply_edits(PREV, "@@ 改 6-7\n## 壓縮當時進行中\n- 沒有\n@@ 插 7\n## 當時還沒做完的\n- 發版")
        self.assertTrue(out.endswith("## 壓縮當時進行中\n- 沒有\n## 當時還沒做完的\n- 發版"))

    def test_rejects_bad_output(self):
        for bad in ("這是新的摘要全文", "@@ 改 9\nx", "@@ 刪 2-4\n@@ 改 3\nx", "@@ 插 3", "說明\n@@ 刪 2", "@@ 不變\n@@ 刪 2"):
            self.assertIsNone(C._apply_edits(PREV, bad), bad)
        long = "\n".join(f"{i}. 很長的一行內容，記錄做過的事情與結果，" * 3 for i in range(60))
        self.assertIsNone(C._apply_edits(long, "@@ 刪 1-50"))


class Quotes(unittest.TestCase):
    def msgs(self):
        out, i = [], 0
        for text in ["第一個要求" * 30, "好", "第三個要求", "照這樣", "最後的要求" * 10]:
            i += 1
            out.append({"id": i, "role": "assistant", "content": [{"type": "text", "text": f"提議{i}：" + "內容" * 200}]})
            i += 1
            out.append({"id": i, "role": "user", "content": [{"type": "text", "text": text}], "created": 1795666632})
        return out

    def test_latest_few_with_cap(self):
        s = C._users_section(self.msgs(), 99, 3, 2000)
        self.assertEqual(s.count("使用者："), 3)
        self.assertIn("最後的要求", s)
        self.assertNotIn("第一個要求", s)
        self.assertEqual(s.count("助手前一則（開頭）：提議"), 3)   # 每則都附它回應的那則開頭
        self.assertNotIn("提議1", s)
        self.assertIn("內容" * 50, s)
        self.assertNotIn("內容" * 200, s)                       # 只附開頭 250 字
        self.assertNotIn("助手前一則", C._users_section(self.msgs(), 99, 3, 2000, 0))
        self.assertLessEqual(len(C._users_section(self.msgs(), 99, 20, 300)), 900)
        self.assertEqual(C._users_section(self.msgs(), 99, 0, 2000), "")


def big(n):
    return "資料內容 " * n


class Calib(unittest.TestCase):
    def test_ratio_from_real_report(self):
        from woof import tokens
        s = store.create_session("m", None, "t")
        last = store.add_message(s["id"], "user", [{"type": "text", "text": "中文內容" * 3000}])
        es = {}
        est = tokens._estimate_all(s["id"], es, last["id"])
        store.update_session(s["id"], engine_state={"usage": {"input_tokens": int(est * 1.5), "through": last["id"]}})
        self.assertAlmostEqual(tokens.calib(s["id"]), 1.5, places=2)
        store.update_session(s["id"], engine_state={"usage": {"input_tokens": est * 9, "through": last["id"]}})
        self.assertEqual(tokens.calib(s["id"]), 2.5)                  # 太離譜的夾在 2.5
        store.update_session(s["id"], engine_state={"tok_k": 1.3, "usage": {"input_tokens": 5, "estimated": True}})
        self.assertEqual(tokens.calib(s["id"]), 1.3)                  # 估計值不拿來校正，沿用上次的


class Flow(unittest.TestCase):
    def setUp(self):
        self.hooks = {}
        self.fake = types.SimpleNamespace(settings={"trigger": 50, "tail_tokens": 3000, "quote_count": 2},
                                          on=lambda ev, fn, **k: self.hooks.setdefault(ev, fn))
        C.register(self.fake)
        s = store.create_session("m", None, "t")
        self.sid = s["id"]
        add = lambda role, content: store.add_message(self.sid, role, content)  # noqa: E731
        add("user", [{"type": "text", "text": "最早的要求"}])
        for r in range(4):
            add("assistant", [{"type": "tool_call", "id": f"c{r}", "name": "read_file", "args": {"path": f"f{r}"}}])
            add("tool", [{"type": "tool_result", "id": f"c{r}", "name": "read_file", "output": big(1500)}])
            add("assistant", [{"type": "text", "text": f"第 {r} 步做完"}])
            last = add("user", [{"type": "text", "text": f"第 {r} 個追加要求"}])
        es = {"summary": {"through": 0, "text": PREV}, "tok_k": 1.0,
              "usage": {"context_used": 60000, "input_tokens": 60000, "through": last["id"], "estimated": True}}
        store.update_session(self.sid, engine_state=es)
        self.events = []

        async def emit(ev):
            self.events.append(ev)
        self.ctx = types.SimpleNamespace(session_id=self.sid, emit=emit, force_compact=True, manual=True,
                                         model_cfg={"id": "m", "provider": "anthropic", "context_window": 100000},
                                         registry=types.SimpleNamespace(providers={"anthropic": object()}, tools={}))

    def run_with(self, replies):
        calls = []

        async def complete(model, system, messages, *a, **k):
            calls.append((system, messages[-1]["content"][0]["text"]))
            return replies.pop(0)
        with unittest.mock.patch.object(loop, "complete", complete), \
                unittest.mock.patch.object(loop, "aux_model", lambda task, fb: None):
            asyncio.run(self.hooks["force_compact"](self.ctx))
        return calls

    def test_edit_applied_tail_by_tokens_quotes_stored(self):
        calls = self.run_with(["@@ 插 5\n3. 讀完 f0 到 f2\n@@ 改 7\n- 在讀 f3"])
        self.assertEqual(len(calls), 1)
        self.assertIn("1| ## 使用者的目標與要求", calls[0][1])          # 舊摘要附行號
        self.assertIn("@@ 改", calls[0][0])
        summ = store.get_session(self.sid)["engine_state"]["summary"]
        self.assertIn("3. 讀完 f0 到 f2", summ["text"])
        self.assertIn("- 做 A", summ["text"])                            # 沒提到的行原樣留著
        self.assertEqual(summ["quotes"].count("使用者："), 2)
        msgs = store.get_messages(self.sid)
        kept = [m for m in msgs if m["id"] > summ["through"]]
        from woof import tokens
        self.assertLessEqual(tokens.estimate_messages(kept), 3000)
        self.assertTrue(kept)
        sent = self.hooks["before_request"]([{"id": m["id"], "role": m["role"], "content": m["content"], "meta": {}}
                                             for m in msgs], self.ctx)
        self.assertIn(summ["quotes"], sent[0]["content"][0]["text"])

    def test_unreadable_edit_falls_back_to_full_rewrite(self):
        calls = self.run_with(["我把摘要整理如下：……", "## 已完成\n1. 全部重寫的版本"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(store.get_session(self.sid)["engine_state"]["summary"]["text"], "## 已完成\n1. 全部重寫的版本")
        self.assertTrue(any("整份重寫" in (e.get("text") or "") for e in self.events))


    def test_cached_path_uses_edits(self):
        import time
        last = store.get_messages(self.sid)[-1]["id"]
        snap = {"model": "m", "at": time.time(), "sig": "s", "through": last, "system": "系統",
                "specs": [], "effort": None,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "前綴"}]}]}
        calls = []

        async def complete(model, system, messages, *a, **k):
            calls.append((system, messages, k.get("kind")))
            return "@@ 改 7\n- 在讀 f3"
        with unittest.mock.patch.dict(loop.LAST_REQ, {self.sid: snap}), \
                unittest.mock.patch.object(loop, "req_sig", lambda sid: "s"), \
                unittest.mock.patch.object(loop, "complete", complete), \
                unittest.mock.patch.object(loop, "aux_model", lambda task, fb: None):
            self.ctx.force_compact = False          # 手動壓縮但上下文沒爆：可以沿用快取
            asyncio.run(self.hooks["force_compact"](self.ctx))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "系統")                       # 系統提示詞跟上一步一樣，前綴吃快取
        self.assertEqual(calls[0][1][0]["content"][0]["text"], "前綴")
        ask = calls[0][1][-1]["content"][0]["text"]
        self.assertIn("7| - 在改 b.py", ask)
        summ = store.get_session(self.sid)["engine_state"]["summary"]
        self.assertEqual(summ.get("how"), "cached")
        self.assertTrue(summ["text"].endswith("## 壓縮當時進行中\n- 在讀 f3"))
        self.assertIn("2. 跑測試全過", summ["text"])


class NotSummary(unittest.TestCase):
    """摘要模型沒寫摘要（照紀錄寫出工具呼叫、太短）：重寫一次；兩次都不行就不存"""
    run_with = Flow.run_with

    def setUp(self):
        Flow.setUp(self)
        es = dict(store.get_session(self.sid)["engine_state"])
        es.pop("summary")                                 # 第一次壓縮
        store.update_session(self.sid, engine_state=es)

    FAKE = "<tool_call>\n[呼叫 terminal] cd C:/x && ls\n</tool_call>"

    def test_transcript_wrapped_and_asked_again(self):
        good = "## 已完成\n" + "1. 讀完 f0 到 f3，內容是資料。\n" * 20
        calls = self.run_with([good])
        text = calls[0][1]
        self.assertTrue(text.startswith(C._BEGIN))
        self.assertTrue(text.endswith(C._ASK_AGAIN))
        self.assertIn(C._END, text)

    def test_fake_call_rewritten_once(self):
        good = "## 已完成\n" + "1. 讀完 f0 到 f3，內容是資料。\n" * 20
        calls = self.run_with([self.FAKE, good])
        self.assertEqual(len(calls), 2)
        self.assertEqual(store.get_session(self.sid)["engine_state"]["summary"]["text"], good.strip())
        self.assertTrue(any("寫成了工具呼叫" in (e.get("text") or "") for e in self.events))

    def test_two_bad_answers_store_nothing(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_with([self.FAKE, "好的，我來看看。"])
        self.assertIn("只寫了", str(cm.exception))
        self.assertNotIn("summary", store.get_session(self.sid)["engine_state"])

    def test_stored_fake_summary_ignored(self):
        from woof import tokens
        es = dict(store.get_session(self.sid)["engine_state"])
        es["summary"] = {"through": 5, "text": self.FAKE}
        store.update_session(self.sid, engine_state=es)
        self.assertEqual(tokens.summary_of(es), {})
        self.assertTrue(tokens.fake_call("  [呼叫 read_file] a.py"))
        self.assertFalse(tokens.fake_call("## 已完成\n1. [呼叫 x] 之後改好"))
        msgs = store.get_messages(self.sid)
        sent = self.hooks["before_request"]([{"id": m["id"], "role": m["role"], "content": m["content"], "meta": {}}
                                             for m in msgs], self.ctx)
        self.assertIsNone(sent)                           # 整段原文照送
        good = "## 已完成\n" + "1. 讀完 f0 到 f3，內容是資料。\n" * 20
        calls = self.run_with([good])
        self.assertNotIn("先前摘要", calls[0][1])          # 從頭重寫，不拿壞的當舊摘要
        summ = store.get_session(self.sid)["engine_state"]["summary"]
        self.assertEqual(summ["text"], good.strip())


class Background(unittest.TestCase):
    """超過門檻：交給背景寫摘要，對話不等；到等待線才等"""

    def setUp(self):
        Flow.setUp(self)
        self.ctx.force_compact, self.ctx.manual, self.ctx.mid_turn = False, False, True
        C._bg.clear()

    def set_used(self, n):
        es = dict(store.get_session(self.sid)["engine_state"])
        es["usage"] = dict(es["usage"], context_used=n, input_tokens=n)
        store.update_session(self.sid, engine_state=es)

    def run_bg(self, used, wait_at=None):
        if wait_at is not None:
            self.fake.settings["wait_at"] = wait_at
        self.set_used(used)
        gate, out = asyncio.Event(), {}

        async def complete(model, system, messages, *a, **k):
            await gate.wait()
            return "@@ 插 5\n3. 背景寫的摘要"

        async def main():
            with unittest.mock.patch.object(loop, "complete", complete), \
                    unittest.mock.patch.object(loop, "aux_model", lambda task, fb: None):
                step = asyncio.create_task(self.hooks["mid_turn"](self.ctx))
                await asyncio.sleep(0.05)
                out["returned_before_summary"] = step.done()
                out["bg_running"] = bool(C._bg.get(self.sid)) and not C._bg[self.sid].done()
                gate.set()
                out["result"] = await step
                if C._bg.get(self.sid):
                    await C._bg[self.sid]
        asyncio.run(main())
        return out

    def test_over_trigger_runs_in_background(self):
        out = self.run_bg(60000)
        self.assertTrue(out["returned_before_summary"])      # 這一步照常送，不等摘要
        self.assertTrue(out["bg_running"])
        self.assertFalse(out["result"])
        summ = store.get_session(self.sid)["engine_state"]["summary"]
        self.assertIn("背景寫的摘要", summ["text"])           # 寫好後存起來，下一次送出就用
        self.assertTrue(any("在背景寫摘要" in (e.get("text") or "") for e in self.events))

    def test_over_wait_line_waits(self):
        out = self.run_bg(95000)
        self.assertFalse(out["returned_before_summary"])     # 快爆了：停下來等
        self.assertTrue(out["result"])
        self.assertIn("背景寫的摘要", store.get_session(self.sid)["engine_state"]["summary"]["text"])

    def test_overflow_while_background_running_uses_it(self):
        self.set_used(60000)
        gate, n = asyncio.Event(), []

        async def complete(model, system, messages, *a, **k):
            n.append(1)
            await gate.wait()
            return "@@ 插 5\n3. 背景寫的摘要"

        async def main():
            with unittest.mock.patch.object(loop, "complete", complete), \
                    unittest.mock.patch.object(loop, "aux_model", lambda task, fb: None):
                await self.hooks["mid_turn"](self.ctx)           # 開始背景壓縮
                await asyncio.sleep(0.05)
                self.ctx.force_compact = True                     # 這時模型回報上下文爆了
                forced = asyncio.create_task(self.hooks["mid_turn"](self.ctx))
                await asyncio.sleep(0.05)
                self.assertFalse(forced.done())                   # 等背景那份
                gate.set()
                return await forced
        self.assertTrue(asyncio.run(main()))
        self.assertEqual(len(n), 1)                               # 只寫一次摘要
        self.assertEqual(store.get_session(self.sid)["engine_state"]["compress_count"], 1)

    def test_under_trigger_nothing(self):
        out = self.run_bg(30000)
        self.assertFalse(out["result"])
        self.assertFalse(out["bg_running"])
        self.assertIn("做 A", store.get_session(self.sid)["engine_state"]["summary"]["text"])


if __name__ == "__main__":
    unittest.main()
