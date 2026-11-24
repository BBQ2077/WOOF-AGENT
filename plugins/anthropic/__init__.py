"""Claude（Anthropic Messages API，使用 Claude Console 的 API key）。

config.toml 的模型欄位：
  provider = "anthropic"
  model = "claude-opus-5-5"
  key_pool = "anthropic"
  efforts = ["low", "medium", "high", "xhigh", "max"]
"""
from __future__ import annotations

import time

from woof.lazy import lazy_module

# SDK 很大（約 24 MB、1.4 秒）：第一次真的呼叫 Claude 時才載入
anthropic = lazy_module("anthropic")

from woof.agent.loop import load_image_b64, sdk_retries
from woof import cachediag


def _to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role, blocks = m["role"], m["content"]
        if role == "user":
            conv = []
            for b in blocks:
                if b["type"] == "text":
                    conv.append({"type": "text", "text": b["text"]})
                elif b["type"] == "note":  # 重點便條：放最後、不打快取斷點
                    conv.append({"type": "text", "text": b["text"], "_note": True})
                elif b["type"] == "image":
                    mt, data = load_image_b64(b)
                    conv.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
            push("user", conv)
        elif role == "assistant":
            conv = []
            for b in blocks:
                if b["type"] == "thinking" and b.get("signature"):
                    conv.append({"type": "thinking", "thinking": b.get("text", ""), "signature": b["signature"]})
                elif b["type"] == "redacted_thinking":
                    conv.append({"type": "redacted_thinking", "data": b["data"]})
                elif b["type"] == "text" and b["text"]:
                    conv.append({"type": "text", "text": b["text"]})
                elif b["type"] == "tool_call":
                    conv.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("args") or {}})
            push("assistant", conv)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": b["id"], "content": _result_content(b),
                           "is_error": bool(b.get("is_error"))} for b in blocks if b["type"] == "tool_result"]
                 + [{"type": "text", "text": b["text"], "_note": True} for b in blocks if b["type"] == "note"])
    return out


def _result_content(b: dict):
    """工具結果：有附圖（vision_analyze）就用文字＋圖片區塊，Claude 的 tool_result 直接收圖。"""
    text = b["output"] or "(無輸出)"
    if not b.get("images"):
        return text
    out: list[dict] = [{"type": "text", "text": text}]
    for p in b["images"]:
        mt, data = load_image_b64(p)
        out.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
    return out


class AnthropicProvider:
    def __init__(self, woof) -> None:
        self.woof = woof

    async def stream(self, model: dict, system: str, messages: list[dict], tools: list[dict], effort: str | None):
        pool = self.woof.keys(model.get("key_pool") or "anthropic")
        tried = 0
        while True:
            entry = pool.get()
            if entry is None:
                raise RuntimeError("金鑰池「anthropic」沒有可用的 API key，請在 keys.toml 設定 Claude Console 的 key")
            try:
                async for ev in self._once(entry.key, model, system, messages, tools, effort):
                    yield ev
                return
            except anthropic.APIStatusError as e:
                tried += 1
                if e.status_code in (401, 403, 429) and tried < len(pool.entries):
                    status = "rate_limited" if e.status_code == 429 else "invalid"
                    pool.mark(entry, status, time.time() + 60 if status == "rate_limited" else None, f"HTTP {e.status_code}")
                    continue
                raise

    def _make_client(self, key, model):
        base_url = (model.get("base_url") or "").strip().rstrip("/")
        if base_url.endswith("/v1"):  # SDK 自己會加 /v1
            base_url = base_url[:-3]
        # 連線 10 秒、讀取 900 秒：長回合不要被讀取逾時誤殺
        return anthropic.AsyncAnthropic(api_key=key, base_url=base_url or None,
                                        timeout=anthropic.Timeout(900.0, connect=10.0), max_retries=sdk_retries(model))

    async def _once(self, key, model, system, messages, tools, effort):
        client = self._make_client(key, model)
        caching = self.woof.settings.get("prompt_caching", "開啟") == "開啟"
        sys_block = {"type": "text", "text": system}
        if caching:
            sys_block["cache_control"] = {"type": "ephemeral"}
        conv = _to_anthropic(messages)
        if caching and conv and conv[-1]["content"]:
            # 快取斷點打在最後一個「不是便條」的區塊：便條每步都換，打在它上面下一步就對不上
            blocks = conv[-1]["content"]
            k = max((i for i, b in enumerate(blocks) if not b.get("_note")), default=None)
            if k is not None:
                blocks[k] = {**blocks[k], "cache_control": {"type": "ephemeral"}}
            # 第二個斷點打在上一次請求的最後一則（中間隔一則助手回覆）：服務商往回找舊快取只找 20 格，
            # 這一步新增的區塊太多（例如一次附十幾張圖）就找不到上次寫的快取、整段重寫；在上次寫快取的位置再打一個就一定找得到
            if len(conv) >= 3 and conv[-3]["content"]:
                prev = conv[-3]["content"]
                k3 = max((i for i, b in enumerate(prev) if not b.get("_note")), default=None)
                if k3 is not None:
                    prev[k3] = {**prev[k3], "cache_control": {"type": "ephemeral"}}
        for m in conv:
            m["content"] = [{kk: vv for kk, vv in b.items() if kk != "_note"} if isinstance(b, dict) else b
                            for b in m["content"]]
        kw: dict = {
            "model": model.get("model") or model["id"],
            "max_tokens": int(model.get("max_tokens") or self.woof.settings.get("max_tokens") or 64000),
            "system": [sys_block],
            "messages": conv,
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        if tools:
            kw["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        if effort and effort != "off":
            kw["extra_body"] = {"output_config": {"effort": effort}}
        cachediag.record(kw["model"], kw["system"], kw.get("tools"), kw["messages"],
                         {"thinking": kw.get("thinking"), "extra": kw.get("extra_body"), "tool_choice": kw.get("tool_choice")})

        async with client.messages.stream(**kw) as stream:
            alive = 0.0
            async for ev in stream:
                if time.monotonic() - alive > 1:   # 有資料在流（含工具參數這種不會變成字的）：讓上層的卡住偵測知道還活著
                    alive = time.monotonic()
                    yield {"type": "alive"}
                if ev.type == "content_block_delta":
                    d = ev.delta
                    if d.type == "text_delta":
                        yield {"type": "text_delta", "text": d.text}
                    elif d.type == "thinking_delta":
                        yield {"type": "thinking_delta", "text": d.thinking}
            final = await stream.get_final_message()

        content: list[dict] = []
        for b in final.content:
            if b.type == "thinking":
                content.append({"type": "thinking", "text": b.thinking, "signature": b.signature})
            elif b.type == "redacted_thinking":
                content.append({"type": "redacted_thinking", "data": b.data})
            elif b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_call", "id": b.id, "name": b.name, "args": b.input})
        u = final.usage
        total_in = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        yield {"type": "usage", "input_tokens": total_in, "output_tokens": u.output_tokens,
               "cache_read": u.cache_read_input_tokens, "cache_write": u.cache_creation_input_tokens}
        yield {"type": "message", "content": content, "meta": {"stop_reason": final.stop_reason}}


def register(woof):
    woof.add_provider("anthropic", AnthropicProvider(woof))
