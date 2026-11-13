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
import unittest.mock
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="woof_test_")
os.environ["WOOF_DATA"] = TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import cost, cron, effort, perms, remote as rconn, snapshot, store, tokens  # noqa: E402
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

    def test_categories(self):
        self.assertEqual(perms.category("read_file"), "read")
        self.assertEqual(perms.category("memory"), "write")
        self.assertEqual(perms.category("delegate_task"), "agent")
        self.assertEqual(perms.category("web_search"), "network")
        self.assertEqual(perms.category("mcp_foo_bar"), "mcp")
        self.assertEqual(perms.category("沒分類的工具"), "other")

    def test_denylist_beats_allowlist(self):
        REG.config["permissions"]["deny_cmds"] = ["shutdown now"]
        self.assertEqual(self.d("ask", "terminal", {"command": "echo hi; shutdown now"}), "deny")

    def test_custom_mode(self):
        REG.config["permissions"]["custom"] = {"read": "deny", "write": "auto"}
        self.assertEqual(self.d("custom", "read_file"), "deny")
        self.assertEqual(self.d("custom", "write_file"), "auto")
        self.assertEqual(self.d("custom", "terminal", {"command": "ls"}), "ask")  # 沒列的類別用預設

    def test_unknown_mode_falls_back_to_auto(self):
        self.assertEqual(self.d("根本沒有這個模式", "terminal", {"command": "ls"}), "auto")

    def test_danger_off(self):
        REG.config["permissions"]["danger_ask"] = False
        self.assertEqual(self.d("auto", "terminal", {"command": "rm -rf build"}), "auto")

    def test_headless_approve(self):
        REG.config["permissions"]["headless"] = "approve"
        self.assertEqual(self.d("ask", "write_file", headless=True), "auto")
        self.assertEqual(self.d("auto", "terminal", {"command": "rm -rf build"}, headless=True), "auto")

    def test_describe_and_loosened(self):
        text = perms.describe("readonly")
        self.assertIn("唯讀", text)
        self.assertIn("一律擋下", text)
        self.assertIn("寫入檔案", perms.loosened("readonly", "edits"))


