"""MCP 用戶端。

設定 servers 每行一個：
  名稱 | command 參數…
  名稱 | http://127.0.0.1:8000/mcp
  名稱 | https://example.com/mcp | Header: 值

對方的工具註冊成 mcp_<名稱>_<工具名>。連線在第一次用到時建立，
插件卸載或後端結束時關閉。stdio 子程序由 MCP SDK 放進 Job Object，
後端被殺時會一起結束。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex

import httpx2

_NAME = re.compile(r"[^0-9A-Za-z_]+")


def _parse(text: str) -> list[dict]:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        target = parts[1] if len(parts) > 1 else ""
        headers = {}
        for extra in parts[2:]:
            if ":" in extra:
                k, v = extra.split(":", 1)
                headers[k.strip()] = v.strip()
        if not name or not target:
            continue
        if target.startswith(("http://", "https://")):
            out.append({"name": name, "url": target, "headers": headers})
        else:
            argv = shlex.split(target, posix=False)
            out.append({"name": name, "command": argv[0], "args": argv[1:], "headers": {}})
    return out


def _safe(name: str) -> str:
    return _NAME.sub("_", name).strip("_") or "server"


class _Conn:
    def __init__(self, spec: dict, timeout: float):
        self.spec = spec
        self.timeout = timeout
        self.client = None
        self.tools: list = []
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self.client is not None or self.error:
                return
            try:
                from mcp import Client, StdioServerParameters
                from mcp.client.streamable_http import streamable_http_client
                self._http = None
                if "url" in self.spec:
                    headers = self.spec.get("headers") or None
                    if headers:
                        self._http = httpx2.AsyncClient(
                            headers=headers, timeout=httpx2.Timeout(30.0, read=300.0))
                        await self._http.__aenter__()
                        transport = streamable_http_client(self.spec["url"], http_client=self._http)
                        client = Client(transport)
                    else:
                        client = Client(self.spec["url"])
                else:
                    client = Client(StdioServerParameters(
                        command=self.spec["command"], args=self.spec["args"]))
                await client.__aenter__()
                listed = await asyncio.wait_for(client.list_tools(), timeout=self.timeout)
                self.client = client
                self.tools = list(listed.tools)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                raise

    async def call(self, tool: str, args: dict) -> tuple[str, bool]:
        if self.client is None:
            await self.connect()
        result = await asyncio.wait_for(
            self.client.call_tool(tool, args or {}), timeout=self.timeout)
        chunks = []
        for block in result.content or []:
            if getattr(block, "type", None) == "text":
                chunks.append(block.text)
            else:
                chunks.append(f"[{getattr(block, 'type', 'content')}]")
        if result.structured_content:
            chunks.append(json.dumps(result.structured_content, ensure_ascii=False))
        return "\n".join(chunks).strip() or "（沒有輸出）", bool(result.is_error)

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
        http, self._http = getattr(self, "_http", None), None
        if http is not None:
            try:
                await http.__aexit__(None, None, None)
            except Exception:
                pass


def register(woof):
    conns: dict[str, _Conn] = {}
    timeout = max(5.0, float(woof.settings.get("timeout") or 120))

    def build_prompt(ctx):
        lines = []
        for spec in _parse(woof.settings.get("servers") or ""):
            conn = conns.get(spec["name"])
            if conn and conn.error:
                lines.append(f"- {spec['name']}：連線失敗（{conn.error}）")
            elif conn and conn.tools:
                names = ", ".join(f"mcp_{_safe(spec['name'])}_{_safe(t.name)}" for t in conn.tools)
                lines.append(f"- {spec['name']}：{names}")
            else:
                lines.append(f"- {spec['name']}：尚未連線，第一次呼叫其工具時才會啟動")
        if not lines:
            return None
        return "## MCP 伺服器\n" + "\n".join(lines)

    async def prepare_turn(ctx):
        disabled = {x.strip() for x in str(woof.settings.get("disabled") or "").split(",") if x.strip()}
        for spec in _parse(woof.settings.get("servers") or ""):
            key = spec["name"]
            if key in disabled or key in conns:
                continue
            conn = _Conn(spec, timeout)
            conns[key] = conn
            try:
                await conn.connect()
            except Exception as e:
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"MCP「{key}」連不上：{type(e).__name__}: {e}"})
                continue
            prefix = f"mcp_{_safe(key)}_"
            for tool in conn.tools:
                exposed = prefix + _safe(tool.name)

                async def handler(args, ctx, _conn=conn, _tool=tool.name):
                    try:
                        output, is_error = await _conn.call(_tool, args)
                    except Exception as e:
                        return {"output": f"{type(e).__name__}: {e}", "is_error": True}
                    return {"output": output, "is_error": is_error}

                woof.add_tool(
                    exposed,
                    f"[MCP {key}] {tool.description or tool.name}",
                    tool.input_schema or {"type": "object", "properties": {}},
                    handler,
                )
            await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                            "text": f"MCP「{key}」已連線，{len(conn.tools)} 個工具"})

    def on_unload(name):
        if name != woof.name:
            return
        pending = [c for c in conns.values() if c.client is not None or getattr(c, "_http", None)]
        conns.clear()

        async def _close_all():
            await asyncio.gather(*(c.close() for c in pending), return_exceptions=True)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_close_all())
        except RuntimeError:
            pass

    def mcp_status(_ctx=None):
        disabled = {x.strip() for x in str(woof.settings.get("disabled") or "").split(",") if x.strip()}
        rows = []
        for spec in _parse(woof.settings.get("servers") or ""):
            conn = conns.get(spec["name"])
            rows.append({
                "name": spec["name"],
                "enabled": spec["name"] not in disabled,
                "connected": bool(conn and conn.client),
                "error": conn.error if conn else None,
                "tools": len(conn.tools) if conn else 0,
            })
        return rows

    woof.on("mcp_status", mcp_status, order=10)
    woof.on("build_prompt", build_prompt, order=60)
    woof.on("prepare_turn", prepare_turn, order=40)
    woof.on("plugin_unload", on_unload, order=10)
