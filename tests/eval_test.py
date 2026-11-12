"""評測系統：跑一批小任務，檢查工具、權限、子代理、壓縮、背景指令等行為。

執行：
  .venv/Scripts/python tests/eval_test.py                          # 假模型跑全部（不花錢，快速回歸）
  .venv/Scripts/python tests/eval_test.py --only read_report,sub_read
  .venv/Scripts/python tests/eval_test.py --model fake-main        # 指定模型（預設就是它）
  .venv/Scripts/python tests/eval_test.py --model nvidia-nemotron-lightning --limit 3
                                                                   # 真模型：讀本體 config.toml 的模型設定，
                                                                   # 金鑰從 keys.toml 在記憶體解開（不落地、不印出）

真模型模式預設跳過「假模型才做得出來」的題目（參數截斷、重複失敗、未知工具），
要全跑加 --all。每題有 --timeout 秒上限（預設 180）。

評測只在暫存資料夾（WOOF_DATA）裡讀寫：對話庫、快照、spill 全部進暫存區，
不碰真實對話與設定。結果存到 data/eval/（.gitignore 已忽略），JSON 與 Markdown 各一份，
方便改提示詞或插件前後比較。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="woof_eval_")
os.environ["WOOF_DATA"] = TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woof import procs, store, tokens as tokens_mod  # noqa: E402
from woof.agent import loop  # noqa: E402
from woof.loader import REG, merge_models  # noqa: E402

MODEL_ID = "fake-main"
AUX_MODEL_ID = "fake-aux"
FAKE_MODEL_ID = "fake-main"
REQ: list[dict] = []   # 真模型模式：模型實際收到的內容（Recorder 寫進來，每題清空）
SUMMARY_TEXT = ("摘要：使用者要求確認 KEYWORD-42。已完成讀檔與加總，"
                "待辦是回報結果。全域設定：window=1500。")

# 要載入的插件：檔案、終端機、TODO、子代理、上下文壓縮
WANT_PLUGINS = ("files", "terminal", "tasks", "subagent", "context")


# ---------------------------------------------------------------- 假模型
class FakeModel:
    """照腳本回覆的假模型。腳本按步驟走，每步可以帶多個工具呼叫。"""

    name = "fake"

    def __init__(self) -> None:
        self.scripts: dict[str, list[dict]] = {}
        self.pos: dict[str, int] = {}
        self.requests: list[dict] = []
        self.scope = "main"
        self.usage = {"input_tokens": 160, "output_tokens": 60, "cache_read": 0, "cache_write": 0}

    def set_script(self, scope: str, steps: list[dict]) -> None:
        self.scripts[scope] = steps
        self.pos[scope] = 0

    def _scope(self, system: str) -> str:
        # 子代理的系統提示以「你是被主代理派出去的子代理」開頭；主代理的系統提示雖然也會提到
        # delegate_task 描述裡的子代理字樣，但不會有這句，所以用整句比對而不是單看關鍵字。
        return f"{self.scope}:sub" if "被主代理派出去的子代理" in (system or "") else self.scope

    async def stream(self, model, system, messages, tools, effort):
        scope = self._scope(system)
        self.requests.append({"scope": scope, "system": system, "messages": messages, "tools": tools})
        if not tools:  # 輔助呼叫（壓縮摘要）
            yield {"type": "text_delta", "text": SUMMARY_TEXT}
            yield {"type": "usage", **self.usage}
            yield {"type": "message", "content": [{"type": "text", "text": SUMMARY_TEXT}]}
            return
        steps = self.scripts.get(scope) or []
        i = self.pos.get(scope, 0)
        if i >= len(steps):
            text = f"（腳本用完：{scope} 第 {i + 1} 步沒有內容）"
            yield {"type": "text_delta", "text": text}
            yield {"type": "usage", **self.usage}
            yield {"type": "message", "content": [{"type": "text", "text": text}]}
            return
        step = steps[i]
        self.pos[scope] = i + 1
        content: list[dict] = []
        if step.get("text"):
            yield {"type": "text_delta", "text": step["text"]}
            content.append({"type": "text", "text": step["text"]})
        for n, call in enumerate(step.get("calls") or []):
            cid = f"{scope}-{i}-{n}"
            if call.get("raw") is not None:  # 參數被截斷：只給得到 _raw
                content.append({"type": "tool_call", "id": cid, "name": call["name"],
                                "args": {"_raw": call["raw"]}})
            else:
                content.append({"type": "tool_call", "id": cid, "name": call["name"],
                                "args": call.get("args") or {}})
        if not content:
            content = [{"type": "text", "text": "（腳本這一格是空的）"}]
        yield {"type": "usage", **self.usage}
        yield {"type": "message", "content": content}


# ---------------------------------------------------------------- 上下文
class Ctx:
    def __init__(self, task: "Task", sid: str, wd: Path, events: list[dict], fake: FakeModel,
                 requests: list[dict] | None = None) -> None:
        self.task, self.sid, self.wd, self.events, self.fake = task, sid, wd, events, fake
        self.requests = requests if requests is not None else []   # 模型實際收到的內容（假模型與真模型都記）

    @property
    def calls(self) -> list[dict]:
        return [e for e in self.events if e.get("type") == "tool_start"]

    @property
    def results(self) -> list[dict]:
        return [e for e in self.events if e.get("type") == "tool_end"]

    def path(self, rel: str) -> Path:
        return self.wd / rel

    def answer(self) -> str:
        for m in reversed(store.get_messages(self.sid)):
            if m["role"] != "assistant":
                continue
            t = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text").strip()
            if t:
                return t
        return ""

    def of(self, name: str) -> list[dict]:
        return [r for r in self.results if r.get("name") == name]

    def session(self) -> dict:
        return store.get_session(self.sid) or {}


# ---------------------------------------------------------------- 題目
@dataclass
class Task:
    id: str
    title: str
    prompt: str
    script: list[dict]
    check: callable
    perm: str = "auto"
    headless: bool = False
    files: dict = field(default_factory=dict)
    window: int = 0
    seed: int = 0
    sub_script: list[dict] | None = None
    sub_mode: str = "read"
    sub_scope: str = ""
    fake_only: bool = False   # 只有照腳本走的假模型做得出來（參數截斷、重複失敗、未知工具），真模型模式跳過


def _txt(ctx: Ctx, name: str, idx: int = -1) -> str:
    rs = ctx.of(name)
    assert rs, f"沒有 {name} 的結果"
    return rs[idx].get("output") or ""


# 1. 讀檔並回報內容
def c_read_report(ctx: Ctx) -> None:
    assert ctx.of("read_file"), "沒有呼叫 read_file"
    assert "alpha 12" in ctx.answer(), f"回答沒有讀到的內容：{ctx.answer()[:80]}"


# 2. 寫檔再讀回驗證
def c_write_verify(ctx: Ctx) -> None:
    p = ctx.path("out/b.txt")
    assert p.is_file(), "沒有建立 out/b.txt"
    assert p.read_text(encoding="utf-8") == "hello 世界", f"內容不對：{p.read_text(encoding='utf-8')!r}"


# 3. 改檔
def c_patch_fix(ctx: Ctx) -> None:
    t = ctx.path("notes/a.txt").read_text(encoding="utf-8")
    assert "beta" in t and "alpha" not in t, f"patch 沒生效：{t!r}"


# 4. 搜尋內容
def c_search_hit(ctx: Ctx) -> None:
    assert ctx.of("search_files"), "沒有呼叫 search_files"
    assert "b.txt" in ctx.answer(), f"回答沒有指出命中的檔案：{ctx.answer()[:80]}"


# 5. 目錄樹
def c_tree(ctx: Ctx) -> None:
    assert ctx.of("list_tree"), "沒有呼叫 list_tree"
    assert "sub" in ctx.answer().lower(), f"回答沒有提到子目錄：{ctx.answer()[:80]}"


# 6. 終端機
def c_terminal(ctx: Ctx) -> None:
    assert "eval-ok" in _txt(ctx, "terminal"), f"終端機輸出沒看到標記：{_txt(ctx, 'terminal')[:80]}"


# 7. 指令失敗後換做法
def c_terminal_retry(ctx: Ctx) -> None:
    rs = ctx.of("terminal")
    assert len(rs) >= 2, f"只跑了 {len(rs)} 次指令"
    assert rs[0].get("is_error"), "第一次（錯誤指令）沒有回錯誤"
    assert not rs[1].get("is_error"), f"第二次還是失敗：{rs[1].get('output')[:80]}"


# 8. 五步以上
def c_five_steps(ctx: Ctx) -> None:
    assert len(ctx.calls) >= 5, f"只有 {len(ctx.calls)} 步工具"
    p = ctx.path("out/total.txt")
    assert p.is_file(), "沒有寫出 total.txt"
    assert p.read_text(encoding="utf-8").strip() == "21", f"總和不是 21：{p.read_text(encoding='utf-8')!r}"


# 9. TODO 清單
def c_todo(ctx: Ctx) -> None:
    todos = [e for e in ctx.events if e.get("type") == "todos"]
    assert todos, "沒有送出 todos 事件"
    items = todos[-1].get("items") or []
    assert len(items) >= 2, f"TODO 太少：{items}"
    for name in ("out/t1.txt", "out/t2.txt", "out/t3.txt"):
        assert ctx.path(name).is_file(), f"沒有做 {name}"


# 10. 權限擋下（唯讀模式不能寫檔）
def c_perm_denied(ctx: Ctx) -> None:
    rs = ctx.of("write_file")
    assert rs, "沒有呼叫 write_file"
    assert rs[0].get("is_error"), "唯讀模式竟然讓寫檔過了"
    assert "權限" in (rs[0].get("output") or ""), f"錯誤訊息沒說是權限：{rs[0].get('output')[:80]}"
    assert not ctx.path("deny.txt").exists(), "檔案還是被寫進去了"


# 11. 危險指令在無人值守時擋下
def c_perm_danger(ctx: Ctx) -> None:
    rs = ctx.of("terminal")
    assert rs, "沒有呼叫 terminal"
    out = rs[0].get("output") or ""
    assert rs[0].get("is_error") and ("權限" in out or "危險" in out), f"沒有擋下：{out[:80]}"


# 12. 子代理唯讀
def c_sub_read(ctx: Ctx) -> None:
    kids = [s for s in store.list_sessions() if s.get("parent") == ctx.sid]
    assert kids, "沒有開出子代理對話"
    assert "needle-value" in ctx.answer(), f"主代理沒有拿到子代理的結果：{ctx.answer()[:80]}"


# 13. 子代理寫入
def c_sub_write(ctx: Ctx) -> None:
    p = ctx.path("out/sub.txt")
    assert p.is_file(), "子代理沒有寫出檔案"
    assert "written-by-sub" in p.read_text(encoding="utf-8"), f"內容不對：{p.read_text(encoding='utf-8')!r}"


# 14. 子代理寫入範圍
def c_sub_scope(ctx: Ctx) -> None:
    assert not ctx.path("outside.txt").exists(), "範圍外的檔案被寫了"
    assert ctx.path("out/in.txt").is_file(), "範圍內的檔案沒有寫成"


# 15. 背景指令
def c_bg(ctx: Ctx) -> None:
    rs = ctx.of("process")
    assert len(rs) >= 1, "沒有用 process 查看背景任務"
    assert "exit=0" in (rs[-1].get("output") or ""), f"背景任務沒正常結束：{rs[-1].get('output')[:120]}"


# 16. 工具輸出過長要轉存
def c_spill(ctx: Ctx) -> None:
    out = _txt(ctx, "terminal")
    assert "全文存於" in out, f"沒有轉存提示：{out[:100]}"
    assert "x" * 500 in out, "輸出被砍太多"


# 17. 工具參數被截斷
def c_raw_args(ctx: Ctx) -> None:
    rs = ctx.of("write_file")
    assert len(rs) >= 2, f"write_file 只被呼叫 {len(rs)} 次"
    assert rs[0].get("is_error") and "JSON" in (rs[0].get("output") or ""), f"第一次沒有報參數錯誤：{rs[0].get('output')[:80]}"
    assert not rs[1].get("is_error"), f"重試還是失敗：{rs[1].get('output')[:80]}"
    assert ctx.path("out/raw.txt").is_file(), "最後沒有寫成"


# 18. 同一個失敗重複三次
def c_repeat_fail(ctx: Ctx) -> None:
    rs = ctx.of("read_file")
    assert len(rs) >= 3, f"只讀了 {len(rs)} 次"
    out = rs[2].get("output") or ""
    assert "已經失敗" in out or "沒有再執行" in out, f"第三次沒有被擋：{out[:80]}"


# 19. 壓縮後接著做
def c_compact(ctx: Ctx) -> None:
    summ = (ctx.session().get("engine_state") or {}).get("summary") or {}
    assert summ.get("text"), "沒有產生摘要"
    heads = [r for r in ctx.requests
             if any("【上下文摘要】" in (b.get("text") or "") for m in r["messages"] for b in (m.get("content") or []))]
    assert heads, "模型收到的訊息裡沒有摘要開頭"
    assert any("KEYWORD-42" in (b.get("text") or "") for r in heads for m in r["messages"]
               for b in (m.get("content") or []) if b.get("type") == "text"), "摘要裡沒有關鍵字"


# 20. 找不到工具
def c_unknown_tool(ctx: Ctx) -> None:
    rs = ctx.results
    assert rs, "沒有任何工具結果"
    assert "找不到工具" in (rs[0].get("output") or ""), f"沒有報找不到工具：{rs[0].get('output')[:80]}"
    assert ctx.of("read_file"), "沒有改用 read_file"


# ---------------------------------------------------------------- 題庫
TASKS: list[Task] = [
    Task("read_report", "讀檔並回報內容",
         "讀 notes/a.txt，把內容原樣回報給我。",
         [{"calls": [{"name": "read_file", "args": {"path": "notes/a.txt"}}]},
          {"text": "內容是 alpha 12"}],
         c_read_report, files={"notes/a.txt": "alpha 12\n"}),
    Task("write_verify", "寫檔再讀回驗證",
         "把 hello 世界 寫進 out/b.txt，然後讀回來確認。",
         [{"calls": [{"name": "write_file", "args": {"path": "out/b.txt", "content": "hello 世界"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "out/b.txt"}}]},
          {"text": "已寫入並確認"}],
         c_write_verify),
    Task("patch_fix", "改檔",
         "notes/a.txt 裡的 alpha 要改成 beta，只改那一個字。",
         [{"calls": [{"name": "read_file", "args": {"path": "notes/a.txt"}}]},
          {"calls": [{"name": "patch", "args": {"path": "notes/a.txt", "old_string": "alpha", "new_string": "beta"}}]},
          {"text": "改好了"}],
         c_patch_fix, files={"notes/a.txt": "alpha 12\n"}),
    Task("search_hit", "搜尋檔案內容",
         "在 notes 裡找 needle，告訴我在哪個檔案。",
         [{"calls": [{"name": "search_files", "args": {"pattern": "needle", "path": "notes"}}]},
          {"text": "在 notes/b.txt 找到 needle"}],
         c_search_hit, files={"notes/a.txt": "alpha 12\n", "notes/b.txt": "這裡有 needle\n"}),
    Task("tree_overview", "看目錄結構",
         "看一下 notes 的目錄結構，回報有哪些子目錄。",
         [{"calls": [{"name": "list_tree", "args": {"path": "notes", "depth": 2}}]},
          {"text": "有 sub 這個子目錄"}],
         c_tree, files={"notes/sub/deep.txt": "x\n"}),
    Task("terminal_run", "跑指令",
         "跑 echo eval-ok，把輸出貼給我。",
         [{"calls": [{"name": "terminal", "args": {"command": "echo eval-ok"}}]},
          {"text": "輸出是 eval-ok"}],
         c_terminal),
    Task("terminal_retry", "指令失敗後換做法",
         "顯示 notes/none.txt 的內容；如果失敗就想辦法完成。",
         [{"calls": [{"name": "terminal", "args": {"command": "cat notes/none.txt"}}]},
          {"calls": [{"name": "terminal", "args": {"command": "cat notes/a.txt"}}]},
          {"text": "檔案不存在，改讀 notes/a.txt 成功"}],
         c_terminal_retry, files={"notes/a.txt": "alpha 12\n"}),
    Task("five_steps", "五步以上的任務",
         "把 notes/n1.txt、n2.txt、n3.txt 的數字加總，寫進 out/total.txt。",
         [{"calls": [{"name": "read_file", "args": {"path": "notes/n1.txt"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "notes/n2.txt"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "notes/n3.txt"}}]},
          {"calls": [{"name": "terminal", "args": {"command": "echo $((5 + 7 + 9))"}}]},
          {"calls": [{"name": "write_file", "args": {"path": "out/total.txt", "content": "21"}}]},
          {"text": "總和是 21"}],
         c_five_steps, files={"notes/n1.txt": "5\n", "notes/n2.txt": "7\n", "notes/n3.txt": "9\n"}),
    Task("todo_flow", "用 TODO 清單做事",
         "把「寫 t1」「寫 t2」「寫 t3」列成 TODO 清單，然後一件一件做完（各寫一個檔）。",
         [{"calls": [{"name": "todo", "args": {"items": [
             {"text": "寫 t1", "status": "doing"},
             {"text": "寫 t2", "status": "pending"},
             {"text": "寫 t3", "status": "pending"}]}}]},
          {"calls": [{"name": "write_file", "args": {"path": "out/t1.txt", "content": "1"}}]},
          {"calls": [{"name": "write_file", "args": {"path": "out/t2.txt", "content": "2"}}]},
          {"calls": [{"name": "write_file", "args": {"path": "out/t3.txt", "content": "3"}}]},
          {"text": "三件事都做完了"}],
         c_todo),
    Task("perm_denied", "權限擋下寫檔",
         "把 blocked 寫進 deny.txt。",
         [{"calls": [{"name": "write_file", "args": {"path": "deny.txt", "content": "blocked"}}]},
          {"text": "權限不足，無法寫檔"}],
         c_perm_denied, perm="readonly"),
    Task("perm_danger", "危險指令在無人值守時擋下",
         "刪掉工作目錄裡的 build 資料夾。",
         [{"calls": [{"name": "terminal", "args": {"command": "rm -rf build"}}]},
          {"text": "被權限擋下，需要使用者處理"}],
         c_perm_danger, headless=True, files={"build/keep.txt": "x\n"}),
    Task("sub_read", "子代理唯讀",
         "派一個子代理去 notes 找 needle，把它的結果回報給我。",
         [{"calls": [{"name": "delegate_task", "args": {"mode": "read", "goal": "在 notes 找 needle 並回報值"}}]},
          {"text": "子代理找到 needle-value"}],
         c_sub_read, files={"notes/needle.txt": "needle-value\n"},
         sub_script=[{"calls": [{"name": "read_file", "args": {"path": "notes/needle.txt"}}]},
                     {"text": "值是 needle-value"}]),
    Task("sub_write", "子代理寫入",
         "派子代理把 written-by-sub 寫進 out/sub.txt。",
         [{"calls": [{"name": "delegate_task", "args": {"mode": "write", "scope": "out",
                                                        "goal": "把 written-by-sub 寫進 out/sub.txt"}}]},
          {"text": "子代理已寫入"}],
         c_sub_write,
         sub_script=[{"calls": [{"name": "write_file", "args": {"path": "out/sub.txt", "content": "written-by-sub"}}]},
                     {"text": "已寫入 out/sub.txt"}]),
    Task("sub_scope", "子代理寫入範圍",
         "派子代理同時寫 out/in.txt 和 outside.txt。",
         [{"calls": [{"name": "delegate_task", "args": {"mode": "write", "scope": "out",
                                                        "goal": "寫 out/in.txt 內容 in，再寫 outside.txt 內容 out"}}]},
          {"text": "範圍內寫成，範圍外被擋"}],
         c_sub_scope,
         sub_script=[{"calls": [{"name": "write_file", "args": {"path": "out/in.txt", "content": "in"}}]},
                     {"calls": [{"name": "write_file", "args": {"path": "outside.txt", "content": "out"}}]},
                     {"text": "範圍外寫不進去"}]),
    Task("bg_task", "背景指令",
         "在背景跑 echo bg-marker 加 sleep 1，然後確認它結束了。",
         [{"calls": [{"name": "terminal", "args": {"command": "echo bg-marker; sleep 1", "background": True}}]},
          {"calls": [{"name": "terminal", "args": {"command": "sleep 2"}}]},
          {"calls": [{"name": "process", "args": {"action": "list"}}]},
          {"text": "背景任務已結束 exit=0"}],
         c_bg),
    Task("spill_output", "工具輸出過長要轉存",
         "跑 python -c \"print('x'*20000)\"，把結果給我。",
         [{"calls": [{"name": "terminal", "args": {"command": "python -c \"print('x'*20000)\""}}]},
          {"text": "輸出太長，已轉存"}],
         c_spill),
    Task("raw_args", "工具參數被截斷",
         "把 truncated 寫進 out/raw.txt。",
         [{"calls": [{"name": "write_file", "raw": "{\"path\": \"out/raw.txt\", \"content\": \"trunc"}]},
          {"calls": [{"name": "write_file", "args": {"path": "out/raw.txt", "content": "truncated"}}]},
          {"text": "拆小後寫成功"}],
         c_raw_args, fake_only=True),
    Task("repeat_fail", "同一個失敗重複三次",
         "讀 notes/missing.txt 三次。",
         [{"calls": [{"name": "read_file", "args": {"path": "notes/missing.txt"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "notes/missing.txt"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "notes/missing.txt"}}]},
          {"text": "檔案一直不存在"}],
         c_repeat_fail, fake_only=True),
    Task("compact_continue", "壓縮後接著做",
         "接著做：確認 KEYWORD-42 並回報。",
         [{"calls": [{"name": "read_file", "args": {"path": "notes/a.txt"}}]},
          {"text": "KEYWORD-42 已確認"}],
         c_compact, window=1500, seed=3, files={"notes/a.txt": "alpha 12\n"}),
    Task("unknown_tool", "找不到工具要換做法",
         "用 no_such_tool 讀 notes/a.txt，不行就換別的方法。",
         [{"calls": [{"name": "no_such_tool", "args": {"path": "notes/a.txt"}}]},
          {"calls": [{"name": "read_file", "args": {"path": "notes/a.txt"}}]},
          {"text": "改用 read_file 讀到 alpha 12"}],
         c_unknown_tool, files={"notes/a.txt": "alpha 12\n"}, fake_only=True),
]


# ---------------------------------------------------------------- 執行
def setup_registry(fake: FakeModel) -> None:
    cfg = {
        "workdir": str(ROOT / "data" / "eval-work"),
        "allowed_dirs": [str(ROOT / "data" / "eval-work")],
        "models": [{"id": MODEL_ID, "provider": "fake", "label": "假模型（評測用）", "model": "fake",
                    "context_window": 8000, "efforts": ["low", "high"], "default_effort": "low",
                    "vendor": "eval"},
                   {"id": AUX_MODEL_ID, "provider": "fake", "label": "假模型（輔助）", "model": "fake",
                    "context_window": 8000, "efforts": [], "default_effort": None, "vendor": "eval"}],
        "default_model": MODEL_ID,
        "aux": {"compact": AUX_MODEL_ID, "subagent": MODEL_ID, "vision": AUX_MODEL_ID},
        "permissions": {"danger_ask": True, "headless": "deny"},
    }
    REG.config = cfg
    REG.providers = {"fake": fake}
    REG.tools = {}
    REG.events.hooks = {}
    REG.plugins = {}
    for info in REG.discover():
        if info.name in WANT_PLUGINS:
            REG.plugins[info.name] = info
            REG._load(info)


class Recorder:
    """包一層 provider，把模型實際收到的 system／messages 記下來（真模型模式檢查壓縮用）。"""

    def __init__(self, inner, log: list[dict]) -> None:
        self._inner, self._log = inner, log

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def stream(self, model, system, messages, tools, effort):
        self._log.append({"system": system, "messages": messages, "tools": tools})
        async for ev in self._inner.stream(model, system, messages, tools, effort):
            yield ev


def default_config_root() -> Path:
    """本體目錄（config.toml、keys.toml 在那裡）。副本自己沒有設定檔時往這裡找。"""
    env = os.environ.get("WOOF_CONFIG_ROOT")
    if env:
        return Path(env)
    if (ROOT / "config.toml").exists():
        return ROOT
    return Path("C:/Users/user/hermes-files/WOOF")


def load_keys_into_memory(root: Path) -> tuple[list[str], list[str]]:
    """把 keys.toml 的金鑰解進金鑰池（只在這個程序的記憶體裡）。

    不寫暫存檔、不印出、也不回傳明文；解不開的只回報「池／標籤＋原因」，方便排查。
    """
    from woof import dpapi, keypool

    path = root / "keys.toml"
    if not path.exists():
        return [], [f"找不到 {path}"]
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [], [f"keys.toml 讀不進來：{type(e).__name__}: {e}"]
    pools, fails = [], []
    for name, items in raw.items():
        if not isinstance(items, list):
            continue
        pool = keypool.KeyPool(name)
        for i, it in enumerate(items):
            if not (isinstance(it, dict) and it.get("key")):
                continue
            k = str(it["key"])
            if k.startswith(dpapi.PREFIX):
                try:
                    k = dpapi.decrypt(k)
                except Exception as e:
                    fails.append(f"{name}#{i + 1} 解不開（{type(e).__name__}）")
                    continue
            pool.entries.append(keypool.KeyEntry(key=k, label=str(it.get("label") or f"#{i + 1}")))
        if pool.entries:
            keypool.POOLS[name] = pool
            pools.append(name)
    return pools, fails


def setup_real(model_id: str, root: Path) -> list[str]:
    """真模型模式：讀本體的 config.toml，只留指定模型；工作目錄換成暫存資料夾；金鑰進記憶體。"""
    cfg_path = root / "config.toml"
    if not cfg_path.exists():
        raise SystemExit(f"找不到 {cfg_path}（用 --config-root 指定本體目錄）")
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    models = [m for m in merge_models(cfg) if m["id"] == model_id]
    if not models:
        names = ", ".join(m["id"] for m in merge_models(cfg))
        raise SystemExit(f"config.toml 沒有模型 {model_id}（可用：{names}）")
    models[0]["label"] = f"{models[0].get('label', model_id)}（評測）"

    workdir = Path(TMP) / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    conf = {k: v for k, v in cfg.items() if k not in ("models", "vendors", "budget", "workdir", "allowed_dirs")}
    conf.update({
        "workdir": str(workdir), "allowed_dirs": [str(workdir)],
        "models": models, "default_model": model_id,
        "aux": {"compact": model_id, "subagent": model_id, "vision": model_id},
        "permissions": {"danger_ask": True, "headless": "deny"},
    })
    REG.config = conf
    REG.providers, REG.tools = {}, {}
    REG.events.hooks = {}
    REG.plugins = {}
    for info in REG.discover():
        if info.name in (*WANT_PLUGINS, "openai_compat"):
            REG.plugins[info.name] = info
            REG._load(info)
    if model_id not in [m["id"] for m in REG.models()]:
        raise SystemExit(f"模型 {model_id} 的 provider 沒載入成功（{REG.plugins.get('openai_compat').error if REG.plugins.get('openai_compat') else '插件不存在'}）")

    # 包一層記錄器：之後檢查「模型收到的訊息裡有沒有壓縮摘要」
    for name, prov in list(REG.providers.items()):
        REG.providers[name] = Recorder(prov, REQ)

    pools, fails = load_keys_into_memory(root)
    print(f"設定來源：{cfg_path}")
    print(f"金鑰池（記憶體）：{', '.join(pools) if pools else '（沒有）'}"
          + (f"；解不開：{'、'.join(fails)}" if fails else ""))
    return fails


def seed_history(sid: str, rounds: int) -> int:
    """先塞幾輪又長又無關痛癢的對話，讓上下文用量衝過門檻。"""
    for i in range(rounds):
        store.add_message(sid, "user", [{"type": "text",
                                         "text": f"第 {i + 1} 輪：請記下 KEYWORD-42，並說明你的計畫。"}])
        store.add_message(sid, "assistant", [{"type": "text", "text": "好的，記下了。" + "補充說明。" * 120}])
    return store.get_messages(sid)[-1]["id"]


async def run_one(task: Task, fake: FakeModel | None, base: Path, timeout: int = 180) -> dict:
    wd = base / task.id
    wd.mkdir(parents=True, exist_ok=True)
    for rel, body in (task.files or {}).items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    info = REG.plugins.get("context")
    if info is not None:
        info.settings["window"] = task.window or 0
        info.settings["trigger"] = 50
        info.settings["keep"] = 1
        info.settings["mid_turn"] = True

    sid = store.create_session(MODEL_ID, None, f"[{task.id}] {task.title}")["id"]
    es: dict = {"workdir": str(wd)}
    if task.headless:
        es["headless"] = True
    store.update_session(sid, engine_state=es, perm=task.perm)
    if task.seed:
        last = seed_history(sid, task.seed)
        es = dict((store.get_session(sid) or {}).get("engine_state") or {})
        es["usage"] = {"input_tokens": 1200, "output_tokens": 150, "context_used": 1350,
                       "through": last, "context_window": 8000}
        store.update_session(sid, engine_state=es)

    if fake is not None:
        fake.set_script(task.id, task.script)
        if task.sub_script:
            fake.set_script(f"{task.id}:sub", task.sub_script)
        fake.scope = task.id
        fake.requests = []
        requests = fake.requests
    else:
        REQ.clear()
        requests = REQ

    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    t0 = time.time()
    try:
        await asyncio.wait_for(loop.run_turn(sid, [{"type": "text", "text": task.prompt}], emit), timeout)
    except asyncio.TimeoutError:
        events.append({"type": "eval_error", "text": f"超過 {timeout} 秒沒跑完"})
    except Exception as e:  # 評測不因單題失敗而中斷
        events.append({"type": "eval_error", "text": f"{type(e).__name__}: {e}"[:300]})
    await asyncio.sleep(0.2)  # 讓背景壓縮、背景通知跑完
    ms = int((time.time() - t0) * 1000)

    ctx = Ctx(task, sid, wd, events, fake, requests)
    note, ok = "", True
    try:
        task.check(ctx)
        note = "OK"
    except AssertionError as e:
        ok, note = False, str(e)
    except Exception as e:
        ok, note = False, f"檢查本身出錯：{type(e).__name__}: {e}"

    for t in list(procs.TASKS.values()):
        if t.ended is None:
            procs.stop_task(t.id)

    st = store.session_stats(sid)
    return {"id": task.id, "title": task.title, "pass": ok, "note": note,
            "steps": len(ctx.calls), "tool_errors": len([r for r in ctx.results if r.get("is_error")]),
            "turns": st["turns"], "model_calls": st["model_calls"],
            "tokens": st["usage"]["input_tokens"] + st["usage"]["output_tokens"], "ms": ms}


def write_report(rows: list[dict], out_dir: Path, mode: str = "fake") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    passed = sum(1 for r in rows if r["pass"])
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip() or "?"
    data = {"time": datetime.now().isoformat(timespec="seconds"), "model": MODEL_ID, "mode": mode, "commit": head,
            "total": len(rows), "passed": passed,
            "failed": [{"id": r["id"], "why": r["note"]} for r in rows if not r["pass"]], "tasks": rows}
    jp = out_dir / f"eval-{stamp}.json"
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# WOOF 評測 {stamp}", "",
             f"- 模型：{MODEL_ID}（{'真模型' if mode == 'real' else '假模型'}）　commit：{head}",
             f"- 通過：{passed}／{len(rows)}（{passed * 100 // max(1, len(rows))}%）", "",
             "| # | 題目 | 結果 | 工具步數 | 工具錯誤 | 模型呼叫 | token | 時間 | 說明 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['title']} | {'PASS' if r['pass'] else 'FAIL'} | {r['steps']} | "
                     f"{r['tool_errors']} | {r['model_calls']} | {r['tokens']} | {r['ms']} ms | "
                     f"{r['note'].replace('|', '/')} |")
    mp = out_dir / f"eval-{stamp}.md"
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jp, mp


async def main(only: set[str] | None, model: str, *, limit: int = 0, timeout: int = 180,
               run_fake_only: bool = False, config_root: Path | None = None) -> int:
    global MODEL_ID
    MODEL_ID = model
    real = model != FAKE_MODEL_ID
    if real:
        setup_real(model, config_root or default_config_root())
        fake = None
    else:
        fake = FakeModel()
        setup_registry(fake)
    if not REG.model_cfg(MODEL_ID):
        print(f"找不到模型 {MODEL_ID}（可用：{', '.join(m['id'] for m in REG.config['models'])}）")
        return 2

    base = Path(tempfile.mkdtemp(prefix="eval_work_", dir=TMP))
    tasks = [t for t in TASKS if not only or t.id in only]
    if real and not run_fake_only:
        tasks = [t for t in tasks if not t.fake_only]
    if limit > 0:
        tasks = tasks[:limit]
    if not tasks:
        print("沒有符合的題目")
        return 2

    print(f"評測 {len(tasks)} 題（{'真模型' if real else '假模型'}），資料夾 {TMP}")
    rows = []
    for t in tasks:
        r = await run_one(t, fake, base, timeout)
        rows.append(r)
        print(f"  {'PASS' if r['pass'] else 'FAIL'}  {t.id:<16} {r['steps']:>2} 步  {r['ms']:>5} ms  {r['note'][:70]}")

    jp, mp = write_report(rows, ROOT / "data" / "eval", "real" if real else "fake")
    passed = sum(1 for r in rows if r["pass"])
    print(f"\n{passed}/{len(rows)} 通過")
    print(f"JSON：{jp}")
    print(f"Markdown：{mp}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    only = None
    model = MODEL_ID
    limit = 0
    timeout = 180
    run_fake_only = "--all" in args
    config_root = None
    if "--only" in args:
        only = {x.strip() for x in args[args.index("--only") + 1].split(",") if x.strip()}
    if "--model" in args:
        model = args[args.index("--model") + 1]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--timeout" in args:
        timeout = int(args[args.index("--timeout") + 1])
    if "--config-root" in args:
        config_root = Path(args[args.index("--config-root") + 1])
    raise SystemExit(asyncio.run(main(only, model, limit=limit, timeout=timeout,
                                      run_fake_only=run_fake_only, config_root=config_root)))
