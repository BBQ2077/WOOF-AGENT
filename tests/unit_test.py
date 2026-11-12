"""純函式單元測試：權限判斷、思考強度換算、排程下次時間、成本、壓縮前清舊工具輸出、目錄樹。

用暫存資料夾當 data（WOOF_DATA），不碰真正的對話資料庫。執行：
  .venv/Scripts/python tests/unit_test.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
