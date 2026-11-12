"""Claude Code CLI 引擎。

每輪對話啟動一次本機 `claude -p`（stream-json 輸入輸出），用 --resume 接續同一個
Claude Code session。工具、壓縮、MCP 都由 Claude Code 自己處理；WOOF 只負責
介面、對話紀錄、停止與程序樹管理。登入由 claude 自己處理（claude auth login），WOOF 不碰憑證。

config.toml 的模型欄位：
  provider = "claude_cli"
  model = "opus"                      Claude Code 的模型別名或完整名稱
  efforts = ["low", "medium", "high", "xhigh", "max"]
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path

from woof import procs, store
from woof.agent.loop import build_system, load_image_b64
from woof.config import DATA

PROMPTS = DATA / "claude_prompts"
PROMPTS.mkdir(exist_ok=True)


def _find_claude(custom: str) -> str | None:
    if custom and Path(custom).exists():
        return custom
    hit = shutil.which("claude")
    if hit:
        return hit
    home = Path.home()
    for p in (home / ".local/bin/claude.exe", home / ".claude/local/claude.exe",
              home / "AppData/Roaming/npm/claude.cmd"):
        if p.exists():
            return str(p)
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "image":
                    parts.append("[圖片]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _to_cli_content(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            mt, data = load_image_b64(b)
            out.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
    return out


class ClaudeCLI:
    def __init__(self, woof) -> None:
        self.woof = woof
        self.running: dict[str, object] = {}

    def exe(self) -> str | None:
        return _find_claude(self.woof.settings.get("claude_path") or "")

    async def stop(self, session_id: str) -> None:
        proc = self.running.pop(session_id, None)
        if proc is not None and proc.poll() is None:  # type: ignore[attr-defined]
            procs.kill_tree(proc.pid)  # type: ignore[attr-defined]

    async def run(self, ctx, blocks: list[dict]) -> None:
        exe = self.exe()
        if not exe:
            raise RuntimeError("找不到 claude 執行檔。請先安裝 Claude Code 並執行 claude auth login 登入。")
        sid = ctx.session_id
        es = (store.get_session(sid) or {}).get("engine_state") or {}
        cwd = es.get("cwd") or ctx.cwd

        prompt_file = PROMPTS / f"{sid}.md"
        prompt_file.write_text(await build_system(ctx), encoding="utf-8")

        argv = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-mode", self.woof.settings.get("permission_mode") or "bypassPermissions",
                "--append-system-prompt-file", str(prompt_file),
                "--model", ctx.model_cfg.get("model") or ctx.model_cfg["id"]]
        if ctx.effort and ctx.effort != "off":
            argv += ["--effort", ctx.effort]
        if es.get("claude_session"):
            argv += ["--resume", es["claude_session"]]

        env = {"CLAUDE_CODE_ENTRYPOINT": "woof"}
        proc = procs.spawn(argv, cwd=cwd, env=env, stdin=True)
        self.running[sid] = proc
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def reader():
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                loop.call_soon_threadsafe(q.put_nowait, raw)
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=reader, daemon=True).start()

        msg = {"type": "user", "message": {"role": "user", "content": _to_cli_content(blocks)}}
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()

        noise: list[str] = []
        got_result = False
        try:
            while True:
                raw = await q.get()
                if raw is None:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    noise.append(line)
                    continue
                done = await self._handle(ev, ctx, es, cwd)
                if done:
                    got_result = True
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
        except asyncio.CancelledError:
            procs.kill_tree(proc.pid)
            raise
        finally:
            self.running.pop(sid, None)

        code = await asyncio.to_thread(proc.wait)
        if not got_result:
            tail = "\n".join(noise[-15:]) or f"exit {code}"
            raise RuntimeError(f"claude 異常結束：\n{tail}")

    async def _handle(self, ev: dict, ctx, es: dict, cwd: str) -> bool:
        sid = ctx.session_id
        t = ev.get("type")
        sub = ev.get("parent_tool_use_id") is not None

        if t == "system" and ev.get("subtype") == "init":
            if ev.get("session_id") and es.get("claude_session") != ev["session_id"]:
                es["claude_session"] = ev["session_id"]
                es["cwd"] = cwd
                store.update_session(sid, engine_state=es)
            return False

        if t == "system" and ev.get("subtype") == "api_retry":
            await ctx.emit({"type": "notice", "session_id": sid,
                            "text": f"API 重試第 {ev.get('attempt')} 次（{ev.get('error')}）"})
            return False

        if t == "stream_event" and not sub:
            e = ev.get("event") or {}
            if e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta":
                    await ctx.emit({"type": "delta", "session_id": sid, "kind": "text", "text": d.get("text", "")})
                elif d.get("type") == "thinking_delta":
                    await ctx.emit({"type": "delta", "session_id": sid, "kind": "thinking", "text": d.get("thinking", "")})
            return False

        if t == "assistant":
            m = ev.get("message") or {}
            content = []
            for b in m.get("content") or []:
                bt = b.get("type")
                if bt == "thinking" and b.get("thinking"):
                    content.append({"type": "thinking", "text": b["thinking"]})
                elif bt == "text" and b.get("text"):
                    content.append({"type": "text", "text": b["text"]})
                elif bt == "tool_use":
                    content.append({"type": "tool_call", "id": b["id"], "name": b["name"], "args": b.get("input") or {}})
            if not content:
                return False
            meta = {"model": ctx.model_cfg["id"], "api_model": m.get("model"), "engine": "claude_cli"}
            if sub:
                meta["parent"] = ev["parent_tool_use_id"]
            saved = store.add_message(sid, "assistant", content, meta)
            await ctx.emit({"type": "message", "session_id": sid, "message": saved})
            for b in content:
                if b["type"] == "tool_call":
                    await ctx.emit({"type": "tool_start", "session_id": sid, "id": b["id"],
                                    "name": b["name"], "args": b["args"]})
            u = m.get("usage") or {}
            if u and not sub:
                total = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
                        (u.get("cache_creation_input_tokens") or 0)
                usage = {"input_tokens": total, "output_tokens": u.get("output_tokens"),
                         "cache_read": u.get("cache_read_input_tokens"),
                         "cache_write": u.get("cache_creation_input_tokens"),
                         "context_window": __import__("woof.tokens", fromlist=["window_of"]).window_of(ctx.model_cfg) or None}
                es["usage"] = usage
                await ctx.emit({"type": "usage", "session_id": sid, **usage})
            return False

        if t == "user":
            m = ev.get("message") or {}
            results = []
            content = m.get("content")
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    out = _result_text(b.get("content"))
                    r = {"type": "tool_result", "id": b.get("tool_use_id"), "name": "",
                         "output": out, "is_error": bool(b.get("is_error"))}
                    results.append(r)
                    await ctx.emit({"type": "tool_end", "session_id": sid, "id": r["id"], "name": "",
                                    "output": out[:40000], "is_error": r["is_error"]})
            if results:
                saved = store.add_message(sid, "tool", results, {"parent": ev.get("parent_tool_use_id")} if sub else {})
                await ctx.emit({"type": "message", "session_id": sid, "message": saved})
            return False

        if t == "result":
            st = store.get_session(sid) or {}
            merged = {**(st.get("engine_state") or {}), **es}
            if ev.get("total_cost_usd") is not None:
                merged["cost_usd"] = ev["total_cost_usd"]
            store.update_session(sid, engine_state=merged)
            if ev.get("is_error"):
                await ctx.emit({"type": "error", "session_id": sid,
                                "text": str(ev.get("result") or ev.get("subtype") or "claude 回報錯誤")})
            return True
        return False


def register(woof):
    woof.add_engine("claude_cli", ClaudeCLI(woof))
