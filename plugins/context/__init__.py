"""上下文壓縮。

用量超過模型上下文窗口的設定比例時，把較早的訊息摘要成一段，
之後的回合只送「摘要 + 最近幾則」。原始訊息留在資料庫，不刪除。

摘要存在對話的 engine_state.summary：
  {"through": 最後被摘要的訊息 id, "text": 摘要, "at": 時間, "kept": [...], "mode": 模式, "parts": 段數}

設定（插件 context）：
  window    壓縮用的上下文大小（token）；0＝照模型的 context_window
  trigger   用量達到上下文大小的幾 % 就壓縮：回合結束後在背景做；回合進行中、送出前超過也會先壓縮
  keep      保留最近幾輪不壓縮（使用者一則＋模型回覆＝一輪）
  ratio     摘要長度佔原文的百分比（壓縮幅度）
  method    壓縮方式：單次（預設，一次摘要）／多工（依字數分段同時摘要，最後合併）
  parallel  多工時，一次同時跑幾段（1–8）
  mid_turn  回合進行中也檢查用量，超過就先壓縮再送；上下文爆掉時壓縮後重送
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

_bg: dict[str, asyncio.Task] = {}

MSG_CAP = 6000       # 單則訊息送給摘要的字數上限
SEG_MIN = 12000      # 每段至少這麼多字才值得分開
TARGET_MIN, TARGET_MAX = 400, 12000
MID_KEEP = 2         # 回合中途壓縮時，保留最近幾步（模型呼叫＋工具結果）原樣
WHO = {"user": "使用者", "assistant": "助手", "tool": "工具"}

_STRUCT = ("依下面的段落整理，沒有內容的段落就省略：\n"
           "## 目標與決定\n## 已完成\n## 進行中與未完成\n"
           "## 檔案、指令與結果（保留路徑、數字、關鍵輸出）\n## 限制與錯誤")
# 摘要用的提示詞在 woof/prompts.py（compact、compact_merge、compact_part…），設定頁可以檢視、覆寫
from woof.prompts import get as _prompt_get, get_in as _prompt_in  # noqa: E402


def _text_of(content: list) -> str:
    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "tool_call":
            args = b.get("args") or {}
            brief = args.get("command") or args.get("path") or args.get("query") or ""
            parts.append(f"[呼叫 {b.get('name')} {str(brief)[:200]}]")
        elif t == "tool_result":
            body = (b.get("output") or "").strip().replace("\n", " ")
            flag = "失敗" if b.get("is_error") else "完成"
            parts.append(f"[工具{flag}] {body[:400]}")
        elif t == "image":
            parts.append(f"[圖片 {b.get('path', '')}]")
    return "\n".join(p for p in parts if p).strip()


def _render(items: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"【{WHO.get(role, role)}】\n{body}" for role, body in items)


def _segments(items: list[tuple[str, str]], k: int) -> list[list[tuple[str, str]]]:
    """依字數均分：每段字數差不多，不切斷單則訊息。"""
    total = sum(len(b) for _, b in items)
    k = max(1, min(k, total // SEG_MIN or 1))
    if k == 1:
        return [items]
    segs, cur, acc = [], [], 0
    goal = total / k
    for it in items:
        cur.append(it)
        acc += len(it[1])
        if len(segs) < k - 1 and acc >= goal * (len(segs) + 1):
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def register(woof):
    def _mode() -> str:
        return "常規"  # 摘要固定用結構化版本；提示詞模式只屬於「提示詞與記憶」

    def _ratio() -> float:
        return max(5, min(int(woof.settings.get("ratio") or 20), 60)) / 100

    def _method() -> str:
        return "多工" if str(woof.settings.get("method") or "") == "多工" else "單次"

    def _parallel() -> int:
        if _method() != "多工":
            return 1
        return max(1, min(int(woof.settings.get("parallel") or 4), 8))

    def _trigger() -> float:
        return max(10, min(int(woof.settings.get("trigger") or 70), 95)) / 100

    async def _call(model: dict, system: str, text: str) -> str:
        from woof.agent.loop import aux_model, complete

        use = aux_model("compact", model) or model
        try:
            return (await complete(use, system, [{"role": "user", "content": [{"type": "text", "text": text}]}])).strip()
        except Exception as e:
            raise RuntimeError(f"壓縮摘要失敗（{use.get('label', use['id'])}）：{e}") from e

    async def _summarize(model: dict, prev: str, items: list[tuple[str, str]], target: int,
                         emit, session_id: str) -> tuple[str, int]:
        mode = _mode()
        base = _prompt_in("compact", mode)
        if prev:
            items = [("先前摘要", prev), *items]
        segs = _segments(items, _parallel())
        if len(segs) == 1:
            return await _call(model, f"{base}\n約 {target} 字以內。", _render(segs[0])), 1
        n = len(segs)
        per = max(200, target // n)
        await emit({"type": "notice", "session_id": session_id,
                    "text": f"字數較多，分成 {n} 段同時摘要（每段約 {per} 字）…"})
        parts = await asyncio.gather(*[
            _call(model, f"{base}\n{_prompt_in('compact_part', mode, i=i + 1, k=n)}\n本段約 {per} 字以內。", _render(seg))
            for i, seg in enumerate(segs)
        ])
        await emit({"type": "notice", "session_id": session_id, "text": f"{n} 段都摘要完成，正在合併…"})
        merged = "\n\n".join(f"【第 {i + 1} 段摘要】\n{p}" for i, p in enumerate(parts))
        tail = f"\n{_STRUCT}" if mode == "常規" else ""
        final = await _call(model, f"{_prompt_in('compact_merge', mode)}{tail}\n約 {target} 字以內。", merged)
        return final, n

    async def _precompact(session_id: str, model: dict, registry, emit) -> None:
        job = SimpleNamespace(session_id=session_id, model_cfg=model, registry=registry, emit=emit,
                              force_compact=False, is_bg=True, threshold=_trigger(), compact_reason="", compact_count=0)
        try:
            await prepare_turn(job)
        except Exception as e:
            try:
                await emit({"type": "notice", "session_id": session_id, "text": f"背景壓縮失敗：{e}"})
            except Exception:
                pass

    async def after_turn(ctx):
        from woof import store

        from woof import tokens

        window = tokens.window_of(ctx.model_cfg)
        if not window:
            return

        used = tokens.current(ctx.session_id)["used"]
        if used / window < _trigger():
            return
        if ctx.session_id in _bg and not _bg[ctx.session_id].done():
            return
        _bg[ctx.session_id] = asyncio.create_task(_precompact(ctx.session_id, ctx.model_cfg, ctx.registry, ctx.emit))

    async def prepare_turn(ctx):
        from woof import store
        from woof.agent.loop import aux_model

        pending_bg = _bg.get(ctx.session_id)
        if pending_bg and not pending_bg.done() and not getattr(ctx, "force_compact", False) and not getattr(ctx, "is_bg", False):
            await pending_bg  # 背景摘要還在跑，等它完成，下一回合直接用結果

        from woof import tokens

        model = ctx.model_cfg
        window = tokens.window_of(model)
        force = bool(getattr(ctx, "force_compact", False))
        can = model.get("provider") in ctx.registry.providers or aux_model("compact", None)
        if not can:
            if force:
                ctx.compact_reason = "這個模型不能寫摘要，請在「設定 → 輔助模型 → 壓縮」指定一個 API 型模型"
            return
        if not force and not window:
            return

        st = store.get_session(ctx.session_id) or {}
        # 上次模型實際回報的用量＋之後新增訊息的估計（剛貼進來的長文也算得到）
        used = tokens.current(ctx.session_id)["used"]
        trigger = getattr(ctx, "threshold", None) or _trigger()
        if not force and (not used or used / window < trigger):
            return

        mid = bool(getattr(ctx, "mid_turn", False))
        keep = max(1, min(int(woof.settings.get("keep") or 3), 30))
        msgs = store.get_messages(ctx.session_id)
        es = dict(st.get("engine_state") or {})
        summary = es.get("summary") or {}  # 讀取時的摘要，寫回前會重新讀取
        through = int(summary.get("through") or 0)
        pending = [m for m in msgs if m["id"] > through]
        # 一輪＝使用者的一則（不含內部訊息）＋之後模型的回覆與工具呼叫，保留區從第 keep 輪的開頭切
        starts = [i for i, m in enumerate(pending)
                  if m["role"] == "user" and not (m.get("meta") or {}).get("internal")]
        inflight = 0
        if len(starts) > keep:
            cut = starts[-keep]
        elif mid:
            # 回合進行中、這一輪自己就太長（工具結果一直疊）：從這一輪裡面切。
            # 保留最近 MID_KEEP 步（模型呼叫＋工具結果）原樣，使用者這次的要求在摘要後面重述（before_request）
            req = starts[-1] if starts else None
            lo = (req + 1) if req is not None else 0
            asst = [i for i in range(lo, len(pending)) if pending[i]["role"] == "assistant"]
            if len(asst) <= MID_KEEP:
                ctx.compact_reason = "這一回合的步數還太少，沒有可以壓縮的部分"
                return
            cut = asst[-MID_KEEP]
            inflight = pending[req]["id"] if req is not None else int(summary.get("inflight") or 0)
        else:
            ctx.compact_reason = f"還沒壓縮過的只有 {len(starts)} 輪，最近 {keep} 輪要保留原樣，不需要壓縮"
            return
        older, recent_ids = pending[:cut], {m["id"] for m in pending[cut:]}

        items = []
        for m in older:
            body = _text_of(m["content"])
            if body:
                items.append((m["role"], body[:MSG_CAP]))
        if not items:
            ctx.compact_reason = "較早的訊息都是空的，沒有可以摘要的內容"
            return
        prev = summary.get("text") or ""
        src = sum(len(b) for _, b in items)
        target = max(TARGET_MIN, min(TARGET_MAX, int((src + len(prev)) * _ratio())))
        mode = _mode()
        method = _method()

        why = (getattr(ctx, "compact_reason", "") or "手動壓縮") if force else f"上下文已用 {used / window:.0%}"
        if mid:
            why = "回合進行中，" + why
        ctx.compact_count = len(older)
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": f"{why}，正在壓縮較早的 {len(older)} 則訊息（{method}・{mode}模式，目標約 {target} 字）…"})
        text, parts = await _summarize(model, prev, items, target, ctx.emit, ctx.session_id)
        text = text.strip()
        if not text:
            raise RuntimeError("壓縮摘要是空的，已中止這次回合")
        cur = dict((store.get_session(ctx.session_id) or {}).get("engine_state") or {})
        cur["compress_count"] = int(cur.get("compress_count") or 0) + 1
        cur["summary"] = {"through": older[-1]["id"], "text": text, "at": time.time(),
                          "kept": sorted(recent_ids), "mode": mode, "method": method, "parts": parts,
                          "inflight": inflight}
        s2 = store.update_session(ctx.session_id, engine_state=cur)
        # 對話畫面據此在被摘要的訊息後面畫一條分隔線（可展開看摘要）
        await ctx.emit({"type": "session_updated", "session": s2})
        await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                        "text": (f"已把這一回合較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {MID_KEEP} 步保持原樣，"
                                 "使用者這次的要求會在摘要後面重述，模型接著做") if inflight else
                                f"已把較早的 {len(older)} 則摘要成 {len(text)} 字，最近 {keep} 輪（{len(recent_ids)} 則）保持原樣"})
        # 舊的用量回報不算數了：先換成估計值，下一次模型回報再更新（避免剛壓縮完又被判定要壓縮）
        u = tokens.reestimate(ctx.session_id, window or None)
        await ctx.emit({"type": "usage", "session_id": ctx.session_id, **u})
        return True

    def before_request(messages, ctx):
        from woof import store

        st = store.get_session(ctx.session_id) or {}
        summary = (st.get("engine_state") or {}).get("summary")
        if not summary or not summary.get("text"):
            return None
        through = int(summary.get("through") or 0)
        kept = [m for m in messages if int(m.get("id") or 0) > through]
        # history() 目前不帶 id，退回用序號：訊息是按 id 排序的
        if not any("id" in m for m in messages):
            return None
        body = _prompt_get("compact_head", summary=summary["text"])
        # 回合中途壓縮時，使用者這次的要求被摘要進去了：在摘要後面原文重述，模型才知道還在做什麼
        inf = int(summary.get("inflight") or 0)
        newer_user = any(m["role"] == "user" and not (m.get("meta") or {}).get("internal") for m in kept)
        if inf and inf <= through and not newer_user:
            req = store.get_message(inf)
            req_text = "\n".join(b.get("text") or "" for b in (req or {}).get("content") or [] if b.get("type") == "text").strip()
            if req_text:
                body += _prompt_get("compact_inflight", request=req_text)
        head = {"role": "user", "content": [{"type": "text", "text": body}]}
        return [head, *kept]

    async def force_compact(ctx):
        ctx.force_compact = True
        return bool(await prepare_turn(ctx))

    async def mid_turn(ctx):
        if str(woof.settings.get("mid_turn", True)).lower() in ("false", "0", "off"):
            return None
        # 斷路器：這一回合壓縮連續失敗 2 次（例如摘要模型一直 429）就不再每步重試，等上下文真的爆了（force）才再試
        fails = getattr(ctx, "compact_fails", 0)
        if fails >= 2 and not getattr(ctx, "force_compact", False):
            return None
        try:
            done = bool(await prepare_turn(ctx))
        except Exception as e:
            ctx.compact_fails = fails + 1
            if ctx.compact_fails == 2:
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"壓縮連續失敗兩次，這一回合先不再自動壓縮（{str(e)[:160]}）"})
            raise
        ctx.compact_fails = 0
        return done

    woof.on("prepare_turn", prepare_turn, order=20)
    woof.on("after_turn", after_turn, order=20)
    woof.on("force_compact", force_compact, order=20)
    woof.on("mid_turn", mid_turn, order=20)
    woof.on("before_request", before_request, order=20)
