"""壓縮插件：行號編輯指令、附的原話、按 token 切保留區、整段壓縮流程（假模型，不連外）。
寫摘要只有一種做法：接在下一步本來就要送的內容後面（前面吃快取）；第一次寫整份，之後只寫要改的行。"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

TMP = os.environ.get("WOOF_DATA") or tempfile.mkdtemp(prefix="woof_test_")
os.environ["WOOF_DATA"] = TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import store, tokens  # noqa: E402
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
GOOD = "## 已完成\n" + "1. 讀完 f0 到 f3，內容是資料。\n" * 20
FAKE = "<tool_call>\n[呼叫 terminal] cd C:/x && ls\n</tool_call>"


def edit(prev, out):
    return C._apply_edits(prev, out)[0]


class Edits(unittest.TestCase):
    def test_replace_delete_insert(self):
        out = "@@ 插 5\n3. 改好 b.py\n@@ 改 7\n- 在寫 README\n@@ 刪 2\n@@ 完"
        self.assertEqual(edit(PREV, out), "\n".join([
            "## 使用者的目標與要求", "## 已完成", "1. 改了 a.py", "2. 跑測試全過", "3. 改好 b.py",
            "## 壓縮當時進行中", "- 在寫 README"]))

    def test_unchanged_prefix_and_chatter_ignored(self):
        self.assertEqual(edit(PREV, "@@ 不變"), PREV)
        out = edit(PREV, "好的，以下是修改：\n```\n@@ 插 0\n1| # 交接\n```\n以上。")
        self.assertTrue(out.startswith("# 交接\n## 使用者的目標與要求"))   # 前面的說明、``` 、行號前綴都略過
        self.assertNotIn("以上", out)
        self.assertNotIn("好的", out)
        out = edit(PREV, "@@ 插 5\n3. 改好 b.py\n@@ 完\n以上是這次的修改。")
        self.assertNotIn("以上", out)                                   # @@ 完 後面的都不算

    def test_trailing_chatter_without_end_rejected(self):
        text, why = C._apply_edits(PREV, "@@ 插 5\n3. 改好 b.py\n以上是這次的修改。")
        self.assertIsNone(text)                                         # 分不出是內容還是說明：不採用，不會塞進摘要
        self.assertIn("@@ 完", why)

    def test_insert_after_replaced_range(self):
        out = edit(PREV, "@@ 改 6-7\n## 壓縮當時進行中\n- 沒有\n@@ 插 7\n## 當時還沒做完的\n- 發版\n@@ 完")
        self.assertTrue(out.endswith("## 壓縮當時進行中\n- 沒有\n## 當時還沒做完的\n- 發版"))

    def test_new_section_appended_past_end(self):
        out = edit(PREV, "@@ 插 99\n## 重要的檔案、指令與數據\n- plugins/context/__init__.py\n- 測試 317 個\n@@ 完")
        self.assertTrue(out.startswith(PREV))                          # 原本的全部留著
        self.assertTrue(out.endswith("## 重要的檔案、指令與數據\n- plugins/context/__init__.py\n- 測試 317 個"))

    def test_any_bad_op_rejects_whole(self):
        for bad, hint in (("@@ 改 0\nx\n@@ 插 5\n3. 改好 b.py\n@@ 完", "只有 1 到 7 行"),
                          ("@@ 改 9\nx\n@@ 完", "只有 1 到 7 行"),
                          ("@@ 刪 2-4\n@@ 改 3\n重疊\n@@ 完", "重疊"),
                          ("@@ 插 3\n@@ 完", "沒有內容"),
                          ("@@ 改 2\n@@ 完", "沒有內容"),
                          ("@@ 刪 2\n- 新的\n@@ 完", "@@ 改"),
                          ("@@ 不變\n@@ 刪 2", "不變"),
                          ("這是新的摘要全文", "沒有寫出")):
            text, why = C._apply_edits(PREV, bad)
            self.assertIsNone(text, bad)                                # 有一條不對整份不用：不存半套的摘要
            self.assertIn(hint, why, bad)
        long = "\n".join(f"{i}. 很長的一行內容，記錄做過的事情與結果，" * 3 for i in range(60))
        self.assertIsNone(edit(long, "@@ 刪 1-50\n@@ 完"))             # 一次刪掉大半：多半是看錯行號

    def test_full_summary_instead_of_edits_accepted(self):
        full = PREV.replace("- 在改 b.py", "- 在寫 README") + "\n## 當時還沒做完的\n- 發版"
        self.assertEqual(edit(PREV, full), full)                       # 直接寫了整份（段落齊、長度夠）：照用
        self.assertIsNone(edit(PREV, "## 已完成\n- 只有新的"))          # 只寫新的那一點：不採用（會丟掉舊的）


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
    """預設：上一步剛送過（記憶體裡有那份請求），壓縮接在它後面送"""

    def setUp(self):
        self.hooks = {}
        C._fails.clear()
        C._bg.clear()
        short = C.SHORT_SRC_TOK
        C.SHORT_SRC_TOK = 10 ** 9                        # 這裡的假摘要只有幾行，不檢查長度（test_two_bad_answers 另外測）
        self.addCleanup(setattr, C, "SHORT_SRC_TOK", short)
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
        self.calls = []

        async def emit(ev):
            self.events.append(ev)
        self.ctx = types.SimpleNamespace(session_id=self.sid, emit=emit, force_compact=False, manual=True,
                                         model_cfg={"id": "m", "provider": "anthropic", "context_window": 100000},
                                         registry=types.SimpleNamespace(providers={"anthropic": object()}, tools={}))
        # 上一步實際送出的內容（系統提示詞、工具、歷史）
        self.sent = [{"id": m["id"], "role": m["role"], "content": m["content"]} for m in store.get_messages(self.sid)]
        self.snap = {"model": "m", "at": time.time(), "sig": "s", "through": last["id"], "system": "系統",
                     "specs": [{"name": "read_file", "description": "讀檔", "schema": {}}], "effort": "high", "messages": self.sent}
        for p in (unittest.mock.patch.dict(loop.LAST_REQ, {self.sid: self.snap}),
                  unittest.mock.patch.object(loop, "req_sig", lambda sid: "s"),
                  unittest.mock.patch.object(loop, "aux_model", lambda task, fb: None)):
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, replies, hook="force_compact"):
        async def complete(model, system, messages, tools=None, effort=None, session_id=None, kind=None):
            self.calls.append({"system": system, "messages": messages, "tools": tools, "effort": effort, "kind": kind,
                               "ask": messages[-1]["content"][0]["text"]})
            n = sum(tokens.estimate_messages([m]) for m in messages)
            loop.AUX_USAGE[session_id] = {"input_tokens": n, "cache_read": n - 100}
            r = replies.pop(0)
            if r is None:
                raise loop.ToolCallInstead("tool")
            return r
        with unittest.mock.patch.object(loop, "complete", complete):
            return asyncio.run(self.hooks[hook](self.ctx))

    def summ(self):
        return store.get_session(self.sid)["engine_state"].get("summary")

    def notices(self):
        return " / ".join(e.get("text") or "" for e in self.events if e.get("type") == "notice")

    def test_appended_to_last_request(self):
        self.run_with(["@@ 插 5\n3. 讀完 f0 到 f2\n@@ 改 7\n- 在讀 f3"])
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual(c["system"], "系統")                         # 系統提示詞、工具、思考強度跟上一步一字不差
        self.assertEqual(c["tools"], [{"name": "read_file", "description": "讀檔", "schema": {}}])
        self.assertEqual(c["effort"], "high")
        self.assertEqual(c["messages"][:-1], self.sent)                # 歷史原封不動，前面整段吃快取
        self.assertEqual(c["messages"][-1]["meta"], {"internal": "compact_ask"})
        self.assertIn("1| ## 使用者的目標與要求", c["ask"])            # 舊摘要附行號
        self.assertIn("@@ 插 n", c["ask"])
        self.assertIn("7＝接在最後面", c["ask"])
        s = self.summ()
        self.assertIn("3. 讀完 f0 到 f2", s["text"])
        self.assertIn("- 做 A", s["text"])                            # 沒提到的行原樣留著
        self.assertTrue(s["text"].endswith("- 在讀 f3"))
        self.assertEqual(s["quotes"].count("使用者："), 2)
        self.assertEqual(s["cache"][0] - s["cache"][1], 100)
        self.assertIn("快取命中", self.notices())
        kept = [m for m in store.get_messages(self.sid) if m["id"] > s["through"]]
        self.assertTrue(kept)
        self.assertLessEqual(tokens.estimate_messages(kept), 3000)
        sent = self.hooks["before_request"]([{**m, "meta": {}} for m in store.get_messages(self.sid)], self.ctx)
        self.assertIn(s["quotes"], sent[0]["content"][0]["text"])

    def test_new_messages_after_last_request_appended(self):
        self.snap["messages"] = self.sent[:-2]
        self.snap["through"] = self.sent[-3]["id"]
        self.run_with(["@@ 改 7\n- 在讀 f3"])
        got = self.calls[0]["messages"][:-1]
        self.assertEqual(got[:-2], self.sent[:-2])                     # 上一步送過的原封不動
        hist = {m["id"]: m for m in loop.history(self.sid)}
        self.assertEqual(got[-2:], [hist[m["id"]] for m in self.sent[-2:]])   # 之後新增的照主對話的送法接在後面

    def test_first_summary_full_text(self):
        es = dict(store.get_session(self.sid)["engine_state"])
        es.pop("summary")
        store.update_session(self.sid, engine_state=es)
        self.run_with([GOOD])
        self.assertNotIn("先前摘要", self.calls[0]["ask"])
        self.assertIn("字以內", self.calls[0]["ask"])
        self.assertEqual(self.summ()["text"], GOOD.strip())

    def test_unreadable_edit_retried_as_edit_with_cache(self):
        self.run_with(["我把摘要整理如下：……", "@@ 改 7\n- 在讀 f3\n@@ 完"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["messages"][:-1], self.sent)    # 同一份請求，前面照樣吃快取
        self.assertIn("整套 @@ 編輯指令", self.calls[1]["ask"])         # 還是只寫要改的行，不是整份重寫
        self.assertIn("2. 跑測試全過", self.summ()["text"])
        self.assertTrue(self.summ()["text"].endswith("- 在讀 f3"))
        self.assertIn("沒有寫出 @@ 編輯指令", self.notices())

    def test_partly_bad_edit_not_stored_half(self):
        self.run_with(["@@ 改 30\nx\n@@ 插 5\n3. 讀完 f0 到 f2\n@@ 完", "@@ 插 5\n3. 讀完 f0 到 f2\n- 在讀 f3\n@@ 完"])
        self.assertEqual(len(self.calls), 2)                            # 有一條不對：整套重寫一次指令
        self.assertIn("@@ 改 30：先前摘要只有 1 到 7 行", self.calls[1]["ask"])   # 告訴模型哪一條錯在哪
        self.assertIn("3. 讀完 f0 到 f2", self.summ()["text"])
        self.assertNotIn("x", self.summ()["text"].split("\n"))

    def test_partly_bad_twice_stores_nothing(self):
        self.ctx.manual, self.ctx.force_compact = False, True
        with self.assertRaises(RuntimeError):
            self.run_with(["@@ 改 30\nx\n@@ 插 5\n3. 讀完\n@@ 完", "@@ 改 31\nx\n@@ 完"])
        self.assertEqual(self.summ()["text"], PREV)                     # 舊摘要原樣，不存半套

    def test_fake_call_rewritten_once_with_cache(self):
        self.run_with([FAKE, "@@ 改 7\n- 在讀 f3"])
        self.assertEqual([c["system"] for c in self.calls], ["系統", "系統"])
        self.assertEqual(self.calls[1]["messages"][:-1], self.sent)
        self.assertIn("寫成了工具呼叫", self.notices())

    def test_two_bad_answers_store_nothing(self):
        es = dict(store.get_session(self.sid)["engine_state"])
        es.pop("summary")
        store.update_session(self.sid, engine_state=es)
        self.ctx.manual, self.ctx.force_compact = False, True
        C.SHORT_SRC_TOK = 200
        with self.assertRaises(RuntimeError) as cm:
            self.run_with([None, "好的，我來看看。"])                   # 去呼叫工具、太短
        self.assertIn("只寫了", str(cm.exception))
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.summ())

    def test_later_compaction_checked_on_full_text(self):
        long = "\n".join(f"{i}. 很長的一行內容，記錄做過的事情與結果，" * 3 for i in range(60))
        es = dict(store.get_session(self.sid)["engine_state"])
        es["summary"] = {"through": 0, "text": long}
        store.update_session(self.sid, engine_state=es)
        self.ctx.manual, self.ctx.force_compact = False, True
        with self.assertRaises(RuntimeError) as cm:
            self.run_with(["@@ 刪 1-55", "@@ 刪 1-58"])
        self.assertIn("一次刪掉大半", str(cm.exception))
        self.assertEqual(self.summ()["text"], long)

    def test_rebuilt_when_not_in_memory(self):
        loop.LAST_REQ.pop(self.sid)
        seen = []

        async def rebuild(sid, model, turn=None):
            seen.append(sid)
            return {"system": "重組的系統", "messages": self.sent, "specs": [], "effort": None}
        with unittest.mock.patch.object(loop, "rebuild_request", rebuild):
            self.run_with(["@@ 改 7\n- 在讀 f3"])
        self.assertEqual(seen, [self.sid])
        self.assertEqual(self.calls[0]["system"], "重組的系統")         # 同一種做法：照現在的狀態重組
        self.assertEqual(self.calls[0]["messages"][:-1], self.sent)
        self.assertIn("照現在的狀態重組", self.notices())

    def test_overflow_drops_oldest_keeps_recent(self):
        self.ctx.model_cfg = dict(self.ctx.model_cfg, context_window=9000)
        self.ctx.force_compact = True
        self.run_with(["@@ 改 7\n- 在讀 f3"])
        msgs = self.calls[0]["messages"][:-1]
        self.assertLess(len(msgs), len(self.sent))
        self.assertEqual(msgs[0]["role"], "user")                     # 不從呼叫和結果中間切
        self.assertEqual(msgs[-1], self.sent[-1])                     # 最近的都在
        s = self.summ()
        self.assertTrue(all(m in msgs for m in self.sent if m["id"] > s["through"]))
        self.assertIn("這次沒送", self.notices())

    def test_other_model_noted(self):
        other = {"id": "small", "label": "小模型", "provider": "anthropic", "context_window": 100000}
        with unittest.mock.patch.object(loop, "aux_model", lambda task, fb: other):
            self.run_with(["@@ 改 7\n- 在讀 f3"])
        self.assertIn("吃不到主對話的快取", self.notices())

    def test_auto_stops_after_one_failure(self):
        self.ctx.manual, self.ctx.is_bg = False, True
        with self.assertRaises(RuntimeError):
            self.run_with([FAKE, FAKE], hook="prepare_turn")
        self.assertEqual(len(self.calls), 2)                    # 一次壓縮最多送兩次
        self.assertEqual(C._fails[self.sid], 1)
        self.assertIn("先停止自動壓縮", self.notices())
        self.ctx.is_bg = False
        asyncio.run(self.hooks["after_turn"](self.ctx))
        self.assertIsNone(C._bg.get(self.sid))                  # 不再開背景壓縮
        self.assertIsNone(asyncio.run(self.hooks["prepare_turn"](self.ctx)))
        self.ctx.manual = True
        self.run_with(["@@ 改 7\n- 在讀 f3"])                   # 手動照樣能壓，成功就歸零
        self.assertNotIn(self.sid, C._fails)

    def test_stored_fake_summary_ignored(self):
        es = dict(store.get_session(self.sid)["engine_state"])
        es["summary"] = {"through": 5, "text": FAKE}
        store.update_session(self.sid, engine_state=es)
        self.assertEqual(tokens.summary_of(es), {})
        self.assertTrue(tokens.fake_call("  [呼叫 read_file] a.py"))
        self.assertFalse(tokens.fake_call("## 已完成\n1. [呼叫 x] 之後改好"))
        sent = self.hooks["before_request"]([{**m, "meta": {}} for m in store.get_messages(self.sid)], self.ctx)
        self.assertIsNone(sent)                           # 整段原文照送
        self.run_with([GOOD])
        self.assertNotIn("先前摘要", self.calls[0]["ask"])  # 從頭寫，不拿壞的當舊摘要
        self.assertEqual(self.summ()["text"], GOOD.strip())

    def test_no_second_path_left(self):
        for name in ("_call", "_summarize", "_summarize_cached", "_render", "_text_of", "_BEGIN", "_END",
                     "_ASK_AGAIN", "_segments", "_parallel", "_method"):
            self.assertFalse(hasattr(C, name), name)
        src = (ROOT / "plugins" / "context" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("多工", src)


class Background(unittest.TestCase):
    """超過門檻：交給背景寫摘要，對話不等；到等待線才等；快取過期先不壓"""
    run_with, summ, notices = Flow.run_with, Flow.summ, Flow.notices

    def setUp(self):
        Flow.setUp(self)
        self.ctx.force_compact, self.ctx.manual, self.ctx.mid_turn = False, False, True

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
            with unittest.mock.patch.object(loop, "complete", complete):
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
        self.assertIn("背景寫的摘要", self.summ()["text"])     # 寫好後存起來，下一次送出就用
        self.assertIn("在背景寫摘要", self.notices())

    def test_cold_cache_sends_first(self):
        self.snap["at"] = time.time() - 3600                 # 上一步一小時前：快取大概過期了
        out = self.run_bg(60000)
        self.assertTrue(out["returned_before_summary"])
        self.assertFalse(out["bg_running"])                  # 先讓這一步送出去（順便把快取建回來）
        self.assertEqual(self.summ()["text"], PREV)
        self.snap["at"] = time.time()                        # 這一步送完了
        self.ctx.mid_turn = False

        async def after():
            async def complete(model, system, messages, *a, **k):
                return "@@ 插 5\n3. 背景寫的摘要"
            with unittest.mock.patch.object(loop, "complete", complete):
                await self.hooks["after_turn"](self.ctx)
                self.assertTrue(C._bg.get(self.sid))        # 這時才在背景壓縮
                await C._bg[self.sid]
        asyncio.run(after())
        self.assertIn("背景寫的摘要", self.summ()["text"])

    def test_over_wait_line_waits(self):
        out = self.run_bg(95000)
        self.assertFalse(out["returned_before_summary"])     # 快爆了：停下來等
        self.assertTrue(out["result"])
        self.assertIn("背景寫的摘要", self.summ()["text"])

    def test_overflow_while_background_running_uses_it(self):
        self.set_used(60000)
        gate, n = asyncio.Event(), []

        async def complete(model, system, messages, *a, **k):
            n.append(1)
            await gate.wait()
            return "@@ 插 5\n3. 背景寫的摘要"

        async def main():
            with unittest.mock.patch.object(loop, "complete", complete):
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
        self.assertIn("做 A", self.summ()["text"])


if __name__ == "__main__":
    unittest.main()