class Effort(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(effort.resolve(["low", "medium", "high"], "xhigh"), "high")
        self.assertEqual(effort.resolve(["low", "high"], "medium"), "low")
        self.assertEqual(effort.resolve(["medium", "high"], "minimal"), "medium")
        self.assertEqual(effort.resolve(["off", "on"], "high"), "on")
        self.assertEqual(effort.resolve(["off", "on"], "off"), "off")
        self.assertIsNone(effort.resolve([], "high"))

    def test_default_and_off(self):
        self.assertEqual(effort.resolve(["low", "medium", "high"], None, "medium"), "medium")
        self.assertEqual(effort.resolve(["low", "medium", "high"], None, "xhigh"), "high")
        self.assertEqual(effort.resolve(["off", "low", "high"], "off"), "off")
        self.assertEqual(effort.resolve(["low", "high"], "none"), "low")  # 想關閉但模型不能關 → 最低一檔
        self.assertEqual(effort.resolve(["minimal", "low"], "max"), "low")  # 往下都沒有 → 最低
        self.assertEqual(effort.resolve(["low", "high"], "on"), "high")  # 不在檔位表裡（例如 on）→ 沿用模型預設
        self.assertEqual(effort.resolve(["minimal", "max"], "xhigh"), "minimal")  # 往下只剩 minimal


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

    def test_next_edges(self):
        def at(expr, *args):
            return datetime.fromtimestamp(cron.next_after(expr, datetime(*args).timestamp()))

        nxt = at("0 9 * * *", 2026, 11, 12, 9, 30)  # 今天的時間過了 → 明天
        self.assertEqual((nxt.day, nxt.hour), (13, 9))
        nxt = at("*/20 * * * *", 2026, 11, 12, 9, 41)
        self.assertEqual((nxt.hour, nxt.minute), (10, 0))
        nxt = at("0 9 1 * *", 2026, 11, 12, 9, 30)  # 每月 1 號
        self.assertEqual((nxt.month, nxt.day), (12, 1))
        nxt = at("30 8 15 * *", 2026, 11, 12, 9, 30)  # 本月 15 號還沒到
        self.assertEqual((nxt.month, nxt.day, nxt.minute), (11, 15, 30))
        nxt = at("0 9 * * 0", 2026, 11, 12, 9, 30)  # 週日（7 也等於週日）
        self.assertEqual((nxt.day, nxt.weekday()), (15, 6))
        nxt = at("0 9 * * 7", 2026, 11, 12, 9, 30)
        self.assertEqual(nxt.day, 15)
        nxt = at("0 9 20 * 5", 2026, 11, 12, 9, 30)  # 日與週都限定：取聯集，最近的週五先到
        self.assertEqual(nxt.day, 13)

    def test_parse_lists_and_ranges(self):
        mi, hr, dom, mon, dow = cron.parse("0,30 9-11 1,15 * 1-5")
        self.assertEqual(sorted(mi), [0, 30])
        self.assertEqual(sorted(hr), [9, 10, 11])
        self.assertEqual(sorted(dom), [1, 15])
        self.assertEqual(sorted(dow), [1, 2, 3, 4, 5])

    def test_bad_expr_detail(self):
        for bad in ("0 9 * *", "0 24 * * *", "0 9 0 * *", "0 9 * 13 *", "*/0 * * * *", "0 9 * * 8", "a * * * *", ""):
            with self.assertRaises(ValueError):
                cron.parse(bad)

    def test_describe(self):
        self.assertEqual(cron.describe("*/15 * * * *"), "每 15 分鐘")
        self.assertEqual(cron.describe("* * * * *"), "每分鐘")
        self.assertEqual(cron.describe("0 * * * *"), "每個整點")
        self.assertEqual(cron.describe("0 */2 * * *"), "每 2 小時（:00）")
        self.assertEqual(cron.describe("0 9 * * *"), "每天 09:00")
        self.assertEqual(cron.describe("0 9 * * 1-5"), "工作日 09:00")
        self.assertEqual(cron.describe("0 9 * * 0,6"), "週末 09:00")
        self.assertEqual(cron.describe("0 9 * * 3"), "每週三 09:00")
        self.assertEqual(cron.describe("0 9 1 * *"), "每月 1 日 09:00")
        self.assertEqual(cron.describe("0 9 * *"), "0 9 * *")  # 欄數不對：原樣顯示

    def test_normalize(self):
        j = cron.normalize({"name": "每天早上", "prompt": "整理進度", "schedule": "daily"})
        self.assertEqual(j["schedule"], "0 9 * * *")
        self.assertEqual(j["name"], "每天早上")
        self.assertTrue(j["enabled"])
        self.assertNotIn("history", j)
        self.assertTrue(j["next_run"] > time.time())
        with self.assertRaises(ValueError):
            cron.normalize({"prompt": "要打掃", "schedule": "0 9 * * 9"})


class WorkspaceSnapshot(unittest.TestCase):
    """工作區快照：改檔前存一份、還原到某一則訊息之前、撤銷還原。用暫存資料夾，不碰真正的對話。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="woof_snap_"))
        self.turn = types.SimpleNamespace(root_session_id="s1", turn_mid=1)
        self.ctx = types.SimpleNamespace(turn=self.turn)
        snapshot.before_write(self.ctx, self.dir / "a.txt")  # 這回合第一次寫 a.txt：存原檔（不存在）
        (self.dir / "a.txt").write_text("第一版\n", encoding="utf-8")

    def test_first_write_then_restore_removes_new_file(self):
        cps = snapshot.checkpoints("s1")
        self.assertEqual([c["mid"] for c in cps], [1])
        self.assertEqual(cps[0]["files"][0]["state"], "created")
        r = snapshot.restore("s1", 1)
        self.assertIn(str((self.dir / "a.txt").resolve()), r["restored"])
        self.assertFalse((self.dir / "a.txt").exists())  # 原本不存在 → 還原成刪掉

    def test_only_second_file_same_turn(self):
        snapshot.before_write(self.ctx, self.dir / "a.txt")  # 同一回合同一個檔只記第一次
        (self.dir / "a.txt").write_text("第二版\n", encoding="utf-8")
        self.assertEqual(len(snapshot.checkpoints("s1")[0]["files"]), 1)
        r = snapshot.restore("s1", 1)
        self.assertFalse((self.dir / "a.txt").exists())

    def test_undo_restore(self):
        (self.dir / "a.txt").write_text("第二版\n", encoding="utf-8")
        snapshot.restore("s1", 1)
        self.assertFalse((self.dir / "a.txt").exists())
        r = snapshot.undo("s1")
        self.assertIn(str((self.dir / "a.txt").resolve()), r["restored"])
        self.assertEqual((self.dir / "a.txt").read_text(encoding="utf-8"), "第二版\n")

    def test_restore_to_earlier_message_keeps_later_state(self):
        (self.dir / "a.txt").write_text("第二版\n", encoding="utf-8")
        self.turn.turn_mid = 2
        snapshot.before_write(self.ctx, self.dir / "a.txt")
        (self.dir / "a.txt").write_text("第三版\n", encoding="utf-8")
        r = snapshot.restore("s1", 2)  # 回到第 2 則訊息之前：用第 2 個檢查點存的第二版
        self.assertIn(str((self.dir / "a.txt").resolve()), r["restored"])
        self.assertEqual((self.dir / "a.txt").read_text(encoding="utf-8"), "第二版\n")
        # 還原後這兩個檢查點收進 _undo，只剩第 1 個
        self.assertEqual([c["mid"] for c in snapshot.checkpoints("s1") if isinstance(c["mid"], int)], [1])

    def test_skipped_and_disabled(self):
        (self.dir / "sub").mkdir()
        snap = snapshot.settings()
        with unittest.mock.patch.object(snapshot, "settings", lambda: {**snap, "max_file_mb": 0}):
            self.turn.turn_mid = 3
            snapshot.before_write(self.ctx, self.dir / "sub")  # 資料夾不存
            snapshot.before_write(self.ctx, self.dir / "a.txt")  # 超過上限 → 略過
        last = [c for c in snapshot.checkpoints("s1") if c["mid"] == 3][0]
        states = {Path(f["path"]).name: f for f in last["files"]}
        self.assertEqual(states["sub"]["skipped"], "資料夾")
        self.assertTrue(states["a.txt"]["skipped"])  # 檔案太大：沒有 sha 可還原
        self.assertTrue(any("略過" in n for n in snapshot.restore("s1", 3)["notes"]))
        with unittest.mock.patch.object(snapshot, "settings", lambda: {**snap, "enabled": False}):
            self.turn.turn_mid = 4
            snapshot.before_write(self.ctx, self.dir / "off.txt")
        self.assertNotIn(4, [c["mid"] for c in snapshot.checkpoints("s1")])

    def test_drop(self):
        snapshot.drop("s1")
        self.assertEqual(snapshot.checkpoints("s1"), [])


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



class Presets(unittest.TestCase):
    """模式預設組：內建模式、自訂存檔、套用時的權限與插件開關、排隊才換插件。"""

    PLUGINS = ["files", "terminal", "web_search", "vision", "memory", "subagent", "tasks"]

    def setUp(self):
        from woof import presets
        self.presets = presets
        self._plugins = REG.plugins
        REG.plugins = {n: types.SimpleNamespace(name=n) for n in self.PLUGINS}
        presets.PATH.unlink(missing_ok=True)

    def tearDown(self):
        REG.plugins = self._plugins
        self.presets.PATH.unlink(missing_ok=True)

    def test_builtin(self):
        ps = self.presets.all_presets()
        self.assertEqual([p["id"] for p in ps[:5]], ["chat", "write", "build", "orchestrate", "readonly"])
        chat = self.presets.get("chat")
        self.assertTrue(chat["builtin"])
        self.assertEqual(chat["perm"], "ask")
        off = self.presets.off_for(chat)
        self.assertIn("terminal", off)      # 一般聊天不給終端機
        self.assertIn("subagent", off)
        self.assertNotIn("files", off)      # 讀檔、搜尋留著
        # 只查不改：寫檔和跑指令都要擋
        ro = self.presets.get("readonly")
        self.assertEqual(ro["perm"], "readonly")
        self.assertIn("terminal", self.presets.off_for(ro))

    def test_custom_save_and_delete(self):
        self.presets.save([{"name": "隨手問", "desc": "只查", "perm": "readonly",
                            "plugins": ["files", "terminal", "不存在"]}])
        custom = [p for p in self.presets.all_presets() if not p["builtin"]]
        self.assertEqual(len(custom), 1)
        c = custom[0]
        self.assertEqual(c["name"], "隨手問")
        self.assertEqual(c["perm"], "readonly")
        self.assertEqual(c["plugins"], ["files", "terminal"])   # 不存在的插件被濾掉
        self.assertEqual(self.presets.get(c["id"])["name"], "隨手問")
        self.assertTrue(self.presets.delete(c["id"]))
        self.assertEqual([p for p in self.presets.all_presets() if not p["builtin"]], [])
        self.assertFalse(self.presets.delete(c["id"]))          # 刪不掉第二次

    def test_custom_keeps_builtin_id_safe(self):
        self.presets.save([{"id": "chat", "name": "假的一般聊天", "perm": "auto", "plugins": []},
                           {"name": "壞權限", "perm": "亂填", "plugins": ["files"]}])
        custom = [p for p in self.presets.all_presets() if not p["builtin"]]
        self.assertEqual(len(custom), 2)
        self.assertNotIn("chat", [c["id"] for c in custom])     # 不能蓋掉內建的
        self.assertEqual(self.presets.get("chat")["name"], "一般聊天")
        self.assertEqual([c["perm"] for c in custom if c["name"] == "壞權限"], ["ask"])  # 不行的權限退回先問我

    def test_default(self):
        self.assertIsNone(self.presets.for_new_session())
        self.assertEqual(self.presets.default_id(), "")
        self.presets.save([{"name": "寫東西", "perm": "edits", "plugins": ["files"]}])
        pid = [p for p in self.presets.all_presets() if not p["builtin"]][0]["id"]
        self.assertEqual(self.presets.set_default(pid), pid)
        self.assertEqual(self.presets.default_id(), pid)
        self.assertEqual(self.presets.for_new_session()["id"], pid)
        self.assertEqual(self.presets.set_default(""), "")      # 取消
        self.assertIsNone(self.presets.for_new_session())
        with self.assertRaises(ValueError):
            self.presets.set_default("沒有這個")
        # 預設被刪掉時，預設要跟著清掉
        self.presets.set_default(pid)
        self.presets.delete(pid)
        self.assertEqual(self.presets.default_id(), "")

    def test_apply_to_new_session(self):
        s = store.create_session("m", None, "新")
        self.presets.apply_to_new(s, self.presets.get("write"))
        got = store.get_session(s["id"])
        self.assertEqual(got["perm"], "edits")
        self.assertEqual(got["engine_state"]["preset"], "write")
        self.assertEqual(got["engine_state"]["plugins_off"], self.presets.off_for(self.presets.get("write")))
        self.assertNotIn("pending_off", got["engine_state"])     # 新對話直接換，不用排隊

    def test_plugin_switch_deferred_until_next_rebuild(self):
        """中途套用模式：權限馬上換，插件開關先排隊；指紋不變就不重組提示詞。"""
        from woof.agent import loop

        s = store.create_session("m", None, "t")
        sid = s["id"]
        store.add_message(sid, "user", [{"type": "text", "text": "哈囉"}])
        before = store.get_session(sid)
        off = self.presets.off_for(self.presets.get("write"))
        es = dict(before["engine_state"])
        es["pending_off"], es["pending_gen"] = off, 0
        es["preset"] = "write"
        queued = store.update_session(sid, perm="edits", engine_state=es)
        # 權限換了不該改到提示詞指紋（靠 settings_note 附在訊息後面）
        self.assertEqual(loop.pending_off(queued), off)
        self.assertEqual(loop.prompt_fp(types.SimpleNamespace(
            session=queued, extra_system="", cwd=queued.get("cwd") or "",
            model_cfg={"id": "m", "provider": "p"})),
            loop.prompt_fp(types.SimpleNamespace(
                session=before, extra_system="", cwd=before.get("cwd") or "",
                model_cfg={"id": "m", "provider": "p"})))
        # 套用後：插件開關真的換了、排隊清掉
        applied = loop.apply_pending(queued)
        self.assertEqual(applied["engine_state"]["plugins_off"], off)
        self.assertEqual(loop.pending_off(applied), [])
        # 換了插件開關，指紋要變（這就是為什麼要排隊）
        self.assertNotEqual(
            loop.prompt_fp(types.SimpleNamespace(session=applied, extra_system="", cwd="",
                                                 model_cfg={"id": "m", "provider": "p"})),
            loop.prompt_fp(types.SimpleNamespace(session=before, extra_system="", cwd="",
                                                 model_cfg={"id": "m", "provider": "p"})))

    def test_no_pending_is_noop(self):
        from woof.agent import loop

        s = store.create_session("m", None, "t")
        self.assertIs(loop.apply_pending(s), s)     # 沒排隊就原樣返回，不白寫一次資料庫
        self.assertEqual(loop.pending_off(s), [])


class Profiles(unittest.TestCase):
    """多組設定：每組的金鑰、模型、記憶、Skills、對話、排程各自獨立；預設那組不動。"""

    def setUp(self):
        from woof import config, profiles

        import shutil

        self.config, self.profiles = config, profiles
        self.made = []
        self.reg_bak = config.REGISTRY.read_text(encoding="utf-8") if config.REGISTRY.exists() else None
        # 從「只有預設」開始：清掉別的測試留下的設定組，這個類別才不會互相影響
        shutil.rmtree(config.PROFILES_DIR, ignore_errors=True)
        config.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        config.REGISTRY.write_text(
            json.dumps({"version": 1, "last": "default",
                        "profiles": [{"id": "default", "name": "預設", "created": 0}]}, ensure_ascii=False),
            encoding="utf-8")
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil

        for d in self.made:
            shutil.rmtree(d, ignore_errors=True)
        if self.reg_bak is None:
            try:
                self.config.REGISTRY.unlink()
            except OSError:
                pass
        else:
            self.config.REGISTRY.write_text(self.reg_bak, encoding="utf-8")

    def mk(self, name="工作"):
        p = self.profiles.create(name)
        self.made.append(p.dir)
        return p

    def test_default_only(self):
        doc = self.profiles.load()
        self.assertEqual([r["id"] for r in doc["profiles"]], ["default"])
        d = self.profiles.get("default")
        self.assertTrue(d.is_default)
        self.assertEqual(d.data, self.config.BASE_DATA)          # 預設就是專案本來的 data
        self.assertEqual(d.config, self.config.BASE_CFG / "config.toml")
        self.assertEqual(d.keys, self.config.BASE_CFG / "keys.toml")

    def test_create_is_isolated(self):
        p = self.mk()
        self.assertEqual(p.dir, self.config.PROFILES_DIR / p.id)
        self.assertTrue((p.dir / "config.toml").is_file())
        self.assertTrue((p.dir / "keys.toml").is_file())
        self.assertTrue((p.data / "prompts").is_dir())
        self.assertNotEqual(p.data, self.config.BASE_DATA)
        self.assertEqual(self.profiles.current().id, "default")   # 新增不會把目前這組切走
        self.assertEqual(self.profiles.last_used(), p.id)         # 但會記成「下次開這組」

    def test_new_config_parses(self):
        import tomllib

        p = self.mk()
        cfg = tomllib.loads(p.config.read_text(encoding="utf-8"))
        self.assertIn("workdir", cfg)
        self.assertEqual(cfg.get("models"), [])
        self.assertEqual(p.keys.read_text(encoding="utf-8").splitlines()[0][:2], "# ")

    def test_names(self):
        for bad in ["", "   ", "名字" * 11, "a/b", "a\\b", "a:b", "a?b"]:
            with self.assertRaises(ValueError, msg=bad):
                self.profiles.check_name(bad)
        self.assertEqual(self.profiles.check_name("  工作   Stuff "), "工作 Stuff")

    def test_create_bad_and_dup(self):
        with self.assertRaises(ValueError):
            self.mk("")               # 沒填名稱
        with self.assertRaises(ValueError):
            self.mk("   ")            # 空白名稱
        p = self.mk("工作")
        self.assertTrue(p.dir.is_dir())
        with self.assertRaises(ValueError):
            self.mk("工作")            # 同名不行
        self.assertTrue(self.mk("其他").dir.is_dir())
        with self.assertRaises(ValueError):
            self.mk("其他")            # 名稱不能重複

    def test_id_from_name(self):
        a = self.mk("Work Stuff")
        b = self.mk("work stuff")
        self.assertEqual(a.id, "work-stuff")
        self.assertEqual(b.id, "work-stuff-2")
        self.assertNotEqual(a.id, b.id)

    def test_rename(self):
        p = self.mk()
        self.assertEqual(self.profiles.rename(p.id, "  家庭  ").name, "家庭")
        with self.assertRaises(ValueError):
            self.profiles.rename("default", "亂改")     # 預設不能改名
        other = self.mk("第三組")
        with self.assertRaises(ValueError):
            self.profiles.rename(other.id, "家庭")       # 撞名不行
        with self.assertRaises(ValueError):
            self.profiles.rename("nope", "無名")

    def test_remove(self):
        p = self.mk()
        self.profiles.remove(p.id)
        self.assertFalse(p.dir.exists())
        self.assertEqual([r["id"] for r in self.profiles.load()["profiles"]], ["default"])
        self.assertEqual(self.profiles.last_used(), "default")    # 刪掉的正是 last → 退回預設
        with self.assertRaises(ValueError):
            self.profiles.remove("default")

    def test_switch_and_last(self):
        p = self.mk()
        self.profiles.set_last(p.id)
        self.assertEqual(self.profiles.load()["last"], p.id)
        with self.assertRaises(ValueError):
            self.profiles.set_last("nope")

    def test_bad_registry_falls_back(self):
        self.config.REGISTRY.write_text("{ 壞掉的 json", encoding="utf-8")
        self.assertEqual([r["id"] for r in self.profiles.load()["profiles"]], ["default"])
        self.assertEqual(self.profiles.current().id, "default")    # 原資料還在，不會不見

    def test_missing_registry_is_default(self):
        try:
            self.config.REGISTRY.unlink()
        except OSError:
            pass
        self.assertEqual(self.profiles.current().name, "預設")
        self.assertEqual(len(self.profiles.all_profiles()), 1)


class ConfigPathsByProfile(unittest.TestCase):
    """路徑看 WOOF_PROFILE：預設用專案本來的檔案，其他用 data/profiles/<id>/。"""

    CODE = ("import json;from woof import config, cron, store;"
            "print(json.dumps({'profile': config.PROFILE, 'data': str(config.DATA),"
            " 'config': str(config.CONFIG_PATH), 'keys': str(config.KEYS_PATH),"
            " 'cron': str(cron.FILE), 'db': str(store._DB)}))")

    def run_py(self, profile=None, data=None):
        import subprocess

        env = dict(os.environ)
        env["WOOF_DATA"] = data or TMP
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("WOOF_PROFILE", None)
        if profile:
            env["WOOF_PROFILE"] = profile
        r = subprocess.run([sys.executable, "-c", self.CODE], capture_output=True, text=True,
                           env=env, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_default(self):
        got = self.run_py()
        base = str(Path(TMP))
        self.assertEqual(got["profile"], "default")
        self.assertEqual(got["data"], base)
        self.assertEqual(got["config"], str(Path(base) / "config.toml"))
        self.assertEqual(got["keys"], str(Path(base) / "keys.toml"))
        self.assertEqual(got["cron"], str(Path(base) / "cron.json"))   # 排程跟著這組的 data
        self.assertEqual(got["db"], str(Path(base) / "woof.db"))       # 對話庫也是

    def test_profile_and_unknown(self):
        from woof import config, profiles

        bak = config.REGISTRY.read_text(encoding="utf-8") if config.REGISTRY.exists() else None
        p = profiles.create("獨立組")
        self.addCleanup(lambda: (self._rm(p.dir), self._back(bak)))

        got = self.run_py(profile=p.id)
        d = str(Path(TMP) / "profiles" / p.id / "data")
        self.assertEqual(got["profile"], p.id)
        self.assertEqual(got["data"], d)
        self.assertEqual(got["config"], str(Path(TMP) / "profiles" / p.id / "config.toml"))
        self.assertEqual(got["keys"], str(Path(TMP) / "profiles" / p.id / "keys.toml"))
        self.assertEqual(got["cron"], str(Path(d) / "cron.json"))
        self.assertEqual(got["db"], str(Path(d) / "woof.db"))
        self.assertNotEqual(got["data"], self.run_py()["data"])        # 跟預設組不同

        unknown = self.run_py(profile="不存在的組")
        self.assertEqual(unknown["profile"], "default")                # 認不得的 id 就回預設，不會炸掉

    def test_two_data_dirs_are_independent(self):
        import subprocess

        outs = []
        for name in ["woof_a", "woof_b"]:
            dd = tempfile.mkdtemp(prefix=name + "_")
            self.addCleanup(lambda dd=dd: __import__("shutil").rmtree(dd, ignore_errors=True))
            outs.append(self.run_py(data=dd))
        self.assertNotEqual(outs[0]["db"], outs[1]["db"])
        self.assertNotEqual(outs[0]["cron"], outs[1]["cron"])

    @staticmethod
    def _rm(d):
        import shutil

        shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _back(text):
        from woof import config

        if text is None:
            try:
                config.REGISTRY.unlink()
            except OSError:
                pass
        else:
            config.REGISTRY.write_text(text, encoding="utf-8")


class RemoteConn(unittest.TestCase):
    """對外連線：預設只綁本機、密碼只存雜湊、登入換憑證、憑證會過期、沒登入就擋。"""

    def setUp(self):
        self.real_secret = rconn._SECRET
        rconn._SECRET = b"t" * 32          # 固定金鑰，測試不寫 data/server_secret
        rconn.reset()

    def tearDown(self):
        rconn._SECRET = self.real_secret
        rconn.reset()

    def test_default_bind_is_local(self):
        st = rconn.configure({})
        self.assertEqual(st["bind"], "local")
        self.assertFalse(st["open_mode"])
        self.assertEqual(rconn.bind_host("local"), "127.0.0.1")
        self.assertEqual(rconn.startup_error(st), "")   # 只綁本機不用密碼也不用 HTTPS

    def test_local_mode_keeps_machine_token(self):
        rconn.configure({})
        self.assertTrue(rconn.check(False, "/api/state", "abc", "abc"))
        self.assertFalse(rconn.check(False, "/api/state", "abc", "別的"))
        self.assertTrue(rconn.check(False, "/api/state", "", ""))   # 後端沒拿到 TOKEN 時跟以前一樣全放行

    def test_password_is_hashed(self):
        h = rconn.hash_password("密碼 horse battery")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertNotIn("horse", h)
        self.assertTrue(rconn.verify_password("密碼 horse battery", h))
        self.assertFalse(rconn.verify_password("錯的密碼", h))
        self.assertFalse(rconn.verify_password("密碼 horse battery", "壞掉的字串"))
        self.assertFalse(rconn.verify_password("", h))

    def test_token_roundtrip_and_expiry(self):
        tok, exp = rconn.issue(30)
        self.assertGreater(exp, time.time())
        self.assertIsNotNone(rconn.verify(tok))
        self.assertIsNone(rconn.verify(tok[:-4] + "AAAA"))   # 簽章被動過就不認
        real = rconn.time

        class Later:
            @staticmethod
            def time():
                return real.time() + 40 * 86400

        rconn.time = Later
        try:
            self.assertIsNone(rconn.verify(tok), "過了期限的憑證要失效")
        finally:
            rconn.time = real

    def test_open_mode_blocks_without_login(self):
        st = rconn.configure({"server": {"bind": "lan", "password_hash": rconn.hash_password("pw-12345678")}})
        self.assertTrue(st["open_mode"])
        self.assertFalse(rconn.check(True, "/api/state", "", "machine"))
        self.assertFalse(rconn.check(True, "/api/state", "亂打的", "machine"))
        self.assertTrue(rconn.check(True, "/api/login", "", "machine"))     # 登入端點本身要進得去
        self.assertTrue(rconn.check(True, "/api/session", "", "machine"))
        tok, _ = rconn.issue(1)
        self.assertTrue(rconn.check(True, "/api/state", tok, "machine"))

    def test_refuse_open_without_setup(self):
        st = rconn.configure({"server": {"bind": "lan"}})
        self.assertIn("密碼", rconn.startup_error(st))
        st = rconn.configure({"server": {"bind": "public", "password_hash": rconn.hash_password("pw-12345678")}})
        self.assertIn("HTTPS", rconn.startup_error(st))
        st = rconn.configure({"server": {"bind": "public", "password_hash": rconn.hash_password("pw-12345678"),
                                         "trust_proxy": True}})
        self.assertEqual(rconn.startup_error(st), "")
        self.assertEqual(rconn.bind_host("public"), "0.0.0.0")


class RemoteHttp(unittest.TestCase):
    """真的打 API：沒登入被擋、密碼對才發憑證。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        from woof import server

        self.server = server
        self.real_secret = rconn._SECRET
        rconn._SECRET = b"t" * 32
        self.pw = "pw-12345678"
        rconn.configure({"server": {"bind": "public", "password_hash": rconn.hash_password(self.pw),
                                    "trust_proxy": True}})
        self.c = TestClient(server.app)

    def tearDown(self):
        rconn._SECRET = self.real_secret
        rconn.reset()

    def test_login_flow(self):
        self.assertEqual(self.c.get("/api/state").status_code, 401)                 # 沒帶憑證
        self.assertEqual(self.c.get("/api/session").status_code, 200)               # 開場問要不要登入
        self.assertTrue(self.c.get("/api/session").json()["open"])
        self.assertEqual(self.c.post("/api/login", json={"password": "錯的"}).status_code, 401)
        r = self.c.post("/api/login", json={"password": self.pw})
        self.assertEqual(r.status_code, 200)
        tok = r.json()["token"]
        self.assertTrue(tok)
        self.assertTrue(rconn.check(True, "/api/state", tok, ""))                   # 拿到的憑證能過


class Groups(unittest.TestCase):
    """對話群組：建立、排序、改名、收合、歸屬、刪群組時對話的處置。"""

    def setUp(self):
        from woof import store
        self.store = store
        self.made = []

    def tearDown(self):
        for gid in self.made:
            self.store.delete_group(gid)

    def mk(self, name):
        g = self.store.create_group(name)
        self.made.append(g["id"])
        return g

    def test_create_and_order(self):
        a = self.mk("第一個")
        b = self.mk("第二個")
        names = [g["name"] for g in self.store.list_groups()]
        self.assertLess(names.index("第一個"), names.index("第二個"))
        self.assertEqual(a["n"], 0)
        self.assertFalse(a["collapsed"])
        self.assertNotEqual(a["id"], b["id"])

    def test_rename_and_collapse(self):
        g = self.mk("舊名字")
        self.assertEqual(self.store.update_group(g["id"], name="新名字")["name"], "新名字")
        self.assertTrue(self.store.update_group(g["id"], collapsed=True)["collapsed"])
        self.assertFalse(self.store.update_group(g["id"], collapsed=False)["collapsed"])

    def test_session_belongs(self):
        g = self.mk("暫存")
        sid = self.store.create_session("m", "high")["id"]
        self.store.update_session(sid, grp=g["id"])
        self.assertEqual(self.store.get_session(sid)["grp"], g["id"])
        self.assertEqual([x["n"] for x in self.store.list_groups() if x["id"] == g["id"]], [1])
        self.assertEqual(self.store.group_session_ids(g["id"]), [sid])
        # 搬回未分組
        self.store.update_session(sid, grp=None)
        self.assertEqual(self.store.get_session(sid)["grp"], "")
        self.assertEqual(self.store.group_session_ids(g["id"]), [])
        self.store.delete_session(sid)

    def test_delete_group_keeps_sessions(self):
        g = self.mk("散掉的群組")
        sid = self.store.create_session("m", "high")["id"]
        self.store.update_session(sid, grp=g["id"])
        self.store.delete_group(g["id"])
        self.assertNotIn(g["id"], [x["id"] for x in self.store.list_groups()])
        self.assertEqual(self.store.get_session(sid)["grp"], "")
        self.store.delete_session(sid)

    def test_archived_not_counted(self):
        g = self.mk("封存的")
        sid = self.store.create_session("m", "high")["id"]
        self.store.update_session(sid, grp=g["id"])
        self.store.update_session(sid, archived=True)
        self.assertEqual([x["n"] for x in self.store.list_groups() if x["id"] == g["id"]], [0])
        self.store.delete_session(sid)



class Pet(unittest.TestCase):
    """寵物：解析模型輸出、設定夾範圍、不看整份對話。"""

    def up(self):
        from woof import pet

        return pet

    def test_parse_json(self):
        p = self.up()
        got = p.parse('```json\n{"say":"辛苦了","face":"comfort","mood":"心疼"}\n```')
        self.assertEqual(got["say"], "辛苦了")
        self.assertEqual(got["face"], "comfort")
        self.assertEqual(got["mood"], "心疼")

    def test_parse_bad_face_goes_happy(self):
        p = self.up()
        self.assertEqual(p.parse('{"say":"嗨","face":"dancing"}')["face"], "happy")
        self.assertEqual(p.parse("就一句話沒有 JSON")["say"], "就一句話沒有 JSON")
        self.assertEqual(p.parse("")["say"], "")

    def test_parse_quiet_glance(self):
        p = self.up()
        got = p.parse('{"say":"","face":"sleepy","mood":"想睡"}')
        self.assertEqual(got["say"], "")
        self.assertEqual(got["face"], "sleepy")

    def test_clean_clamps(self):
        p = self.up()
        got = p.clean({"every_sec": 0, "ctx": 99999999, "max_out": 5, "watch": "head"})
        self.assertEqual(got["every_sec"], 3)
        self.assertEqual(got["ctx"], 200000)
        self.assertEqual(got["max_out"], 40)
        self.assertNotIn("watch", got)  # 舊欄位不再收
        self.assertEqual(p.clean({"effort": "HIGH"})["effort"], "off")

    def test_est_tokens(self):
        p = self.up()
        self.assertGreater(p.est_tokens("一二三四五"), 4)
        self.assertLess(p.est_tokens("hello world"), 6)

    def test_system_lists_every_face(self):
        p = self.up()
        system = p.build_system({"effort": "off"}, "glance")
        for f in p.FACES:
            self.assertIn(f, system)
        self.assertIn("say", system)

    def test_parse_not_json_flagged(self):
        p = self.up()
        self.assertFalse(p.parse("汪！過來啦")["_ok"])
        self.assertTrue(p.parse('{"say":"好","face":"gentle"}')["_ok"])

    def test_chars_have_persona_and_sprites(self):
        from woof.config import WEB_DIST

        p = self.up()
        p._ensure()
        for cid in p.CHARS:
            self.assertTrue((p.CHARS_DIR / (cid + ".md")).exists(), cid)
            for f in p.FACES:
                self.assertTrue((WEB_DIST / "pet" / cid / (f + ".webp")).exists(), f"{cid}/{f}")

    def test_clean_accepts_char(self):
        p = self.up()
        self.assertEqual(p.clean({"char": "fish"})["char"], "fish")
        self.assertNotIn("char", p.clean({"char": "cat"}))

    def test_parse_multibubble_and_react(self):
        p = self.up()
        g = p.parse('{"say":["汪！","尾巴斷了"],"face":"cheer","mood":"開心","react":"🐾"}')
        self.assertEqual(g["say"].count("\n"), 1)
        self.assertEqual(g["react"], "🐾")
        self.assertTrue(g["_ok"])

    def test_parse_react_defaults_empty(self):
        p = self.up()
        self.assertEqual(p.parse('{"say":"汪"}')["react"], "")

    def test_fit_react_only_palette(self):
        p = self.up()
        self.assertEqual(p.fit_react("😂", "dog"), "😂")
        self.assertEqual(p.fit_react("❤", "dog"), "❤️")  # 沒有變體字元也算
        self.assertEqual(p.fit_react("🐾", "dog"), "🐾")
        self.assertEqual(p.fit_react("🐾", "fish"), "")  # 狗的招牌魚不能用
        self.assertEqual(p.fit_react("🍚", "fish"), "🍚")
        self.assertEqual(p.fit_react("🤖", "dog"), "")
        self.assertEqual(len(p.palette("dog")), 8)

    def test_length_hint_follows_user(self):
        p = self.up()
        self.assertIn("表情", p.length_hint("好"))
        self.assertIn("表情", p.length_hint("哈哈哈"))
        self.assertIn("二十", p.length_hint("在幹嘛"))
        self.assertIn("問你想法", p.length_hint("你覺得這個架構要不要先拆成兩塊？"))

    def test_user_react_toggles_and_changes_face(self):
        p = self.up()
        d = p._load()
        d["msgs"] = [{"id": "u1", "role": "user", "text": "嗨", "at": 1},
                     {"id": "p1", "role": "pet", "text": "汪！\n你回來了", "at": 2, "face": "happy"}]
        p._save(d)
        asyncio.run(p.user_react("p1", "😂"))
        m = p.history()[-1]
        self.assertEqual(m["ureact"], "😂")
        self.assertEqual(p.state()["face"], "laugh")
        asyncio.run(p.user_react("p1", "😂"))  # 同一個再按＝收回
        self.assertNotIn("ureact", p.history()[-1])
        self.assertIn("error", asyncio.run(p.user_react("nope", "😂")))
        # 牠看得到你按了什麼：寫在你下一句前面
        d = p._load()
        d["msgs"][-1]["ureact"] = "❤️"
        d["msgs"].append({"id": "u2", "role": "user", "text": "好乖", "at": 3})
        p._save(d)
        conv = p._own_messages(p.cfg())
        self.assertEqual(conv[0]["role"], "user")
        self.assertIn("按了 ❤️", conv[-1]["content"][0]["text"])
        p.reset_chat()

    def test_personas_follow_new_defaults(self):
        p = self.up()
        p._ensure()
        for cid in p.CHARS:
            text = (p.CHARS_DIR / (cid + ".md")).read_text(encoding="utf-8")
            self.assertNotIn("魚片", text)
            self.assertIn("## 示範", text)

    def test_simplified_leak_becomes_tw(self):
        p = self.up()
        g = p.parse('{"say":["这里好乱","然后呢"],"face":"think","mood":"头痛","sug":"先弄登录吧"}')
        self.assertEqual(g["say"], "這裡好亂\n然後呢")
        self.assertEqual(g["sug"], ["先弄登錄吧"])
        self.assertEqual(g["mood"], "頭痛")
        from woof.zh_tw import to_tw
        self.assertEqual(to_tw("喂！後面裡面台灣公里"), "喂！後面裡面台灣公里")

    def test_clean_quick(self):
        p = self.up()
        self.assertEqual(p.clean({"quick": "嗯嗯|然後呢"})["quick"], ["嗯嗯", "然後呢"])
        self.assertEqual(p.clean({"quick": ["a", "b", "c", "d", "e"]})["quick"], ["a", "b", "c", "d"])
        self.assertNotIn("quick", p.clean({"quick": "  "}))
        self.assertIn("quick", p.cfg())

    def test_parse_sug(self):
        p = self.up()
        got = p.parse('{"say":"去睡吧","face":"gentle","mood":"心疼","sug":["好，晚安","你也早點睡"]}')
        self.assertEqual(got["sug"], ["好，晚安"])  # 只留一顆模型建議
        self.assertEqual(p.parse('{"say":"嗨"}')["sug"], [])
        self.assertEqual(p.parse('{"say":"嗨","sug":"單獨一句"}')["sug"], ["單獨一句"])

    def test_reply_not_forced_short(self):
        p = self.up()
        system = p.build_system({"effort": "off"}, "talk", p.length_hint("今天好累"))
        self.assertNotIn("三十", system)
        self.assertIn("sug", system)


    def test_faces_have_images(self):
        p = self.up()
        for cid in p.CHARS:
            for f in p.FACES:
                self.assertIsNotNone(p.sprite(f, cid), f"{cid}/{f}")


class FocusNote(unittest.TestCase):
    """重點便條：只在壓縮把狀態摘要掉之後接回來一次；平常不貼。"""

    def mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("focus_plugin", ROOT / "plugins" / "focus" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def hook(self):
        m = self.mod()
        got = {}

        class W:
            settings = types.SimpleNamespace(get=lambda self, k, d=None: d)

            def on(self, ev, fn, order=0):
                got[ev] = fn

        m.register(W())
        return got["before_request"]

    def session(self, todos=True, cc=0):
        st = {"engine_state": {"todos": [
            {"text": "修好 UI", "status": "done"}, {"text": "寫測試", "status": "doing"}]}} if todos else {}
        if cc:
            st["engine_state"]["compress_count"] = cc
        s = store.create_session("m", None, "便條測試")
        store.add_message(s["id"], "user", [{"type": "text", "text": "把 UI 修好"}])
        store.update_session(s["id"], **st)
        return store.get_session(s["id"])

    def ctx(self, sid, step=2):
        return types.SimpleNamespace(session_id=sid, step=step, max_steps=60, mid_turn=False, focus_note=None)

    def test_no_request_restated(self):
        m = self.mod()
        sess = self.session()
        text, _ = m.build(sess, store.get_messages(sess["id"]), 1, 60, 300)
        self.assertIn("待辦 1/2", text)
        self.assertNotIn("這回合的要求", text)
        self.assertNotIn("把 UI 修好", text)  # 需求不重貼：它本來就在對話裡

    def test_sig_ignores_step(self):
        m = self.mod()
        sess = self.session()
        _, a = m.build(sess, store.get_messages(sess["id"]), 1, 60, 300)
        _, b = m.build(sess, store.get_messages(sess["id"]), 7, 60, 300)
        self.assertEqual(a, b)  # 只有步數變不算狀態變

    def test_nothing_to_say(self):
        m = self.mod()
        sess = self.session(todos=False)
        text, _ = m.build(sess, store.get_messages(sess["id"]), 3, 60, 300)
        self.assertEqual(text, "")  # 沒東西可說就不附

    def test_attach_only_after_compress(self):
        hook = self.hook()
        sess = self.session()
        msgs = [{"id": 1, "role": "user", "content": [{"type": "text", "text": "把 UI 修好"}]}]
        ctx = self.ctx(sess["id"])
        self.assertIsNone(hook(list(msgs), ctx))        # 平常：不貼
        self.assertIsNone(hook(list(msgs), ctx, ))      # 下一步：還是不貼
        es = dict(store.get_session(sess["id"])["engine_state"])
        es["compress_count"] = 1                        # 剛剛壓縮過
        store.update_session(sess["id"], engine_state=es)
        out = hook(list(msgs), ctx)
        self.assertIsNotNone(out)                       # 壓縮後：接回來一次
        self.assertEqual(out[-1]["content"][-1]["type"], "note")
        self.assertIsNone(hook(list(msgs), ctx))        # 再下一步：不重貼


class AskSkip(unittest.TestCase):
    """使用者直接打字送訊息時，還在等的提問要一起結掉，不能讓回合卡在等那張卡。"""

    def test_skip_only_that_session(self):
        from woof.agent import tasks

        loop = asyncio.new_event_loop()
        try:
            f1, f2, f3 = loop.create_future(), loop.create_future(), loop.create_future()
            f3.set_result("already")
            tasks.ASKS["t1"] = {"future": f1, "session_id": "s1"}
            tasks.ASKS["t2"] = {"future": f2, "session_id": "s2"}
            tasks.ASKS["t3"] = {"future": f3, "session_id": "s1"}
            self.assertEqual(tasks.skip_ask("s1"), 1)
            self.assertTrue(f1.done())
            self.assertEqual(f1.result(), "（使用者直接回話，跳過這個問題）")
            self.assertFalse(f2.done())
            self.assertEqual(tasks.skip_ask("s1"), 0)   # 已經結掉的不重複算
        finally:
            tasks.ASKS.clear()
            loop.close()

    def test_has_ask_and_typed_answer(self):
        from woof.agent import tasks

        loop = asyncio.new_event_loop()
        try:
            f1 = loop.create_future()
            tasks.ASKS["a1"] = {"future": f1, "session_id": "s1"}
            self.assertTrue(tasks.has_ask("s1"))
            self.assertFalse(tasks.has_ask("s2"))
            # 使用者打的那句話要原樣當成答案（不是罐頭訊息）
            self.assertEqual(tasks.skip_ask("s1", "我要處理 WOOF 的顯示問題"), 1)
            self.assertEqual(f1.result(), "我要處理 WOOF 的顯示問題")
            self.assertFalse(tasks.has_ask("s1"))       # 結掉之後就不算在等
        finally:
            tasks.ASKS.clear()
            loop.close()


class MemoryPerms(unittest.TestCase):
    """memory 的讀與寫分開：讀＝記憶與使用者偏好放進系統提示，寫＝模型自己改記憶檔。"""

    def mod(self, flags):
        import importlib.util
        spec = importlib.util.spec_from_file_location("memory_plugin", ROOT / "plugins" / "memory" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        got = {}

        class Sett:
            def get(self, k, d=None):
                return flags.get(k, d)

        class W:
            settings = Sett()

            def on(self, ev, fn, order=0):
                got.setdefault(ev, []).append(fn)

            def add_tool(self, name, desc, schema, fn):
                got["tool"] = fn

        mod.register(W())
        return got

    def test_category_splits_read_and_write(self):
        self.assertEqual(perms.category("memory", {"action": "list"}), "read")
        for a in ("add", "replace", "remove", ""):
            self.assertEqual(perms.category("memory", {"action": a}), "write")
        self.assertEqual(perms.category("memory", None), "write")   # 沒帶 action 當寫，寧可保守
        self.assertEqual(perms.category("terminal", {"command": "ls"}), "exec")  # 其他工具不受影響

    def test_read_only_mode_still_reads_memory(self):
        REG.config = {"permissions": {}}
        self.assertEqual(perms.decide("readonly", "memory", {"action": "list"})["decision"], "auto")
        self.assertEqual(perms.decide("readonly", "memory", {"action": "add", "text": "x"})["decision"], "deny")

    def test_write_off_still_reads(self):
        run = self.mod({"allow_read": True, "allow_write": False})["tool"]
        out = asyncio.run(run({"action": "add", "text": "x"}, None))
        self.assertTrue(out.get("is_error"))
        self.assertIn("關掉", out["output"])
        listed = asyncio.run(run({"action": "list"}, None))
        self.assertFalse(listed.get("is_error"))   # 只關寫，讀照常

    def test_read_off_hides_prompt_and_list(self):
        from woof import promptfiles

        data = {"soul": "人格", "instructions": "規則", "user": "偏好甲", "memory": "記憶乙"}
        with unittest.mock.patch.object(promptfiles, "read", lambda k: data.get(k, "")), \
                unittest.mock.patch.object(promptfiles, "memory_switch", lambda k, d=True: k != "allow_read"):
            text_off = promptfiles.build_prompt_gated()
            self.assertNotIn("記憶乙", text_off)
            self.assertNotIn("偏好甲", text_off)
            self.assertIn("人格", text_off)          # 人物檔、設定檔不受影響
        with unittest.mock.patch.object(promptfiles, "read", lambda k: data.get(k, "")), \
                unittest.mock.patch.object(promptfiles, "memory_switch", lambda k, d=True: True):
            self.assertIn("記憶乙", promptfiles.build_prompt_gated())
        out = asyncio.run(self.mod({"allow_read": False, "allow_write": True})["tool"]({"action": "list"}, None))
        self.assertTrue(out.get("is_error"))

    def test_prompt_files_are_core_not_plugin(self):
        """關掉 memory 插件只是少了工具；提示詞檔照樣進系統提示（不再掛在插件掛鉤上）。"""
        src = (ROOT / "plugins" / "memory" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn('"build_prompt"', src)
        loop_src = (ROOT / "woof" / "agent" / "loop.py").read_text(encoding="utf-8")
        self.assertIn("build_prompt_gated", loop_src)


class PromptLimits(unittest.TestCase):
    """字數上限：預設值在程式裡，使用者在設定頁改的值存在 data/prompt_limits.json。"""

    def setUp(self):
        from woof import promptfiles
        self.pf = promptfiles
        if promptfiles.LIMITS_FILE.is_file():
            promptfiles.LIMITS_FILE.unlink()

    def test_default_and_override(self):
        pf = self.pf
        self.assertEqual(pf.limit("memory"), pf.LIMITS["memory"])
        pf.set_limit("memory", 6000)
        self.assertEqual(pf.limit("memory"), 6000)
        self.assertEqual(pf.limits()["user"], pf.LIMITS["user"])   # 沒改的還是預設
        self.assertEqual(pf.info("memory")["limit"], 6000)
        pf.set_limit("memory", pf.LIMITS["memory"])                 # 改回去
        self.assertEqual(pf.limit("memory"), pf.LIMITS["memory"])

    def test_rejects_bad_values(self):
        pf = self.pf
        for bad in (0, 10, 999999, "abc", None):
            with self.assertRaises(ValueError):
                pf.set_limit("memory", bad)
        with self.assertRaises(KeyError):
            pf.set_limit("nope", 4000)

    def test_memory_tool_respects_limit(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("memory_plugin_lim", ROOT / "plugins" / "memory" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        got = {}

        class Sett:
            def get(self, k, d=None):
                return d

        class W:
            settings = Sett()

            def on(self, ev, fn, order=0):
                got.setdefault(ev, []).append(fn)

            def add_tool(self, name, desc, schema, fn):
                got["tool"] = fn

        mod.register(W())
        self.pf.set_limit("memory", 200)
        out = asyncio.run(got["tool"]({"action": "add", "text": "字" * 300}, None))
        self.assertTrue(out.get("is_error"))
        self.assertIn("200", out["output"])

    def test_broken_file_falls_back_to_defaults(self):
        pf = self.pf
        pf.LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pf.LIMITS_FILE.write_text("{ 壞掉的 json", encoding="utf-8")
        self.assertEqual(pf.limit("memory"), pf.LIMITS["memory"])


class GoalFlow(unittest.TestCase):
    """目標／方案流程：步驟 1～5、每步不再逐步子代理審核、最後只對抗檢查一次。"""

    def test_parse_check(self):
        from woof.agent import tasks

        ok, gaps, _ = tasks._parse_check('{"pass": true, "gaps": []}')
        self.assertTrue(ok)
        self.assertEqual(gaps, [])
        ok, gaps, _ = tasks._parse_check('檢查完了\n{"pass": false, "gaps": ["沒有跑測試", "少改一個檔"]}')
        self.assertFalse(ok)
        self.assertEqual(gaps, ["沒有跑測試", "少改一個檔"])
        ok, gaps, why = tasks._parse_check('我覺得還行')          # 格式不對：不能當成通過
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_gaps_capped_at_three(self):
        from woof.agent import tasks

        ok, gaps, _ = tasks._parse_check('{"pass": false, "gaps": ["a", "b", "c", "d", "e"]}')
        self.assertFalse(ok)
        self.assertEqual(len(gaps), 3)

    def test_recent_report_includes_tool_output(self):
        from woof.agent import tasks

        s = store.create_session("m", None, "對抗檢查")
        store.add_message(s["id"], "assistant", [{"type": "text", "text": "檔案改好了"}])
        store.add_message(s["id"], "tool", [{"type": "tool_result", "output": "已寫入 a.py（3 行）"}])
        rep = tasks._recent_report(s["id"])
        self.assertIn("檔案改好了", rep)
        self.assertIn("已寫入 a.py", rep)          # 證據要帶給檢查員，不只執行者的說法


class AskSkipButton(unittest.TestCase):
    """ask 卡：空白按送出一樣是略過；略過要留下「已略過」的痕跡。"""

    def test_empty_submit_is_skip(self):
        import re as _re

        js = open(r"C:\Users\user\hermes-files\WOOF\web\dist\app.js", encoding="utf-8").read()
        self.assertIn("answerAsk(id, v || ASK_SKIP, !v)", js)      # 空白送出＝略過
        self.assertIn("data-askskip", js)                          # 卡片上有略過按鈕
        self.assertIn("已略過", js)                                 # 答完留在畫面上

    def test_skip_marks_flag(self):
        from woof.agent import tasks

        loop = asyncio.new_event_loop()
        try:
            f = loop.create_future()
            tasks.ASKS["a9"] = {"future": f, "session_id": "s9", "skipped": False}
            tasks.skip_ask("s9", "（略過這題，你自己判斷決定）")
            self.assertTrue(tasks.ASKS["a9"]["skipped"])
        finally:
            tasks.ASKS.clear()
            loop.close()


class EffortLadder(unittest.TestCase):
    """推理強度檔位統一一組：最小／低／中／高／極高／最高／超高（不再混英文 MAX）。"""

    def test_scale_has_seven_levels(self):
        from woof import effort

        self.assertEqual(effort.SCALE, ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"))

    def test_ultra_falls_back_to_model_max(self):
        from woof import effort

        self.assertEqual(effort.resolve(["low", "high", "max"], "ultra"), "max")
        self.assertEqual(effort.resolve(["low", "high", "max"], "xhigh"), "high")
        self.assertEqual(effort.resolve(["low", "high", "max"], "minimal"), "low")

    def test_ui_labels_are_chinese(self):
        js = open(r"C:\Users\user\hermes-files\WOOF\web\dist\app.js", encoding="utf-8").read()
        self.assertIn("max: '最高'", js)
        self.assertIn("ultra: '超高'", js)
        self.assertNotIn("max: 'MAX'", js)          # 不要中文清單裡突然冒一個英文
        self.assertIn("'xhigh', 'max', 'ultra'", js)


class BasePrompt(unittest.TestCase):
    """系統提示只留必要的事：身分是軟體名，不是把人定死成執行者；不加額外的禁令。"""

    def test_no_agent_framing(self):
        from woof.agent import loop

        self.assertNotIn("個人 AI agent", loop.BASE_PROMPT)
        self.assertNotIn("personal AI agent", loop.BASE_PROMPT_EN)
        self.assertIn("使用者的要求優先", loop.BASE_PROMPT)
        self.assertIn("The user's actual request comes first", loop.BASE_PROMPT_EN)

    def test_stays_short(self):
        from woof.agent import loop

        # 剃刀原則：只有身分、語言、工具、驗證、環境、輸出位置六條，不再往上長
        for p in (loop.BASE_PROMPT, loop.BASE_PROMPT_EN):
            self.assertLessEqual(len([l for l in p.splitlines() if l.startswith("- ")]), 5)
            self.assertLess(len(p), 700)

    def test_chinese_is_first_rule(self):
        from woof.agent import loop

        lines = [l for l in loop.BASE_PROMPT.splitlines() if l.startswith("- ")]
        self.assertTrue(lines[0].startswith("- 用繁體中文回覆"))


class IdentityTab(unittest.TestCase):
    """身份與環境要能在設定頁看到、改得動（設定頁分頁走 TABS，不被 build_prompt 的 ORDER 綁住）。"""

    def test_tabs_has_identity_first(self):
        from woof import promptfiles

        self.assertEqual(promptfiles.TABS[0], "identity")
        self.assertNotIn("identity", promptfiles.ORDER)  # 不重複塞進系統提示

    def test_edit_changes_base_prompt(self):
        from woof import promptfiles
        from woof.agent import loop

        promptfiles.write("identity", "你是測試身分。")
        self.assertEqual(loop.base_prompt(), "你是測試身分。")
        self.assertEqual(promptfiles.info("identity")["label"], "身份與環境")

class UploadsGC(unittest.TestCase):
    """貼圖（data/uploads）是快取：對話刪掉要跟著刪，沒人引用的要回收。"""

    def test_gc_keeps_referenced_only(self):
        from woof import store, uploads

        sid = store.create_session("m", "high", "測試")["id"]
        keep = uploads.DIR / "aaaa11112222.png"
        junk = uploads.DIR / "bbbb33334444.png"
        for p in (keep, junk):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 10)
        store.add_message(sid, "user", [{"type": "image", "path": str(keep.resolve())}])

        self.assertEqual(uploads.files_of(sid), [keep])
        n, size = uploads.gc()
        self.assertGreaterEqual(n, 1)
        self.assertTrue(keep.is_file())
        self.assertFalse(junk.is_file())
        uploads.remove(uploads.files_of(sid))
        self.assertFalse(keep.is_file())

class PasteToFile(unittest.TestCase):
    """貼上的長文存成檔案（data/pastes），跟貼圖同一條生命週期，但專案產出永遠不碰。"""

    def test_paste_saved_and_referenced(self):
        from woof import store, uploads

        sid = store.create_session("m", "high", "貼上測試")["id"]
        p = uploads.save_paste("x" * 5000)
        self.assertEqual(p.parent.name, "pastes")
        store.add_message(sid, "user", [{"type": "text", "text": f"（貼上內容 5 KB，已存成檔案：{p.resolve()}）"}])
        self.assertIn(p, uploads.files_of(sid))
        uploads.remove(uploads.files_of(sid))
        self.assertFalse(p.exists())

    def test_gc_never_touches_outputs(self):
        from woof import uploads
        from woof.config import DATA

        keepdir = DATA / "report"
        keepdir.mkdir(parents=True, exist_ok=True)
        out = keepdir / "gc_probe.md"
        out.write_text("產出", encoding="utf-8")
        orphan = uploads.save_paste("沒人引用")
        uploads.gc()
        self.assertTrue(out.is_file(), "gc 不准碰專案／產出資料夾")
        self.assertFalse(orphan.exists())
        out.unlink()


class MidTurnSteer(unittest.TestCase):
    """子代理跑的時候可以追加要求；主對話執行中 Enter 預設插話。"""

    def test_agent_runs_registered(self):
        from woof.agent import loop

        self.assertTrue(hasattr(loop, "AGENT_RUNS"))
        src = open(loop.__file__, encoding="utf-8").read()
        self.assertIn("AGENT_RUNS.add(ctx.session_id)", src)
        self.assertIn("AGENT_RUNS.discard(ctx.session_id)", src)

    def test_server_routes_steer_to_agent(self):
        from woof import server

        src = open(server.__file__, encoding="utf-8").read()
        self.assertIn("agent_running = (not running) and sid in AGENT_RUNS", src)
        self.assertIn("已把追加要求送給子代理", src)

    def test_busy_default_is_steer(self):
        js = open(r"C:\Users\user\hermes-files\WOOF\web\dist\app.js", encoding="utf-8").read()
        self.assertIn("includes(v) ? v : 'steer'", js)

class AskDonePayload(unittest.TestCase):
    """ask_done 要帶答案跟「是不是略過」，不然前端只會留一張空白的已回答卡。"""

    def _run(self, answer_fn):
        import asyncio
        from types import SimpleNamespace
        from woof.agent import tasks

        events = []

        async def emit(ev):
            events.append(ev)

        turn = SimpleNamespace(session_id="asktest", emit=emit)

        async def go():
            t = asyncio.create_task(tasks.ask(turn, "這張圖你想我做什麼？", [], timeout=5))
            for _ in range(50):
                await asyncio.sleep(0.01)
                ids = [k for k, v in tasks.ASKS.items() if v.get("session_id") == "asktest"]
                if ids:
                    answer_fn(ids[0])
                    break
            return await t

        got = asyncio.run(go())
        done = [e for e in events if e.get("type") == "ask_done"][-1]
        return got, done

    def test_answer_carried_back(self):
        from woof.agent import tasks

        got, done = self._run(lambda aid: tasks.skip_ask("asktest", "好色"))
        self.assertEqual(got, "好色")
        self.assertEqual(done.get("answer"), "好色")
        self.assertFalse(done.get("skipped"))

    def test_skip_flagged(self):
        from woof.agent import tasks

        got, done = self._run(lambda aid: tasks.resolve_ask(aid, "略過"))
        self.assertEqual(got, "略過")
        self.assertEqual(done.get("answer"), "略過")
        self.assertTrue(done.get("skipped"))

class BackgroundSubagent(unittest.TestCase):
    """派出子代理不該讓主對話停下來等；同一個子代理要能接續。"""

    def test_non_blocking_and_continue(self):
        from woof.agent import loop

        sid = store.create_session("m", "high", "主對話")["id"]

        async def emit(ev):
            return None

        parent = types.SimpleNamespace(session={"id": sid, "parent": None}, session_id=sid,
                                       model_cfg={"id": "m", "provider": "p"}, emit=emit, cwd=str(ROOT),
                                       tool_exclude=set(), root_session_id=sid, turn_mid=0)

        async def fake_loop(ctx):
            await asyncio.sleep(0.5)
            store.add_message(ctx.session_id, "assistant", [{"type": "text", "text": "做完了"}])

        injected = []

        async def fake_inject(s, kind, text):
            injected.append((s, kind, text))

        model = {"id": "m", "provider": "p", "label": "M", "default_effort": "high"}

        async def go():
            with unittest.mock.patch.dict(loop.REG.providers, {"p": object()}), \
                 unittest.mock.patch.object(loop, "run_model_loop", fake_loop), \
                 unittest.mock.patch.object(loop.REG, "inject", fake_inject, create=True), \
                 unittest.mock.patch.object(loop, "aux_model", lambda role, cfg: dict(model)):
                t0 = time.monotonic()
                info = await loop.run_subagent_bg(parent, "測試任務")
                waited = time.monotonic() - t0
                first = info["session_id"]

                # 接續同一個子代理：同一個對話，不要重開
                again = await loop.run_subagent_bg(parent, "再補一句", session_id=first,
                                                   kind="subagent_followup")
                for _ in range(60):
                    if not loop.BG_TASKS:
                        break
                    await asyncio.sleep(0.05)
                return waited, info, first, again, [m for m in store.get_messages(first)]

        waited, info, first, again, msgs = asyncio.run(go())
        self.assertLess(waited, 0.4, "delegate 不能等子代理跑完")
        self.assertTrue(info["background"])
        self.assertNotEqual(first, sid, "子代理要有自己的對話")
        self.assertEqual(again["session_id"], first, "接續要用同一個子代理對話")
        self.assertGreaterEqual(len(msgs), 2, "接續的訊息要進同一份上下文")
        self.assertTrue(any("回報" in t for _, _, t in injected), "跑完要把報告送回主對話")

class OpenStateKept(unittest.TestCase):
    """手動展開的 details（內部指令、子代理清單、壓縮記號）重畫後不能被收起來。"""

    def test_chat_details_remember_open(self):
        js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()
        self.assertIn("const itk = 'internal-' + m.id;", js)
        self.assertIn("det.dataset.tk = itk;", js)
        self.assertIn("det.open = THINK_OPEN.has(itk);", js)
        # 每個 delegate_task 各自記，開一個不會把其他的一起打開
        self.assertIn("bar.dataset.tk = 'subfold-' + (call.id || S.sid || '');", js)
        self.assertIn("det.dataset.tk = 'cmp-' + (S.sid || '');", js)

class ChatUx29(unittest.TestCase):
    """第二十九輪：子代理分兩疊、捲動錨點、背景還在跑、右鍵、本機路徑連結、便條預設關。"""

    def setUp(self):
        self.js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()

    def test_frontend_pieces(self):
        js = self.js
        for s in ("function subKind(", "function subBatches(", "async function deleteSubs(", "function chatAnchor(",
                  "function chatRestore(", "function innerScrolls(", "function restoreInnerScrolls(",
                  "function renderBgRun(", "function linkPaths(", "function chatCtxItems(", "function copyImageSrc(",
                  "function openFileLink("):
            self.assertIn(s, js)
        self.assertEqual(js.count("function subKind("), 1)

    def test_subagent_tagging(self):
        from woof.agent import loop
        self.assertEqual(loop.SUB_KIND.get("review_request"), "review")
        self.assertEqual(loop.SUB_KIND.get("expand_request"), "review")
        src = open(r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\subagent\\__init__.py", encoding="utf-8").read()
        self.assertIn("SUB_BATCH.set(", src)

    def test_focus_off_by_default(self):
        import tomllib
        from woof import presets
        t = tomllib.loads(open(r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\focus\\plugin.toml", encoding="utf-8").read())
        self.assertFalse(t["enabled"])
        self.assertNotIn('"focus"', open(presets.__file__, encoding="utf-8").read())


class AttachFiles(unittest.TestCase):
    """附件（xlsx／pdf 之類）要能附加，存進 data/files，跟對話一起清。"""

    def test_save_files_and_notes(self):
        import base64
        from woof import server as srv, store, uploads

        raw = b"PK\x03\x04" + b"x" * 200
        blocks = srv._save_files([{"name": "股票數據爬取統整.xlsx", "data": base64.b64encode(raw).decode(),
                                   "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}])
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["type"], "file")
        self.assertEqual(b["bytes"], len(raw))
        p = Path(b["path"])
        self.assertEqual(p.parent.name, "files")
        self.assertEqual(p.suffix, ".xlsx")
        self.assertEqual(p.read_bytes(), raw)

        notes = srv._file_notes(blocks)
        self.assertTrue(notes and "股票數據爬取統整.xlsx" in notes[0]["text"])
        self.assertIn(b["path"], notes[0]["text"])

        # 對話引用它 → gc 不能刪；沒引用 → 可以刪
        sid = store.create_session("m", "high", "附件測試")["id"]
        store.add_message(sid, "user", blocks + notes)
        self.assertIn(p, uploads.files_of(sid))
        uploads.gc()
        self.assertTrue(p.is_file(), "被引用的附件不能被回收")
        uploads.remove(uploads.files_of(sid))
        self.assertFalse(p.exists(), "刪對話要連附件一起刪")

    def test_regex_handles_file_names(self):
        from woof import uploads

        s = r'{"type": "file", "path": "C:\\Users\\u\\data\\files\\ab12cd34ef56.xlsx"}'
        self.assertEqual(uploads.names_in(s), {"files/ab12cd34ef56.xlsx"})

class XlsxDigest(unittest.TestCase):
    """xlsx 用 read_file 一次看懂：工作表、欄位、前幾列（不必開一堆 terminal）。"""

    def _mk(self, tmp):
        import zipfile
        path = Path(tmp) / "t.xlsx"
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("xl/workbook.xml",
                       f'<workbook {ns} {rel}><sheets>'
                       '<sheet name="持股" sheetId="1" r:id="rId1"/>'
                       '<sheet name="回測" sheetId="2" r:id="rId2"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                       '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
            z.writestr("xl/sharedStrings.xml",
                       f'<sst {ns}><si><t>代號</t></si><si><t>股數</t></si><si><t>00687B</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml",
                       f'<worksheet {ns}><dimension ref="A1:B2"/><sheetData>'
                       '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                       '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1437800</v></c></row>'
                       '</sheetData></worksheet>')
            z.writestr("xl/worksheets/sheet2.xml",
                       f'<worksheet {ns}><dimension ref="A1:A1"/><sheetData>'
                       '<row r="1"><c r="A1"><v>42</v></c></row></sheetData></worksheet>')
        return path

    def test_digest(self):
        import tempfile
        from plugins_files_probe import _xlsx_digest
        with tempfile.TemporaryDirectory() as tmp:
            path = self._mk(tmp)
            s = _xlsx_digest(path, 20, 1)
            self.assertIn("[1] 持股", s)
            self.assertIn("代號|股數", s)
            self.assertIn("00687B|1437800", s)
            self.assertIn("工作表：1 持股 / 2 回測", s)
            s2 = _xlsx_digest(path, 20, 2)
            self.assertIn("[2] 回測", s2)
            self.assertNotIn("[1] 持股", s2)


class ResumeNotShownWhileBusy(unittest.TestCase):
    """還在跑（方案／對抗／子代理／待答）時不能顯示「上一回合沒做完」，那會讓人以為卡死。"""

    def test_js_and_server(self):
        js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()
        self.assertIn("(cur() || {}).busy", js)
        self.assertIn("bar._h === h", js)
        sv = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\server.py", encoding="utf-8").read()
        self.assertIn("def _busy(", sv)
        self.assertIn('ev["busy"] = _busy(sid)', sv)
        css = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.css", encoding="utf-8").read()
        self.assertIn(".resumecard{user-select:text}", css)

class ReviewAlwaysRuns(unittest.TestCase):
    """按了方案／對抗又真的拆了步驟，最後一定要派一次審核模型（純問答在前面就 return 了）。"""

    def test_no_changed_paths_guard(self):
        src = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\agent\\tasks.py", encoding="utf-8").read()
        self.assertIn("ok, gaps = await final_check(ctx, session_id, objective, steps, emit)", src)
        self.assertNotIn("if changed_paths(session_id):\n            ok, gaps", src)

class ToolErrPreview(unittest.TestCase):
    """工具失敗要直接寫出原因（不必點開），點一下才展開完整輸出。"""

    def test_js_has_inline_error(self):
        js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()
        self.assertIn("function errPreview(", js)
        self.assertIn("eb.className = 'terr'", js)
        self.assertIn("el.insertBefore(eb, $('pre', el))", js)
        css = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.css", encoding="utf-8").read()
        self.assertIn(".tool .terr{", css)

class ResumeDigest(unittest.TestCase):
    """「繼續」要把做過的事交給模型，不要讓它從頭逛專案。"""

    def test_digest_has_state(self):
        from woof import store
        from woof.agent import tasks

        sid = store.create_session("m", "high", "接續摘要")["id"]
        store.add_message(sid, "user", [{"type": "text", "text": "把 excel 看完給我策略"}])
        store.add_message(sid, "assistant", [{"type": "tool_call", "id": "c1", "name": "write_file",
                                              "args": {"path": "x.py"}}])
        store.add_message(sid, "tool", [{"type": "tool_result", "id": "c1", "name": "write_file",
                                         "output": "已寫入 C:\\tmp\\x.py（10 bytes）", "is_error": False}])
        store.set_engine_state = getattr(store, "set_engine_state", None)
        d = tasks.resume_digest(sid)
        self.assertIn("不必重新探索", d)
        self.assertIn("這次要做的事：把 excel 看完給我策略", d)
        self.assertIn("x.py", d)
        self.assertLess(len(d), 6000, "摘要不能比重新探索還貴")

    def test_continue_uses_digest(self):
        sv = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\server.py", encoding="utf-8").read()
        self.assertIn("_tasks.resume_digest(sid)", sv)

class StopOneSubagent(unittest.TestCase):
    """子代理要能單獨停掉，不用切到它的分頁，也不影響主對話。"""

    def test_stop_agent_cancels_only_that_one(self):
        import asyncio
        from woof.agent import loop

        async def main():
            started = asyncio.Event()

            async def job():
                started.set()
                await asyncio.sleep(30)

            t = asyncio.create_task(job())
            await started.wait()
            loop.BG_TASKS.add(t)
            loop.BG_BY_SID["childA"] = t
            t.add_done_callback(lambda _t: (loop.BG_TASKS.discard(_t), loop.BG_BY_SID.pop("childA", None)))
            self.assertTrue(loop.stop_agent("childA"))
            await asyncio.sleep(0.05)
            self.assertTrue(t.cancelled() or t.done())
            self.assertFalse(loop.stop_agent("childA"))

        asyncio.run(main())

    def test_session_stop_touches_agent(self):
        sv = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\server.py", encoding="utf-8").read()
        self.assertIn("_loop.stop_agent(sid)", sv)
        js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()
        self.assertIn("data-stopsub", js)
        self.assertIn("data-closesub", js)

class XlsxRawTsv(unittest.TestCase):
    """要看整張表的原始數值時，直接吐 TSV；太長就存成檔並回位置（不用 python／openpyxl）。"""

    def test_raw_mode_writes_tsv(self):
        import base64
        from woof import server as srv, store

        raw = open(r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\files\\__init__.py", "rb").read()
        self.assertIn(b"_xlsx_digest", raw)
        src = raw.decode("utf-8")
        self.assertIn("raw=True", src)
        self.assertIn("已存成檔案：{out_path}", src)
        # 摘要要告訴模型怎麼拿整張表
        self.assertIn("limit 設 200000", src)

class PythonHint(unittest.TestCase):
    """缺套件／缺指令時給「換哪一顆 python」的提示，不是重裝重試；也不改 PATH。"""

    def test_hint(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "woof_term_plugin", r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\terminal\\__init__.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("venv", m._python_hint("ModuleNotFoundError: No module named 'openpyxl'"))
        self.assertIn("venv", m._python_hint("bash: python: command not found"))
        self.assertEqual(m._python_hint("hello world"), "")
        # 沒有改 PATH 這種事：檔案裡不該有影響環境的寫法
        src = Path(r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\terminal\\__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ[\"PATH\"]", src)

class ExpandGate(unittest.TestCase):
    """「方案・拓展」：先補成完整的一套再做，做完照樣驗收。"""

    def test_expand_parses_steps(self):
        import asyncio
        from woof.agent import loop, tasks

        calls = {}

        async def fake(parent, goal, *a, **kw):
            calls["kind"] = kw.get("kind")
            calls["goal"] = goal
            return {"answer": '{"steps": ["算出 A 的數字", "同步 B 的公式", "跑回測驗證"], "notes": "B 的公式也要一起改"}',
                    "steps": 3, "session_id": "x"}

        real = loop.run_subagent
        loop.run_subagent = fake
        try:
            steps, notes = asyncio.run(tasks.expand_plan(None, "改一下 A 的算法", ["改 A 的算法"]))
        finally:
            loop.run_subagent = real
        self.assertEqual(steps, ["算出 A 的數字", "同步 B 的公式", "跑回測驗證"])
        self.assertEqual(notes, "B 的公式也要一起改")
        self.assertEqual(calls["kind"], "expand_request")
        self.assertIn("網路", calls["goal"])          # 該查的要先查
        self.assertIn("不要", calls["goal"])          # 不要瞎試

    def test_expand_bad_json_is_safe(self):
        import asyncio
        from woof.agent import loop, tasks

        async def fake(parent, goal, *a, **kw):
            return {"answer": "我覺得可以再想想"}

        real = loop.run_subagent
        loop.run_subagent = fake
        try:
            steps, notes = asyncio.run(tasks.expand_plan(None, "x", ["y"]))
        finally:
            loop.run_subagent = real
        self.assertEqual((steps, notes), ([], ""))

    def test_wiring(self):
        src = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\agent\\tasks.py", encoding="utf-8").read()
        self.assertIn('if gate == "expand":', src)
        self.assertIn("await expand_plan(ctx, objective, steps)", src)
        sv = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\server.py", encoding="utf-8").read()
        self.assertIn('("off", "human", "model", "expand")', sv)
        js = open(r"C:\\Users\\user\\hermes-files\\WOOF\\web\\dist\\app.js", encoding="utf-8").read()
        self.assertIn("方案・思維拓展", js)
        self.assertIn("v: 'expand'", js)
        pr = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\prompts.py", encoding="utf-8").read()
        self.assertIn('_e("expand"', pr)
        self.assertIn('_e("expand_body"', pr)

class SubagentAlwaysReports(unittest.TestCase):
    """子代理中斷／被停掉也要回報，否則主對話會一直等一個不會來的結論。"""

    def test_cancelled_still_injects(self):
        import asyncio
        from woof.agent import loop

        got = {}

        class FakeCtx:
            session_id = "childX"

        async def fake_prepare(*a, **kw):
            return {"id": "childX", "title": "子代理"}, {"id": "m", "label": "M"}, FakeCtx()

        async def fake_drive(ctx):
            raise RuntimeError("bomb")

        async def fake_inject(sid, kind, text):
            got["sid"], got["kind"], got["text"] = sid, kind, text

        class FakeReg:
            inject = staticmethod(fake_inject)

        class Parent:
            session_id = "parentX"

            async def emit(self, ev):
                return None

        real = (loop._prepare_subagent, loop._drive, loop.REG, loop._subagent_report)
        loop._prepare_subagent = fake_prepare
        loop._drive = fake_drive
        loop.REG = FakeReg()
        loop._subagent_report = lambda *a, **kw: {"session_id": "childX", "answer": "（沒有結論）", "steps": 0}
        try:
            async def go():
                await loop.run_subagent_bg(Parent(), "做點事")
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if got:
                        break
            asyncio.run(go())
        finally:
            loop._prepare_subagent, loop._drive, loop.REG, loop._subagent_report = real
        self.assertTrue(got, "子代理炸掉卻沒有回報主對話")
        self.assertEqual(got["sid"], "parentX")
        self.assertIn("bomb", got["text"])

    def test_source_has_steer_and_report(self):
        lp = open(r"C:\\Users\\user\\hermes-files\\WOOF\\woof\\agent\\loop.py", encoding="utf-8").read()
        self.assertIn("子代理被停止，沒有產出結論", lp)
        self.assertIn("asyncio.shield(inject(parent_sid, \"bg_done\", report_text(r)))", lp)
        sp = open(r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\subagent\\__init__.py", encoding="utf-8").read()
        self.assertIn("if sid in AGENT_RUNS:", sp)
        self.assertIn('"steered": True', sp)
        self.assertIn("它最後在做的", sp)

class SubagentProgress(unittest.TestCase):
    """要問正在跑的子代理：帶 session_id 再呼叫 = 插話，不會重開一輪，還會回它最後在做什麼。"""

    def test_steer_when_running(self):
        import asyncio, importlib.util, sys

        from woof.agent import loop, tasks

        got = {}

        def fake_steer(sid, blocks, meta=None):
            got["sid"], got["text"] = sid, blocks[0]["text"]

        real_steer, real_last = loop.push_steer, tasks.last_text
        loop.push_steer = fake_steer
        tasks.last_text = lambda sid, role: "正在跑回測"
        try:
            spec = importlib.util.spec_from_file_location(
                "subplug_t", r"C:\\Users\\user\\hermes-files\\WOOF\\plugins\\subagent\\__init__.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)

            class W:
                settings = {"models": "", "max_parallel": 3, "max_per_turn": 3}
                config = {"models": []}

                def add_tool(self, name, *a, **kw):
                    for x in a:
                        if callable(x):
                            self.fn = x
                    return lambda f: f

            class Parent:
                session_id = root_session_id = "parentX"
                cwd = r"C:\\Users\\user\\hermes-files\\WOOF"
                turn_mid = 1

                async def output(self, s):
                    return None

            class Ctx:
                turn = Parent()

                async def output(self, s):
                    return None

            w = W()
            m.register(w)
            loop.AGENT_RUNS.add("childA")
            r = asyncio.run(w.fn({"goal": "進度怎樣？", "session_id": "childA"}, Ctx()))
        finally:
            loop.AGENT_RUNS.discard("childA")
            loop.push_steer, tasks.last_text = real_steer, real_last
        self.assertFalse(r.get("is_error"))
        self.assertIn("已經送進去", r["output"])
        self.assertIn("正在跑回測", r["output"])
        self.assertEqual((got["sid"], got["text"]), ("childA", "進度怎樣？"))

    def test_resume_mentions_dead_subagents(self):
        from woof import store
        from woof.agent import tasks

        con = store._conn()
        row = con.execute("SELECT parent FROM sessions WHERE parent IS NOT NULL AND parent != '' LIMIT 1").fetchone()
        if not row:
            self.skipTest("沒有子代理對話")
        txt = tasks.resume_digest(row[0])
        self.assertIn("子代理", txt)
        self.assertIn("不會再回報", txt)

if __name__ == "__main__":
    unittest.main(verbosity=2)
