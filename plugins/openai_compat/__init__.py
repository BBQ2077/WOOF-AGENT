"""OpenAI 相容端點（Chat Completions／Responses API）。

config.toml 的模型欄位：
  provider = "openai_compat"
  model = "deepseek-flash"            實際送給 API 的模型名稱
  base_url = "https://api.deepseek.com"
  key_pool = "deepseek"               keys.toml 裡的金鑰池名稱
  effort_style = "deepseek"           deepseek / openai / openrouter / nim / none；沒寫就依位址判斷（woof/caps.py）
  沒寫 efforts 時預設有思考、預設強度「高」；沒寫 vision 時預設會看圖。
  端點回「不支援這個參數／不能看圖」就記住並改掉重送（data/model_caps.json）。
  echo_reasoning = "all" / "off"      思考內容回傳給 API 的範圍；沒寫＝只回傳這一回合（工具呼叫鏈）裡的思考，
                                      使用者開始新的一回合時，舊回合的思考就不送了。DeepSeek 自動用 "all"（它要求帶工具時每回合都回傳）
  api_style = "chat" / "responses"    走哪種 API：沒寫＝chat（Chat Completions）；"responses" 走 /responses；
                                      模型商層級可一起設（woof/loader.py 的 VENDOR_FIELDS），模型自己寫的優先。
"""
from __future__ import annotations

import asyncio
import json
import time

from woof.lazy import lazy_module

# SDK 很大（約 25 MB、1 秒）：第一次真的呼叫模型時才載入，開程式比較快
openai = lazy_module("openai")

from woof import caps
from woof.agent.loop import load_image_b64, new_call_id

_RETRY_STATUS = {401, 402, 403, 429}


def _echo_mode(model: dict) -> str:
    """思考回傳範圍：all＝每回合都回傳、turn＝只回傳使用者最後一則訊息之後的（工具呼叫鏈）、off＝不回傳。"""
    v = model.get("echo_reasoning")
    if v is True or v == "all":
        return "all"
    if v is False or v == "off" or caps.learned(model["id"]).get("no_echo"):
        return "off"
    if model.get("effort_style") == "deepseek" or "deepseek.com" in str(model.get("base_url") or ""):
        return "all"
    return "turn"


def _to_chat(messages: list[dict], vision: bool, echo: str) -> list[dict]:
    out: list[dict] = []
    # 這一回合從使用者最後一則真正的訊息開始（WOOF 自動發的接續、補推不算新回合）
    turn_start = 0
    for k, m in enumerate(messages):
        if m["role"] == "user" and not (m.get("meta") or {}).get("internal"):
            turn_start = k
    for k, m in enumerate(messages):
        role, blocks = m["role"], m["content"]
        if role == "user":
            parts = []
            for b in blocks:
                if b["type"] in ("text", "note"):  # note＝重點便條，只在這次請求的最後面
                    parts.append({"type": "text", "text": b["text"]})
                elif b["type"] == "image":
                    if vision:
                        mt, data = load_image_b64(b)
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}})
                    else:
                        parts.append({"type": "text", "text": f"[使用者附上一張圖片：{b['path']}，此模型無法直接看圖]"})
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
        elif role == "assistant":
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
            thinking = "".join(b.get("text", "") for b in blocks if b["type"] == "thinking")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("args") or {}, ensure_ascii=False)}}
                     for b in blocks if b["type"] == "tool_call"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            if echo == "all" or (echo == "turn" and k > turn_start and thinking):
                msg["reasoning_content"] = thinking
            details = [d for b in blocks if b["type"] == "thinking" for d in (b.get("details") or [])]
            if details:
                msg["reasoning_details"] = details
            out.append(msg)
        elif role == "tool":
            for b in blocks:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["id"], "content": b["output"] or "(無輸出)"})
            notes = [b["text"] for b in blocks if b["type"] == "note"]
            if notes:  # 重點便條：工具結果後面另起一則，工具結果本身一個字都不動，快取前綴不受影響
                out.append({"role": "user", "content": "\n".join(notes)})
    return out


_REASONING_MODELS = ("gpt-5", "gpt-6", "o1", "o3", "o4", "codex")


