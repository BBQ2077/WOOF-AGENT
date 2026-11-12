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
        system = p.build_system({"name": "小麥", "effort": "off"}, "glance")
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

    def test_clean_quick(self):
        p = self.up()
        self.assertEqual(p.clean({"quick": "嗯嗯|然後呢"})["quick"], ["嗯嗯", "然後呢"])
        self.assertEqual(p.clean({"quick": ["a", "b", "c", "d", "e"]})["quick"], ["a", "b", "c", "d"])
        self.assertNotIn("quick", p.clean({"quick": "  "}))
        self.assertIn("quick", p.cfg())

    def test_parse_sug(self):
        p = self.up()
        got = p.parse('{"say":"去睡吧","face":"gentle","mood":"心疼","sug":["好，晚安","你也早點睡"]}')
        self.assertEqual(got["sug"], ["好，晚安", "你也早點睡"])
        self.assertEqual(p.parse('{"say":"嗨"}')["sug"], [])
        self.assertEqual(p.parse('{"say":"嗨","sug":"單獨一句"}')["sug"], ["單獨一句"])

    def test_reply_not_forced_short(self):
        p = self.up()
        system = p.build_system({"name": "小麥", "effort": "off"}, "talk")
        self.assertNotIn("三十", system)
        self.assertIn("sug", system)


    def test_faces_have_images(self):
        p = self.up()
        for cid in p.CHARS:
            for f in p.FACES:
                self.assertIsNotNone(p.sprite(f, cid), f"{cid}/{f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
