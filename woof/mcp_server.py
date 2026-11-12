"""WOOF 當 MCP 伺服器：讓其他模型或工具用 MCP 操作 WOOF。

- 路徑：/mcp（streamable HTTP），只接受本機來源。
- 權杖：data/mcp_token（第一次啟動自動產生）。每個請求都要帶 Authorization: Bearer <權杖>。
  這個權杖和桌面程式用的隨機 TOKEN 不同，桌面程式每次啟動都會換。
- 金鑰不回傳：keys.toml 不讀不寫，模型商只回報「有幾把 key」。
- 工具分兩類：
  讀取：list_sessions、get_session、list_models、list_vendors、get_config、get_prompts_mode
  寫入：send_message（會跑完整回合，會用到模型）、set_prompts_mode、set_compress
"""
from __future__ import annotations

import secrets

from .config import DATA, load_config, load_keys, save_config

TOKEN_FILE = DATA / "mcp_token"


def token() -> str:
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def build(REG, run_message):
    """REG：loader 的註冊表；run_message(sid, text) 為 async，跑完一回合並回傳最後的文字。"""
    from mcp.server import MCPServer
    from woof import store
    from woof.prompts import MODES

    mcp = MCPServer(name="WOOF", version="1", instructions="WOOF 個人 AI agent 的控制介面。金鑰不會回傳。")

    @mcp.tool()
    def list_sessions(limit: int = 30) -> list[dict]:
        """列出最近的對話（不含子代理）。"""
        rows = [s for s in store.list_sessions() if not s.get("parent")]
        return [{"id": s["id"], "title": s.get("title"), "model": s.get("model")} for s in rows[: max(1, min(limit, 100))]]

    @mcp.tool()
    def get_session(session_id: str, last: int = 10) -> dict:
        """取得一個對話最近幾則訊息的文字內容。"""
        s = store.get_session(session_id)
        if not s:
            raise ValueError("找不到對話")
        msgs = store.get_messages(session_id)[-max(1, min(last, 50)):]
        out = []
        for m in msgs:
            text = "\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
            out.append({"role": m["role"], "text": text[:2000]})
        return {"id": s["id"], "title": s.get("title"), "messages": out}

    @mcp.tool()
    async def send_message(session_id: str, text: str) -> dict:
        """送一則訊息給對話並等回覆（會用到模型，會計費）。"""
        reply = await run_message(session_id, text)
        return {"reply": reply}

    @mcp.tool()
    def list_models() -> list[dict]:
        """列出已加入的模型（不含金鑰）。"""
        return [{"id": m.get("id"), "vendor": m.get("vendor"), "model": m.get("model"), "label": m.get("label")}
                for m in (load_config().get("models") or [])]

    @mcp.tool()
    def list_vendors() -> list[dict]:
        """列出模型商，只回報金鑰數量，不回傳金鑰。"""
        keys = load_keys()
        out = []
        for v in (load_config().get("vendors") or []):
            pool = v.get("key_pool")
            out.append({"id": v.get("id"), "label": v.get("label"), "provider": v.get("provider"),
                        "keys": len(keys.get(pool) or []) if pool else None})
        return out

    @mcp.tool()
    def get_prompts_mode() -> dict:
        """目前內部提示詞是精簡（lean）還是常規（normal）。"""
        return {"mode": ((load_config().get("prompts") or {}).get("mode") or "normal")}

    @mcp.tool()
    def set_prompts_mode(mode: str) -> dict:
        """切換內部提示詞模式：lean（精簡）或 normal（常規）。"""
        if mode not in MODES:
            raise ValueError(f"mode 只能是 {', '.join(MODES)}")
        cfg = load_config()
        cfg["prompts"] = {"mode": mode}
        save_config(cfg)
        return {"ok": True, "mode": mode}

    @mcp.tool()
    def set_compress(method: str = "", ratio: int = 0, parallel: int = 0, keep: int = 0) -> dict:
        """調整壓縮：method＝單次／多工；ratio 5 到 60；parallel 1 到 8；keep 2 以上。沒填的不改。"""
        from woof import store as st

        s = {}
        if method:
            if method not in ("單次", "多工"):
                raise ValueError("method 只能是 單次 或 多工")
            s["method"] = method
        if ratio:
            s["ratio"] = max(5, min(int(ratio), 60))
        if parallel:
            s["parallel"] = max(1, min(int(parallel), 8))
        if keep:
            s["keep"] = max(2, int(keep))
        st.set_plugin_state("context", settings=s)
        REG.load_all()
        return {"ok": True, "applied": s}

    return mcp


DEFAULT_PORT = 6990


def asgi_app(REG, run_message):
    """MCP 的 ASGI 應用：/mcp，每個請求都要 Authorization: Bearer <data/mcp_token>。"""
    import json

    mcp = build(REG, run_message)
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)
    want = f"Bearer {token()}"

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if hdrs.get("authorization") != want:
                body = json.dumps({"error": "需要 MCP 權杖（Authorization: Bearer ...）"}, ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await inner(scope, receive, send)

    return guarded


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        try:
            sk.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def serve(app, port: int, state: dict) -> None:
    """在目前的事件迴圈跑 MCP 伺服器。埠被佔用時只記錄錯誤，不影響主程式。"""
    import contextlib

    import uvicorn

    if not _port_free(port):
        state.update(running=False, error=f"埠 {port} 已被其他程式使用，請在 config.toml 的 [mcp] 改 port")
        return

    class _Srv(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # 不接管信號，讓主程式照常處理 Ctrl+C
            yield

    srv = _Srv(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    state.update(running=True, error="")
    try:
        await srv.serve()
    finally:
        state["running"] = False