def _api_style(model: dict) -> str:
    """用哪種 API：寫 "responses" 走 /v1/responses、寫 "chat" 走 Chat Completions。

    沒寫時只有官方 OpenAI 的推理模型（gpt-5／gpt-6／o 系列）預設走 Responses，官方建議這類模型
    用 Responses 做工具呼叫；其他相容服務商一律維持 Chat Completions，行為不變。
    """
    v = str(model.get("api_style") or "").lower()
    if v in ("chat", "responses"):
        return v
    mid = str(model.get("model") or model.get("id") or "").lower()
    if "://api.openai.com" in str(model.get("base_url") or "") and mid.startswith(_REASONING_MODELS):
        return "responses"
    return "chat"


def _to_responses(messages: list[dict], vision: bool, echo: str) -> list[dict]:
    """WOOF 中立格式 → Responses API 的 input 陣列。

    user／assistant 訊息、function_call、function_call_output 都是一筆 item；
    reasoning item 只在有 id 時原樣送回（沒有 id 的舊思考無法回傳）。
    echo 的 turn 範圍跟 Chat Completions 一樣：只保留使用者最後一則訊息之後的思考。
    """
    out: list[dict] = []
    turn_start = 0
    for k, m in enumerate(messages):
        if m["role"] == "user" and not (m.get("meta") or {}).get("internal"):
            turn_start = k
    for k, m in enumerate(messages):
        role, blocks = m["role"], m["content"]
        if role == "user":
            parts = []
            for b in blocks:
                if b["type"] in ("text", "note"):
                    parts.append({"type": "input_text", "text": b["text"]})
                elif b["type"] == "image":
                    if vision:
                        mt, data = load_image_b64(b)
                        parts.append({"type": "input_image", "image_url": f"data:{mt};base64,{data}"})
                    else:
                        parts.append({"type": "input_text", "text": f"[使用者附上一張圖片：{b['path']}，此模型無法直接看圖]"})
            if parts:
                out.append({"role": "user", "content": parts})
        elif role == "assistant":
            # reasoning 先送（官方要求帶工具時把 reasoning 跟 function_call 一起回傳）
            if echo == "all" or (echo == "turn" and k > turn_start):
                for b in blocks:
                    if b["type"] != "thinking":
                        continue
                    for d in b.get("details") or []:
                        if not isinstance(d, dict) or not d.get("id"):
                            continue
                        item: dict = {"type": "reasoning", "id": d["id"],
                                      "summary": d.get("summary") or [{"type": "summary_text", "text": b.get("text", "")}]}
                        if d.get("content"):
                            item["content"] = d["content"]
                        if d.get("encrypted_content"):
                            item["encrypted_content"] = d["encrypted_content"]
                        out.append(item)
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
            if text:
                out.append({"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]})
            for b in blocks:
                if b["type"] == "tool_call":
                    call = {"type": "function_call", "name": b["name"],
                            "arguments": json.dumps(b.get("args") or {}, ensure_ascii=False),
                            "call_id": b["id"]}
                    if b.get("item_id"):
                        call["id"] = b["item_id"]
                    out.append(call)
        elif role == "tool":
            for b in blocks:
                if b["type"] == "tool_result":
                    out.append({"type": "function_call_output", "call_id": b["id"],
                                "output": b["output"] or "(無輸出)"})
            notes = [b["text"] for b in blocks if b["type"] == "note"]
            if notes:
                out.append({"role": "user", "content": [{"type": "input_text", "text": "\n".join(notes)}]})
    return out


def _get(obj, name, default=None):
    """SDK 物件或 dict 都拿得到欄位（串流事件、response、usage 用）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    dump = getattr(obj, "model_dump", None)
    return dump() if callable(dump) else {}


def _merge_detail(details: list[dict], item: dict) -> None:
    """OpenRouter 串流的 reasoning_details 會分段送出；同 index、同 type 的片段接起來。

    Gemini 等模型的工具呼叫需要把 reasoning_details（含簽章）原樣回傳。
    """
    if not isinstance(item, dict):
        return
    for d in details:
        if d.get("index") == item.get("index") and d.get("type") == item.get("type"):
            for k, v in item.items():
                if k in ("text", "summary", "data", "signature") and isinstance(v, str):
                    d[k] = (d.get(k) or "") + v
                elif k not in d or d[k] is None:
                    d[k] = v
            return
    details.append(dict(item))


_DESC_CACHE: dict[str, str] = {}   # 圖片路徑 → 看圖模型的描述（同一張圖每一步不重看）


def _has_image(messages: list[dict]) -> bool:
    return any(b.get("type") == "image" for m in messages if m["role"] == "user" for b in m["content"])


def _short_reason(e) -> str:
    b = getattr(e, "body", None)
    msg = ""
    if isinstance(b, dict):
        msg = str((b.get("error") or {}).get("message") if isinstance(b.get("error"), dict) else b.get("message") or b.get("detail") or "")
    msg = msg or str(getattr(e, "message", "") or e)
    return " ".join(msg.split())[:160]


async def _describe_images(woof, messages: list[dict]) -> list[dict]:
    """模型不能看圖時，用「輔助模型 → 看圖」把每張圖轉成文字；沒有可用的看圖模型就留路徑說明。"""
    from woof.agent.loop import complete, vision_chain
    from woof.prompts import get as _prompt
    chain = [m for m in vision_chain(None) if m.get("vision") is not False]
    vm = chain[0] if chain else None
    cache = _DESC_CACHE
    out = []
    for m in messages:
        if m["role"] != "user" or not any(b.get("type") == "image" for b in m["content"]):
            out.append(m)
            continue
        blocks = []
        for b in m["content"]:
            if b.get("type") != "image":
                blocks.append(b)
                continue
            key = b.get("path") or b.get("sha") or str(id(b))
            if key not in cache:
                desc = ""
                errs = []
                for vm in chain[:3]:
                    try:
                        desc = await complete(vm, _prompt("vision"), [{"role": "user", "content": [
                            b, {"type": "text", "text": "詳細描述這張圖，包含所有看得到的文字。"}]}])
                        break
                    except Exception as ex:  # 換下一個看圖模型
                        errs.append(f"{vm.get('label', vm['id'])}：{str(ex)[:160]}")
                if not desc and errs:
                    blocks.append({"type": "text", "text": f"[使用者附上一張圖片：{b.get('path', '')}，看圖模型都失敗：{'；'.join(errs)}]"})
                    continue
                cache[key] = (f"[使用者附上一張圖片：{b.get('path', '')}。以下是看圖模型的描述]\n{desc}" if desc
                              else f"[使用者附上一張圖片：{b.get('path', '')}，這個模型不能直接看圖，也沒有設定看圖輔助模型]")
            if len(cache) > 200:
                cache.pop(next(iter(cache)))
            blocks.append({"type": "text", "text": cache[key]})
        out.append({**m, "content": blocks})
    return out


def _effort_kwargs(style: str, effort: str | None) -> dict:
    if not effort or style == "none":
        return {}
    if style == "deepseek":
        if effort == "off":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": effort, "extra_body": {"thinking": {"type": "enabled"}}}
    if style == "openrouter":
        if effort == "off":
            return {"extra_body": {"reasoning": {"enabled": False}}}
        return {"extra_body": {"reasoning": {"effort": effort}}}
    if style == "openai":
        return {"reasoning_effort": "minimal" if effort == "off" else effort}
    if style == "nim":
        # NVIDIA NIM Nemotron（Lightning、Omni、Laguna）：chat_template_kwargs.enable_thinking（實測 2026-11-11）
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": effort != "off"}}}
    return {}


def _responses_reasoning(model: dict, effort: str | None) -> dict:
    """Responses API 的 reasoning 參數：思考強度 reasoning.effort 跟摘要 summary。

    強度換算跟 Chat Completions 一樣（"off" 送 minimal），學到的拒收值兩邊共用；
    summary 要 "auto" 才會回摘要，摘要就當畫面顯示的思考內容。
    """
    if not effort or model.get("effort_style", "none") == "none":
        return {}
    v = caps.effort_value(model["id"], "minimal" if effort == "off" else effort)
    if v is None:
        return {}
    return {"reasoning": {"effort": v, "summary": "auto"}}


def _wire_effort(model: dict, effort: str | None) -> str | None:
    """這次實際送出的 reasoning_effort 值（沒送就 None）。"""
    ek = _effort_kwargs(model.get("effort_style", "none"), effort)
    v = ek.get("reasoning_effort") or ((ek.get("extra_body") or {}).get("reasoning") or {}).get("effort")
    return caps.effort_value(model["id"], v) if v else None


class OpenAICompat:
    def __init__(self, woof) -> None:
        self.woof = woof

    async def stream(self, model: dict, system: str, messages: list[dict], tools: list[dict], effort: str | None):
        from woof import media
        kind = media.kind_of(model)
        if kind != "chat":  # 生圖／影片模型：這則訊息當提示詞，回生成的檔案
            async for ev in media.chat_turn(self.woof, model, messages, kind):
                yield ev
            return
        pool = self.woof.keys(model.get("key_pool") or model.get("provider"))
        tried = 0
        waited = 0
        wait_budget = int(model.get("rate_limit_wait") or 0)
        if not model.get("vision") and _has_image(messages):
            messages = await _describe_images(self.woof, messages)
        while True:
            entry = pool.get()
            base = str(model.get("base_url") or "")
            if entry is None and not pool.entries and any(h in base for h in ("://127.0.0.1", "://localhost", "://[::1]")):
                # 本機端點（例如本機 Ollama）不需要 key
                async for ev in self._once("none", model, system, messages, tools, effort):
                    yield ev
                return
            if entry is None:
                if not pool.entries:
                    raise RuntimeError(f"「{model.get('label', model.get('id'))}」還沒設定 API key（金鑰池「{pool.name}」是空的），"
                                       "請到「設定 → API 金鑰」貼上")
                raise RuntimeError(f"金鑰池「{pool.name}」的 key 都在冷卻或失效，稍後再試或加一把新的")
            started = False
            try:
                async for ev in self._once(entry.key, model, system, messages, tools, effort):
                    started = True
                    yield ev
                return
            except openai.APIError as e:
                # 先假設有思考、會看圖；端點明說不支援就記下來，這次改掉重送（下次直接不送）。
                # 有的端點在 HTTP 200 的串流裡才回錯（例如 NIM 的 multimodal），所以抓 APIError 全部。
                code = getattr(e, "status_code", None)
                if not started and code in (None, 400, 404, 415, 422):
                    body = f"{e.message} {json.dumps(e.body, ensure_ascii=False) if e.body else ''}"
                    name = model.get("label", model["id"])
                    from woof import media
                    k = media.kind_from_error(body)
                    if k:  # 服務商說這是圖片／影片模型：記住，這次直接改走生成
                        caps.learn(model["id"], kind=k)
                        yield {"type": "notice", "text": f"{name} 是{media.KIND_LABEL[k]}模型，已記住；這則訊息改當生成提示詞"}
                        async for ev in media.chat_turn(self.woof, model, messages, k):
                            yield ev
                        return
                    wire = _wire_effort(model, effort)
                    if wire and wire in caps.EFFORT_FALLBACK and caps.is_effort_reject(body):
                        # 只是這個強度值不收：換相近的值重送
                        caps.learn_bad_effort(model["id"], wire)
                        yield {"type": "notice", "text": f"{name} 不收思考強度「{wire}」，改用「{caps.effort_value(model['id'], wire) or '不送'}」並記住"}
                        tried = 0
                        continue
                    if effort and model.get("effort_style", "none") != "none" and caps.is_effort_reject(body):
                        caps.learn(model["id"], no_effort=True)
                        model.update(effort_style="none", efforts=[], default_effort=None)
                        yield {"type": "notice", "text": f"{name} 不接受思考強度參數，已改成不送並記住（設定 → 模型可重新偵測）"}
                        tried = 0
                        continue
                    if _echo_mode(model) != "off" and "reasoning_content" in body and not caps.learned(model["id"]).get("no_echo"):
                        # 端點不收 assistant 訊息裡的 reasoning_content：記住，之後不回傳思考
                        caps.learn(model["id"], no_echo=True)
                        yield {"type": "notice", "text": f"{name} 不收回傳的思考內容，已改成不回傳並記住"}
                        tried = 0
                        continue
                    if model.get("vision") and _has_image(messages) and caps.is_image_reject(body):
                        if model.get("vision_mode") == "on":
                            # 使用者手動指定能看圖：照他的，不記住；只有這一次先轉成文字
                            yield {"type": "notice", "text": f"{name} 這次拒收圖片（你手動設成能看圖，所以不記住）；這次先轉成文字再送"}
                        else:
                            caps.learn(model["id"], no_vision=True)
                            yield {"type": "notice", "text": f"{name} 不能直接看圖，已記住；圖片改給看圖模型轉成文字再送。判斷錯了可以到「設定 → 模型商」把看圖改成「能看」"}
                        model["vision"] = False
                        messages = await _describe_images(self.woof, messages)
                        tried = 0
                        continue
                if not started and code in (403, 404, 410, 503):
                    body = f"{e.message} {json.dumps(e.body, ensure_ascii=False) if e.body else ''}"
                    if caps.is_unavailable(body):
                        # 服務商暫停或下架這個模型（例如免費路由需求太高暫停）：記一小時，選單標「暫停」，看圖改找下一個
                        why = _short_reason(e)
                        caps.mark_down(model["id"], why)
                        raise RuntimeError(f"{model.get('label', model['id'])} 現在不能用（服務商回 {code}：{why}）。"
                                           "這是服務商那邊暫停或下架，不是 WOOF 或金鑰的問題；先換一個模型，"
                                           "或在「設定 → 模型用途 → 備用模型」設一個，下次會自動切過去") from e
                if not isinstance(e, openai.APIStatusError):
                    raise
                tried += 1
                if e.status_code in _RETRY_STATUS and tried < len(pool.entries):
                    status = {429: "rate_limited", 402: "exhausted"}.get(e.status_code, "invalid")
                    until = time.time() + (60 if status == "rate_limited" else 86400)
                    pool.mark(entry, status, until, f"HTTP {e.status_code}")
                    continue
                if e.status_code == 429 and not started and waited < wait_budget:
                    delay = min(5 * (1 + waited // 15), wait_budget - waited, 30)
                    waited += delay
                    yield {"type": "notice", "text": f"{model.get('label', model['id'])} 被限流，{delay} 秒後重試（累計 {waited}/{wait_budget} 秒）"}
                    await asyncio.sleep(delay)
                    tried = 0
                    continue
                raise

    async def _once(self, key: str, model: dict, system: str, messages: list[dict], tools: list[dict], effort):
        if _api_style(model) == "responses":
            async for ev in self._once_responses(key, model, system, messages, tools, effort):
                yield ev
            return
        async for ev in self._once_chat(key, model, system, messages, tools, effort):
            yield ev

    async def _once_chat(self, key: str, model: dict, system: str, messages: list[dict], tools: list[dict], effort):
        client = openai.AsyncOpenAI(api_key=key, base_url=model.get("base_url"), timeout=600,
                                    max_retries=int(model.get("max_retries", 2)))
        chat = [{"role": "system", "content": system}] + _to_chat(
            messages, bool(model.get("vision")), _echo_mode(model))
        kw: dict = {
            "model": model.get("model") or model["id"],
            "messages": chat,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": int(model.get("max_tokens") or self.woof.settings.get("max_tokens") or 32000),
        }
        if tools:
            kw["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools]
        ek = _effort_kwargs(model.get("effort_style", "none"), effort)
        if "reasoning_effort" in ek:
            v = caps.effort_value(model["id"], ek["reasoning_effort"])
            if v is None:
                ek.pop("reasoning_effort")
            else:
                ek["reasoning_effort"] = v
        r_ = (ek.get("extra_body") or {}).get("reasoning")
        if isinstance(r_, dict) and r_.get("effort"):
            v = caps.effort_value(model["id"], r_["effort"])
            if v is None:
                r_.pop("effort")
            else:
                r_["effort"] = v
        extra = {**(model.get("extra_body") or {}), **ek.pop("extra_body", {})}
        kw.update(ek)
        if extra:
            kw["extra_body"] = extra

        text, thinking = "", ""
        details: list[dict] = []
        calls: dict[int, dict] = {}
        usage = None
        finish = ""
        resp = await client.chat.completions.create(**kw)
        async for chunk in resp:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            if getattr(chunk.choices[0], "finish_reason", None):
                finish = chunk.choices[0].finish_reason
            d = chunk.choices[0].delta
            extra_fields = getattr(d, "model_extra", None) or {}
            for item in extra_fields.get("reasoning_details") or []:
                _merge_detail(details, item)
            r = getattr(d, "reasoning_content", None) or extra_fields.get("reasoning_content") or extra_fields.get("reasoning")
            if r:
                thinking += r
                yield {"type": "thinking_delta", "text": r}
            if d.content:
                text += d.content
                yield {"type": "text_delta", "text": d.content}
            for tc in d.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    c["id"] = tc.id
                if tc.function and tc.function.name:
                    c["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    c["args"] += tc.function.arguments

        content: list[dict] = []
        if thinking or details:
            block = {"type": "thinking", "text": thinking}
            if details:
                block["details"] = details
            content.append(block)
        if text:
            content.append({"type": "text", "text": text})
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": c["args"]}
            content.append({"type": "tool_call", "id": c["id"] or new_call_id(), "name": c["name"], "args": args})
        if usage:
            extra_u = getattr(usage, "model_extra", None) or {}
            # 快取欄位：DeepSeek 原生用 prompt_cache_hit_tokens；OpenRouter 等用 prompt_tokens_details.cached_tokens
            det = getattr(usage, "prompt_tokens_details", None) or extra_u.get("prompt_tokens_details") or {}
            if not isinstance(det, dict):
                det = det.model_dump() if hasattr(det, "model_dump") else {}
            cached = extra_u.get("prompt_cache_hit_tokens")
            if cached is None:
                cached = det.get("cached_tokens")
            yield {"type": "usage", "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                   "cache_read": cached, "cache_write": det.get("cache_write_tokens")}
        # 輸出長度用完被截斷：統一記成 max_tokens（和 Anthropic 的 stop_reason 一樣），接續插件據此請模型接著寫
        meta = {"stop_reason": "max_tokens"} if finish == "length" else {}
        # 有些服務商（Respan 免費路由等）有算思考 token 卻不回傳思考內容：記下數量，畫面標「有思考、沒回傳內容」
        if usage and not thinking and not details:
            cd = getattr(usage, "completion_tokens_details", None) or (getattr(usage, "model_extra", None) or {}).get("completion_tokens_details") or {}
            if not isinstance(cd, dict):
                cd = cd.model_dump() if hasattr(cd, "model_dump") else {}
            if cd.get("reasoning_tokens"):
                meta["hidden_reasoning"] = int(cd["reasoning_tokens"])
        yield {"type": "message", "content": content, "meta": meta}

    async def _once_responses(self, key: str, model: dict, system: str, messages: list[dict],
                              tools: list[dict], effort):
        """Responses API（POST /responses）：送出 item 陣列，串流事件一個一個回來。

        思考是 reasoning item（摘要靠 reasoning.summary，原文要 include 才有）；
        工具呼叫是 function_call item，回結果用 call_id 對應的 function_call_output。
        """
        client = openai.AsyncOpenAI(api_key=key, base_url=model.get("base_url"), timeout=600,
                                    max_retries=int(model.get("max_retries", 2)))
        echo = _echo_mode(model)
        kw: dict = {
            "model": model.get("model") or model["id"],
            "instructions": system,
            "input": _to_responses(messages, bool(model.get("vision")), echo),
            "stream": True,
            "store": False,
            "max_output_tokens": int(model.get("max_tokens") or self.woof.settings.get("max_tokens") or 32000),
        }
        if tools:
            kw["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                            "parameters": t["schema"]} for t in tools]
        if echo != "off":  # 不在伺服器端留存時，要靠加密的思考內容才送得回下一輪
            kw["include"] = ["reasoning.encrypted_content"]
        kw.update(_responses_reasoning(model, effort))
        if model.get("extra_body"):
            kw["extra_body"] = model["extra_body"]

        text = ""
        sums: dict[str, str] = {}     # reasoning item → 思考摘要（畫面顯示這個）
        rtexts: dict[str, str] = {}   # reasoning item → 思考原文（有 include 才會來）
        items: dict[str, dict] = {}   # item id → 工具呼叫／思考 item，照出現順序
        usage = None
        out_det: dict = {}
        stop = ""
        resp = await client.responses.create(**kw)
        async for ev in resp:
            kind = _get(ev, "type", "")
            if kind == "response.output_text.delta":
                d = _get(ev, "delta") or ""
                text += d
                yield {"type": "text_delta", "text": d}
            elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                d = _get(ev, "delta") or ""
                if d:
                    bucket = sums if kind == "response.reasoning_summary_text.delta" else rtexts
                    iid = _get(ev, "item_id") or ""
                    bucket[iid] = bucket.get(iid, "") + d
                    yield {"type": "thinking_delta", "text": d}
            elif kind == "response.output_item.added":
                item = _as_dict(_get(ev, "item"))
                iid = item.get("id") or f"item_{_get(ev, 'output_index', len(items))}"
                if item.get("type") == "function_call":
                    items[iid] = {"type": "function_call", "id": iid, "name": item.get("name") or "",
                                  "call_id": item.get("call_id") or "", "args": item.get("arguments") or ""}
                elif item.get("type") == "reasoning":
                    items[iid] = {"type": "reasoning", "id": iid,
                                  "encrypted_content": item.get("encrypted_content") or ""}
            elif kind == "response.function_call_arguments.delta":
                it = items.get(_get(ev, "item_id") or "")
                if it is not None:
                    it["args"] += _get(ev, "delta") or ""
            elif kind == "response.function_call_arguments.done":
                it = items.get(_get(ev, "item_id") or "")
                if it is not None and _get(ev, "arguments") is not None:
                    it["args"] = _get(ev, "arguments")
            elif kind == "response.output_item.done":
                item = _as_dict(_get(ev, "item"))
                iid = item.get("id") or ""
                if item.get("type") == "function_call":
                    it = items.setdefault(iid, {"type": "function_call", "id": iid, "name": "",
                                                "call_id": "", "args": ""})
                    it["name"] = item.get("name") or it["name"]
                    it["call_id"] = item.get("call_id") or it["call_id"]
                    if item.get("arguments") is not None:
                        it["args"] = item["arguments"]
                elif item.get("type") == "reasoning":
                    it = items.setdefault(iid, {"type": "reasoning", "id": iid})
                    if item.get("encrypted_content"):
                        it["encrypted_content"] = item["encrypted_content"]
            elif kind in ("response.completed", "response.incomplete"):
                r = _as_dict(_get(ev, "response"))
                usage = _get(r, "usage") or usage
                if _get(r, "status") == "incomplete":
                    reason = _get(_as_dict(_get(r, "incomplete_details")), "reason") or ""
                    if reason in ("max_output_tokens", "max_tokens"):
                        stop = "max_tokens"
            elif kind in ("response.failed", "error"):
                obj = _get(ev, "response") or ev
                msg = _get(_as_dict(_get(obj, "error")) or obj, "message") or "Responses API 回報錯誤"
                raise RuntimeError(f"Responses API 錯誤：{' '.join(str(msg).split())[:200]}")

        thinking = "".join(sums.values()) or "".join(rtexts.values())
        details: list[dict] = []
        for iid, it in items.items():
            if it["type"] != "reasoning":
                continue
            d: dict = {"type": "reasoning", "id": iid}
            if sums.get(iid):
                d["summary"] = [{"type": "summary_text", "text": sums[iid]}]
            if rtexts.get(iid):
                d["content"] = [{"type": "reasoning_text", "text": rtexts[iid]}]
            if it.get("encrypted_content"):
                d["encrypted_content"] = it["encrypted_content"]
            details.append(d)

        content: list[dict] = []
        if thinking or details:
            block: dict = {"type": "thinking", "text": thinking}
            if details:
                block["details"] = details
            content.append(block)
        if text:
            content.append({"type": "text", "text": text})
        for it in items.values():
            if it["type"] != "function_call":
                continue
            try:
                args = json.loads(it["args"]) if it["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": it["args"]}
            content.append({"type": "tool_call", "id": it["call_id"] or new_call_id(), "name": it["name"],
                            "args": args, "item_id": it["id"]})
        if usage:
            det = _as_dict(_get(usage, "input_tokens_details"))
            out_det = _as_dict(_get(usage, "output_tokens_details"))
            yield {"type": "usage", "input_tokens": _get(usage, "input_tokens"),
                   "output_tokens": _get(usage, "output_tokens"),
                   "cache_read": det.get("cached_tokens"), "cache_write": det.get("cache_write_tokens")}
        meta = {"stop_reason": stop} if stop else {}
        # 有算思考 token 卻只回摘要、沒內容時，畫面標「有思考、沒回傳內容」（跟 Chat Completions 一樣）
        if usage and not thinking and out_det.get("reasoning_tokens"):
            meta["hidden_reasoning"] = int(out_det["reasoning_tokens"])
        yield {"type": "message", "content": content, "meta": meta}


def register(woof):
    woof.add_provider("openai_compat", OpenAICompat(woof))
