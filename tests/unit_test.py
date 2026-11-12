"""純函式單元測試：權限判斷、思考強度換算、排程下次時間、成本、壓縮前清舊工具輸出、目錄樹、Responses API 串流。

用暫存資料夾當 data（WOOF_DATA），不碰真正的對話資料庫。執行：
  .venv/Scripts/python tests/unit_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="woof_test_")
os.environ["WOOF_DATA"] = TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import cost, cron, effort, perms, store, tokens  # noqa: E402
from woof.loader import REG  # noqa: E402


class Perms(unittest.TestCase):
    def setUp(self):
        REG.config = {"permissions": {"allow_cmds": ["git status"], "deny_cmds": ["shutdown"]}}

    def d(self, mode, name, args=None, headless=False):
        return perms.decide(mode, name, args or {}, headless)["decision"]

    def test_modes(self):
        self.assertEqual(self.d("ask", "read_file"), "auto")
        self.assertEqual(self.d("ask", "list_tree"), "auto")
        self.assertEqual(self.d("ask", "write_file"), "ask")
        self.assertEqual(self.d("edits", "write_file"), "auto")
        self.assertEqual(self.d("readonly", "terminal", {"command": "ls"}), "deny")
        self.assertEqual(self.d("auto", "terminal", {"command": "ls"}), "auto")

    def test_danger_and_hardline(self):
        self.assertEqual(self.d("auto", "terminal", {"command": "rm -rf build"}), "ask")
        self.assertEqual(self.d("auto", "terminal", {"command": "rm -f -R build"}), "ask")
        self.assertEqual(self.d("auto", "terminal", {"command": "rm -f tmp.py"}), "auto")  # 刪單一檔案不是遞迴刪除
        self.assertEqual(self.d("auto", "terminal", {"command": "format c:"}), "deny")
        self.assertEqual(self.d("auto", "terminal", {"command": "sudo shutdown now"}), "deny")

    def test_allowlist_not_chained(self):
        self.assertEqual(self.d("ask", "terminal", {"command": "git status"}), "auto")
        self.assertEqual(self.d("ask", "terminal", {"command": "git status && rm x"}), "ask")

    def test_headless(self):
        self.assertEqual(self.d("ask", "write_file", headless=True), "deny")


class Effort(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(effort.resolve(["low", "medium", "high"], "xhigh"), "high")
        self.assertEqual(effort.resolve(["low", "high"], "medium"), "low")
        self.assertEqual(effort.resolve(["medium", "high"], "minimal"), "medium")
        self.assertEqual(effort.resolve(["off", "on"], "high"), "on")
        self.assertEqual(effort.resolve(["off", "on"], "off"), "off")
        self.assertIsNone(effort.resolve([], "high"))


class Cron(unittest.TestCase):
    def test_next(self):
        base = datetime(2026, 11, 12, 9, 30).timestamp()
        nxt = datetime.fromtimestamp(cron.next_after("0 10 * * *", base))
        self.assertEqual((nxt.day, nxt.hour, nxt.minute), (12, 10, 0))
        nxt = datetime.fromtimestamp(cron.next_after("*/15 * * * *", base))
        self.assertEqual((nxt.hour, nxt.minute), (9, 45))
        nxt = datetime.fromtimestamp(cron.next_after("0 9 * * 1", base))  # 2026-11-12 是週四，下個週一是 16 日
        self.assertEqual((nxt.day, nxt.hour), (16, 9))

    def test_bad_expr(self):
        with self.assertRaises(Exception):
            cron.parse("61 * * * *")


class Cost(unittest.TestCase):
    def test_cost_of(self):
        m = {"price": [3, 15, 0.3]}
        # 1M 輸入裡 0.5M 是快取：0.5*3 + 0.5*0.3 + 0.1*15 = 3.15
        self.assertAlmostEqual(cost.cost_of(m, 1_000_000, 100_000, 500_000), 3.15)
        self.assertIsNone(cost.cost_of({}, 10, 10, 0))
        self.assertAlmostEqual(cost.cost_of({"price": [2]}, 1_000_000, 1_000_000, 0), 4.0)

    def test_budget(self):
        self.assertEqual(cost.clean_budget({"enabled": True, "daily": "1.5", "action": "pause"}), {"enabled": True, "daily": 1.5, "action": "pause"})
        self.assertEqual(cost.clean_budget({"action": "???"}), {"enabled": False, "action": "warn"})
        with self.assertRaises(ValueError):
            cost.clean_budget({"daily": -1})

    def test_spent_and_status(self):
        cfg = {"models": [{"id": "a", "price": [1, 2]}, {"id": "b"}], "budget": {"enabled": True, "daily": 0.5, "action": "pause"}}
        store.log_usage("s1", "a", {"input_tokens": 1_000_000, "output_tokens": 0})
        store.log_usage("s1", "b", {"input_tokens": 10, "output_tokens": 10})
        st = cost.status(cfg)
        self.assertAlmostEqual(st["today"], 1.0)
        self.assertTrue(st["over"])
        self.assertEqual(cost.spent(cfg, 0)["unpriced"], ["b"])
        cfg["budget"]["enabled"] = False
        self.assertEqual(cost.status(cfg)["over"], [])  # 關閉時不檢查


class Prune(unittest.TestCase):
    def test_apply(self):
        msgs = [
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"id": 2, "role": "tool", "content": [{"type": "tool_result", "id": "c1", "name": "read_file", "output": "x" * 5000}]},
            {"id": 3, "role": "tool", "content": [{"type": "tool_result", "id": "c2", "name": "terminal", "output": "y" * 50, "is_error": True}]},
        ]
        out = tokens.apply_prune(msgs, {"prune": {"through": 2}})
        self.assertIn("read_file", out[1]["content"][0]["output"])
        self.assertIn("5000", out[1]["content"][0]["output"])
        self.assertEqual(out[2]["content"][0]["output"], "y" * 50)
        self.assertEqual(msgs[1]["content"][0]["output"], "x" * 5000)  # 不改原本的
        self.assertIs(tokens.apply_prune(msgs, {}), msgs)
        self.assertIn("失敗", tokens.pruned_result(msgs[2]["content"][0])["output"])


class Tree(unittest.TestCase):
    def test_list_tree(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("files_plugin", ROOT / "plugins" / "files" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tools = {}
        mod.register(types.SimpleNamespace(add_tool=lambda n, d, s, h: tools.__setitem__(n, h)))
        root = Path(tempfile.mkdtemp(prefix="woof_tree_"))
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("print(1)")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "junk.js").write_text("x")
        r = asyncio.run(tools["list_tree"]({"path": str(root)}, types.SimpleNamespace(cwd=str(root))))
        out = r["output"]
        self.assertIn("src/", out)
        self.assertIn("a.py", out)
        self.assertIn("node_modules/  （略過）", out)
        self.assertNotIn("junk.js", out)


class PruneBeforeSummary(unittest.TestCase):
    """壓縮插件：超過門檻時先清舊工具輸出，夠的話不寫摘要。"""

    def test_prune_path(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ctx_plugin", ROOT / "plugins" / "context" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hooks = {}
        fake = types.SimpleNamespace(settings={"trigger": 50, "keep": 1},
                                     on=lambda ev, fn, **k: hooks.setdefault(ev, fn))
        mod.register(fake)
        s = store.create_session("m", None, "t")
        sid = s["id"]
        big = "資料 " * 20000
        store.add_message(sid, "user", [{"type": "text", "text": "第一輪"}])
        store.add_message(sid, "assistant", [{"type": "tool_call", "id": "c1", "name": "read_file", "args": {"path": "a"}}])
        store.add_message(sid, "tool", [{"type": "tool_result", "id": "c1", "name": "read_file", "output": big}])
        store.add_message(sid, "assistant", [{"type": "text", "text": "讀完了"}])
        last = store.add_message(sid, "user", [{"type": "text", "text": "第二輪"}])
        store.update_session(sid, engine_state={"usage": {"context_used": 60000, "through": last["id"]}})
        events = []

        async def emit(ev):
            events.append(ev)

        ctx = types.SimpleNamespace(session_id=sid, model_cfg={"id": "m", "provider": "p", "context_window": 100000},
                                    registry=types.SimpleNamespace(providers={"p": object()}), emit=emit)
        r = asyncio.run(hooks["prepare_turn"](ctx))
        es = store.get_session(sid)["engine_state"]
        self.assertTrue(r)
        self.assertIn("prune", es)
        self.assertNotIn("summary", es)
        self.assertTrue(any("先清掉 1 個" in (e.get("text") or "") for e in events))
        msgs = [{"id": m["id"], "role": m["role"], "content": m["content"]} for m in store.get_messages(sid)]
        sent = hooks["before_request"](msgs, ctx)
        if asyncio.iscoroutine(sent):
            sent = asyncio.run(sent)
        self.assertIsNotNone(sent)
        self.assertNotIn("資料 資料", sent[2]["content"][0]["output"])
        self.assertLess(es["usage"]["context_used"], 50000)


class LoopFallbackAndReview(unittest.TestCase):
    """模型迴圈：回合中途失敗改用備用模型接著做；自動審核放行。用假的服務商，不連網。"""

    def setUp(self):
        from woof.agent import loop
        self.loop = loop
        calls = self.calls = []

        class Flaky:
            n = 0

            async def stream(self, model, system, messages, tools, effort):
                Flaky.n += 1
                calls.append(("A", Flaky.n))
                if Flaky.n == 1:
                    yield {"type": "message", "content": [{"type": "tool_call", "id": "t1", "name": "echo", "args": {"x": 1}}]}
                    return
                raise RuntimeError("429 rate limited")

        class Good:
            async def stream(self, model, system, messages, tools, effort):
                calls.append(("B", len(messages)))
                sys_text = system if isinstance(system, str) else str(system)
                if "安全審核" in sys_text or "SAFE" in sys_text:
                    yield {"type": "text_delta", "text": "SAFE 只是讀取"}
                    yield {"type": "message", "content": [{"type": "text", "text": "SAFE 只是讀取"}]}
                    return
                yield {"type": "text_delta", "text": "備用接手完成"}
                yield {"type": "message", "content": [{"type": "text", "text": "備用接手完成"}]}

        async def echo(args, ctx):
            return {"output": "echo ok"}

        REG.config = {"models": [{"id": "A", "provider": "fa", "label": "A"}, {"id": "B", "provider": "fb", "label": "B"}],
                      "aux": {"fallback": "B", "review": "B"}, "default_model": "A"}
        REG.providers = {"fa": Flaky(), "fb": Good()}
        REG.tools["echo"] = types.SimpleNamespace(name="echo", plugin="t", handler=echo, description="echo",
                                                  schema={"type": "object"}, bridge=False)

    def test_mid_turn_fallback(self):
        s = store.create_session("A", None, "fb")
        events = []

        async def emit(ev):
            events.append(ev)

        asyncio.run(self.loop.run_turn(s["id"], [{"type": "text", "text": "做事"}], emit))
        msgs = store.get_messages(s["id"])
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertEqual(msgs[-1]["meta"]["model"], "B")
        self.assertEqual(msgs[2]["content"][0]["output"], "echo ok")
        self.assertTrue(any("改用備用模型 B 接著做" in (e.get("text") or "") for e in events))

    def test_auto_review(self):
        ctx = types.SimpleNamespace(cwd=TMP, session_id="x")
        why = asyncio.run(self.loop._auto_review("write_file", {"path": "a.txt", "content": "hi"}, ctx))
        self.assertIn("只是讀取", why)
        REG.config["aux"].pop("review")
        self.assertEqual(asyncio.run(self.loop._auto_review("write_file", {}, ctx)), "")


class SubagentSteer(unittest.TestCase):
    """子代理執行中，使用者在主對話插話：子代理做完這一步就停下回報。"""

    def test_stop_on_user_steer(self):
        from woof.agent import loop

        class Loopy:
            async def stream(self, model, system, messages, tools, effort):
                yield {"type": "text_delta", "text": "查一下"}
                yield {"type": "message", "content": [{"type": "text", "text": "查一下"},
                                                      {"type": "tool_call", "id": f"t{len(messages)}", "name": "poke", "args": {}}]}

        parent_s = store.create_session("C", None, "parent")

        async def poke(args, ctx):
            loop.push_steer(parent_s["id"], [{"type": "text", "text": "等一下，先改別的"}])
            return {"output": "ok"}

        REG.config = {"models": [{"id": "C", "provider": "fc", "label": "C"}], "default_model": "C"}
        REG.providers = {"fc": Loopy()}
        REG.tools["poke"] = types.SimpleNamespace(name="poke", plugin="t", handler=poke, description="poke",
                                                  schema={"type": "object"}, bridge=False)

        async def emit(ev):
            pass

        parent = loop.TurnContext(parent_s, REG.config["models"][0], None, emit)
        r = asyncio.run(loop.run_subagent(parent, "做很久的事"))
        text = r.get("answer") or r.get("text") or str(r)
        self.assertIn("使用者在主對話插話", text)
        self.assertEqual(len(loop.peek_steer(parent_s["id"])), 1)  # 插話留給主對話


class SubagentTerminal(unittest.TestCase):
    """寫入子代理的 terminal：工作目錄鎖在範圍內、不能背景執行。"""

    def test_scope(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("term_plugin", ROOT / "plugins" / "terminal" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tools = {}
        mod.register(types.SimpleNamespace(settings={}, add_tool=lambda n, d, s, h, **k: tools.__setitem__(n, h),
                                           on=lambda *a, **k: None))
        scope = Path(tempfile.mkdtemp(prefix="woof_scope_")).resolve()
        turn = types.SimpleNamespace(session={"parent": "p"}, write_scope=[scope])

        async def out(_):
            pass

        def ctx(cwd):
            return types.SimpleNamespace(cwd=cwd, session_id="sub", turn=turn, output=out)

        run = lambda a, c: asyncio.run(tools["terminal"](a, c))
        r = run({"command": "echo hi", "workdir": str(ROOT)}, ctx(str(scope)))
        self.assertTrue(r["is_error"])
        self.assertIn("範圍外", r["output"])
        r = run({"command": "echo hi", "background": True}, ctx(str(scope)))
        self.assertIn("不能開背景", r["output"])
        r = run({"command": "python -c \"import os;print(os.getcwd())\""}, ctx(str(ROOT)))  # 預設工作目錄在範圍外 → 改到範圍裡執行
        self.assertFalse(r["is_error"], r["output"])
        self.assertIn(scope.name.lower(), r["output"].lower())
        turn.write_scope = []
        self.assertIn("沒有指定寫入範圍", run({"command": "echo"}, ctx(str(scope)))["output"])


class EmptyReply(unittest.TestCase):
    """模型回空內容：先請它用文字回覆一次；還是空就明說；思考用完輸出上限時請它精簡思考。用假的服務商，不連網。"""

    def _run(self, replies):
        from woof.agent import loop
        seen = []

        class P:
            async def stream(self, model, system, messages, tools, effort):
                seen.append(messages)
                r = replies.pop(0) if replies else {"content": []}
                yield {"type": "message", "content": r["content"], "meta": r.get("meta") or {}}

        REG.config = {"models": [{"id": "E", "provider": "fe", "label": "E"}], "default_model": "E"}
        REG.providers = {"fe": P()}
        s = store.create_session("E", None, "empty")

        async def emit(ev):
            pass

        asyncio.run(loop.run_turn(s["id"], [{"type": "text", "text": "嗨"}], emit))
        return store.get_messages(s["id"]), seen

    def test_nudge_then_text(self):
        msgs, seen = self._run([{"content": []}, {"content": [{"type": "text", "text": "好的"}]}])
        self.assertEqual(len(seen), 2)  # 空回覆後再問一次
        last = msgs[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertEqual(last["content"][0]["text"], "好的")
        self.assertFalse((last.get("meta") or {}).get("empty"))

    def test_still_empty(self):
        msgs, seen = self._run([{"content": []}, {"content": []}])
        self.assertEqual(len(seen), 2)  # 只追問一次
        self.assertTrue(msgs[-1]["meta"].get("empty"))
        self.assertIn("沒有給出文字回覆", msgs[-1]["content"][0]["text"])

    def test_thinking_used_up_output(self):
        cut = {"content": [], "meta": {"stop_reason": "length"}}
        msgs, seen = self._run([dict(cut), dict(cut)])
        self.assertEqual(len(seen), 2)  # 請它精簡思考重來一次
        self.assertIn("輸出上限", msgs[-1]["content"][0]["text"])


class ResponsesAPI(unittest.TestCase):
    """Responses API（/v1/responses）：本機假伺服器回放官方格式的串流事件，不連網。"""

    @staticmethod
    def _plugin():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "oa_plugin", ROOT / "plugins" / "openai_compat" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _serve(self, plans):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # 沒有 Content-Length：送完就關，串流照樣一段一段來

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.server.seen.append({"path": self.path,
                                         "body": json.loads(self.rfile.read(n) or b"{}")})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev in (self.server.plan.pop(0) if self.server.plan else []):
                    name = ev.get("type") or ev.get("object") or "message"
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.plan = list(plans)
        srv.seen = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1", srv

    def _run(self, plans, messages, tools=None, effort="high", mid="gpt-5.2", **extra):
        mod = self._plugin()
        base, srv = self._serve(plans)
        model = {"id": mid, "provider": "openai_compat", "model": mid, "label": mid, "base_url": base,
                 "effort_style": "openai", "vision": False, "api_style": "responses", **extra}
        prov = mod.OpenAICompat(types.SimpleNamespace(settings={}, keys=lambda *a: None))

        async def collect():
            return [ev async for ev in prov._once("test-key", model, "你是 WOOF", messages, tools or [], effort)]

        return asyncio.run(collect()), srv

    @staticmethod
    def _text_plan():
        return [
            {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
            {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "output_index": 0,
             "summary_index": 0, "delta": "先看題目"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "reasoning", "id": "rs_1",
                      "summary": [{"type": "summary_text", "text": "先看題目"}],
                      "encrypted_content": "enc_abc"}},
            {"type": "response.output_item.added", "output_index": 1,
             "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []}},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1,
             "content_index": 0, "delta": "答案是"},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1,
             "content_index": 0, "delta": " 42"},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {"type": "message", "id": "msg_1", "role": "assistant",
                      "content": [{"type": "output_text", "text": "答案是 42"}]}},
            {"type": "response.completed", "response": {
                "id": "resp_1", "status": "completed",
                "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                          "input_tokens_details": {"cached_tokens": 64},
                          "output_tokens_details": {"reasoning_tokens": 9}}}},
        ]

    def test_text_thinking_usage(self):
        evs, srv = self._run([self._text_plan()],
                             [{"role": "user", "content": [{"type": "text", "text": "1+1"}]}])
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "thinking_delta"), "先看題目")
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "text_delta"), "答案是 42")
        msg = [e for e in evs if e["type"] == "message"][-1]
        blocks = {b["type"]: b for b in msg["content"]}
        self.assertEqual(blocks["text"]["text"], "答案是 42")
        d = blocks["thinking"]["details"][0]
        self.assertEqual((d["id"], d["encrypted_content"]), ("rs_1", "enc_abc"))
        self.assertEqual(d["summary"][0]["text"], "先看題目")
        u = [e for e in evs if e["type"] == "usage"][0]
        self.assertEqual((u["input_tokens"], u["output_tokens"], u["cache_read"]), (120, 30, 64))
        self.assertEqual(srv.seen[0]["path"], "/v1/responses")
        body = srv.seen[0]["body"]
        self.assertEqual(body["instructions"], "你是 WOOF")
        self.assertEqual(body["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertFalse(body["store"])
        self.assertIn("reasoning.encrypted_content", body["include"])
        self.assertEqual(body["input"], [{"role": "user", "content": [{"type": "input_text", "text": "1+1"}]}])
        self.assertEqual(body["max_output_tokens"], 32000)

    @staticmethod
    def _tool_plan():
        return [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_abc",
                      "name": "read_file", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0,
             "delta": "{\"path\": "},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0,
             "delta": "\"README.md\"}"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "read_file",
                      "arguments": "{\"path\": \"README.md\"}"}},
            {"type": "response.completed", "response": {
                "id": "resp_2", "status": "completed",
                "usage": {"input_tokens": 50, "output_tokens": 8}}},
        ]

    def test_tool_round_trip(self):
        tools = [{"name": "read_file", "description": "讀檔", "schema": {"type": "object", "properties": {}}}]
        evs, srv = self._run([self._tool_plan()],
                             [{"role": "user", "content": [{"type": "text", "text": "看 README"}]}], tools=tools)
        msg = [e for e in evs if e["type"] == "message"][-1]
        call = [b for b in msg["content"] if b["type"] == "tool_call"][0]
        self.assertEqual((call["id"], call["item_id"], call["name"]), ("call_abc", "fc_1", "read_file"))
        self.assertEqual(call["args"], {"path": "README.md"})
        body = srv.seen[0]["body"]
        self.assertEqual((body["tools"][0]["type"], body["tools"][0]["name"]), ("function", "read_file"))
        self.assertEqual(body["tools"][0]["parameters"], {"type": "object", "properties": {}})

        # 第二輪：工具呼叫跟結果送回去，call_id 要對得上
        messages = [{"role": "user", "content": [{"type": "text", "text": "看 README"}]},
                    {"role": "assistant", "content": [call]},
                    {"role": "tool", "content": [{"type": "tool_result", "id": "call_abc", "output": "# WOOF"}]}]
        _, srv2 = self._run([self._text_plan()], messages, tools=tools)
        items = srv2.seen[0]["body"]["input"]
        fc = [i for i in items if i.get("type") == "function_call"][0]
        self.assertEqual((fc["id"], fc["call_id"], fc["name"]), ("fc_1", "call_abc", "read_file"))
        self.assertEqual(json.loads(fc["arguments"]), {"path": "README.md"})
        out = [i for i in items if i.get("type") == "function_call_output"][0]
        self.assertEqual((out["call_id"], out["output"]), ("call_abc", "# WOOF"))

    def test_reasoning_echoed_back(self):
        detail = {"type": "reasoning", "id": "rs_1",
                  "summary": [{"type": "summary_text", "text": "想過"}], "encrypted_content": "enc_abc"}
        messages = [{"role": "user", "content": [{"type": "text", "text": "嗨"}]},
                    {"role": "assistant", "content": [{"type": "thinking", "text": "想過", "details": [detail]},
                                                      {"type": "text", "text": "你好"}]},
                    {"role": "user", "content": [{"type": "text", "text": "再說一次"}]}]
        # 預設只在這一回合（工具呼叫鏈）內回傳思考：新回合不送舊思考
        _, srv = self._run([self._text_plan()], messages)
        self.assertFalse([i for i in srv.seen[0]["body"]["input"] if i.get("type") == "reasoning"])
        # echo_reasoning = "all"：每回合都送，加密的思考內容原樣帶回
        _, srv = self._run([self._text_plan()], messages, echo_reasoning="all")
        r = [i for i in srv.seen[0]["body"]["input"] if i.get("type") == "reasoning"][0]
        self.assertEqual((r["id"], r["encrypted_content"]), ("rs_1", "enc_abc"))
        self.assertEqual(r["summary"][0]["text"], "想過")
        # echo_reasoning = "off"：不回傳也不要求加密內容
        _, srv = self._run([self._text_plan()], messages, echo_reasoning="off")
        self.assertNotIn("include", srv.seen[0]["body"])

    def test_image_and_stop(self):
        img = Path(tempfile.mkdtemp(prefix="woof_img_")) / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "看圖"}, {"type": "image", "path": str(img), "media_type": "image/png"}]}]
        plan = [{"type": "response.output_item.added", "output_index": 0,
                 "item": {"type": "message", "id": "msg_1", "role": "assistant"}},
                {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "一張圖"},
                {"type": "response.incomplete", "response": {
                    "id": "resp_3", "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 10, "output_tokens": 400,
                              "output_tokens_details": {"reasoning_tokens": 300}}}}]
        evs, srv = self._run([plan], messages, vision=True)
        part = srv.seen[0]["body"]["input"][0]["content"][1]
        self.assertEqual(part["type"], "input_image")
        self.assertTrue(part["image_url"].startswith("data:image/png;base64,"))
        msg = [e for e in evs if e["type"] == "message"][-1]
        self.assertEqual(msg["meta"], {"stop_reason": "max_tokens", "hidden_reasoning": 300})

    def test_other_vendors_stay_chat(self):
        mod = self._plugin()
        self.assertEqual(mod._api_style({"id": "gpt-5.2", "base_url": "https://api.openai.com/v1"}), "responses")
        self.assertEqual(mod._api_style({"id": "m", "model": "deepseek-chat",
                                         "base_url": "https://api.deepseek.com"}), "chat")
        self.assertEqual(mod._api_style({"id": "gpt-5.2", "base_url": "https://api.openai.com/v1",
                                         "api_style": "chat"}), "chat")
        self.assertEqual(mod._api_style({"id": "llama-3", "base_url": "https://api.openai.com/v1"}), "chat")

        chunk = lambda delta, fin=None: {"id": "c1", "object": "chat.completion.chunk", "created": 1,
                                         "model": "compat-1",
                                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
        evs, srv = self._run([[chunk({"content": "嗨"}), chunk({}, "stop")]],
                             [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                             effort=None, mid="compat-1", api_style="chat")
        self.assertEqual(srv.seen[0]["path"], "/v1/chat/completions")
        self.assertIn("messages", srv.seen[0]["body"])
        self.assertEqual([e["text"] for e in evs if e["type"] == "text_delta"], ["嗨"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
