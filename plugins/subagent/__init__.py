"""子代理。

delegate_task 會為每個子任務開一個子對話（側欄看得到，標記為子代理），
子代理用自己的上下文跑工具，跑完只把最後的報告交回主代理。
模型用「設定 → 模型用途 → 子代理」，沒設就跟主代理同一個。
主代理可以用 model 參數換模型，但只能挑使用者在插件設定「可指派的模型」勾的（防止模型自己挑貴的燒錢）；
每個使用者回合最多派幾個也有上限。子代理不能再派子代理（delegate_task 從子代理的工具裡拿掉）。
"""
from __future__ import annotations

import asyncio
import re


def _mode(t: dict) -> str:
    """有給 scope 卻漏了 mode：scope 只對寫入有意義，當成 write。"""
    m = str(t.get("mode") or "").strip().lower()
    if m in ("read", "write"):
        return m
    return "write" if str(t.get("scope") or "").strip() else "read"


def _rules(t: dict) -> str:
    if t.get("mode") != "write":
        return ("【派工規範】唯讀：只能讀取與查詢，不可修改任何檔案或外部狀態，不可再派子代理。"
                "這個任務如果要建立或修改檔案，不要改用別的方式硬做、也不要假裝做完：立刻回報「需要寫入權限」，"
                "列出要寫哪些檔案、放在哪個資料夾，主代理會改用寫入模式重派。")
    scope = str(t.get("scope") or "").strip()
    return ("【派工規範】寫入：只能修改下列範圍內的檔案：" + (scope or "（主代理沒指定範圍，請先回報，不要改動任何檔案）")
            + "。範圍外一律不動，只做這一個子任務，不可再派子代理。"
            + "可以用 terminal 跑測試、建置來驗證自己的修改：工作目錄限在範圍內，不能開背景指令，超時會被停止。")


def _scope_dirs(scope, cwd: str) -> list:
    """write 模式的 scope：多個路徑用 ; 或 , 分隔，相對路徑以主對話的工作目錄為準。"""
    import os
    from pathlib import Path

    out = []
    for raw in str(scope or "").replace(",", ";").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = Path(cwd) / p
        out.append(p.resolve())
    return out


def _kw(t: dict, parent) -> dict:
    if t.get("mode") == "write":
        return {"read_only": False, "write_scope": _scope_dirs(t.get("scope"), parent.cwd)}
    return {"read_only": True}


def _allowed(woof) -> list[str]:
    return [x.strip() for x in str(woof.settings.get("models") or "").split(",") if x.strip()]


def _pick_model(woof, t: dict) -> tuple[str, str]:
    """主代理指定的模型只能是使用者勾的「可指派的模型」；沒勾就一律用設定的子代理模型。回 (model_id, 錯誤)。"""
    want = str(t.get("model") or "").strip()
    if not want:
        return "", ""
    ok = _allowed(woof)
    if not ok:
        return "", ""  # 使用者沒開放：忽略主代理的選擇，用預設子代理模型（不報錯，免得多花一步）
    models = woof.config.get("models", [])
    hit = next((m for m in models if m["id"] in ok and want in (m["id"], m.get("model"), m.get("label"))), None)
    if hit is None:
        names = "、".join(f"{m.get('label', m['id'])}（{m['id']}）" for m in models if m["id"] in ok)
        return "", f"model「{want}」不在使用者開放的清單裡，只能用：{names}；或不填用預設"
    return hit["id"], ""


_GEN_WORDS = {"image": "image", "生圖": "image", "圖片": "image", "video": "video", "影片": "video", "生影片": "video"}


_GEN_GOAL = re.compile(
    r"^\s*(?:please\s+)?(?:generate|create|make|draw|render|produce|paint)\s+"
    r"(?:(?!(?:sure|conclusions?|from|of|for|to|with|that|which|code|scripts?|a\s+list|folders?|files?)\b)[\w:.-]+\s+){0,4}?"
    r"(?P<en>image|picture|illustration|photo|drawing|painting|video|clip|animation)s?\b"
    r"(?!\s+(?:folders?|files?|director(?:y|ies)|paths?|urls?|links?|list)\b)"
    r"|^\s*(?:請|幫我)?(?:生成|生|畫|做|產生)(?:一|兩|三|\d+)?(?:張|幅|段|支|部|個)?(?P<zh>圖片|圖|插畫|照片|影片|動畫|視頻)"
    r"|^\s*(?:請|幫我)?畫(?:一|兩|三|\d+)?(?P<zh2>張|幅)", re.I)


def _gen_by_goal(goal: str) -> str:
    mt = _GEN_GOAL.search(goal)
    if not mt:
        return ""
    w = (mt.group("en") or mt.group("zh") or mt.group("zh2") or "").lower()
    return "video" if w in ("video", "clip", "animation", "影片", "動畫", "視頻") else "image"


def _gen_target(woof, t: dict) -> dict | None:
    """model 填 image／video（或直接填生圖、影片模型的名字）→ 不開子對話，直接交給生成模型。
    模型順序：主模型直接點名的 → 模型用途「生圖／生影片」選的 → 子代理可指派裡同類的 → 第一個有 key 的同類模型。"""
    from woof import media
    from woof.loader import REG

    want = str(t.get("model") or "").strip()
    if not want:
        # 小模型常漏填 model：goal 一開頭就是「生成一張圖／Generate an image…」也算
        want = _gen_by_goal(str(t.get("goal") or ""))
        if not want:
            return None
    models = [m for m in woof.config.get("models", []) if m.get("enabled") is not False or media.kind_of(m) in ("image", "video")]
    named = next((m for m in models if want in (m["id"], m.get("model"), m.get("label"))), None)
    kind = _GEN_WORDS.get(want.lower()) or (media.kind_of(named) if named else "")
    if kind not in ("image", "video"):
        return None
    if named and media.kind_of(named) == kind:
        return named
    mp = REG.plugins.get("media")
    pref = str(((mp.settings if mp else None) or {}).get(f"{kind}_model") or "")
    if pref:  # 設定 → 模型用途 → 生圖／生影片
        hit = next((m for m in models if m["id"] == pref), None)
        if hit:
            return hit
    ok = _allowed(woof)
    hit = next((m for m in models if m["id"] in ok and media.kind_of(m) == kind), None)
    if hit:
        return hit
    for m in models:
        if media.kind_of(m) == kind and woof.keys(m.get("key_pool") or m.get("provider")).entries:
            return m
    return {"missing": kind}


async def _generate(woof, m: dict, t: dict, parent, progress) -> dict:
    """goal＝提示詞；context 不送進提示詞，只從 goal／context 找比例（16:9）、秒數（8 秒）、參考圖路徑；
    scope 指定資料夾就再複製一份過去。"""
    import re
    from pathlib import Path

    from woof import media

    prompt = str(t.get("goal") or "").strip()
    extra = str(t.get("context") or "").strip()
    text = prompt + "\n" + extra
    rx = "|".join(re.escape(x) for x in media.ALL_RATIOS)
    ratio = (re.search(rf"(?<![\d.:])({rx})(?![\d.:])", text) or [None, ""])[1]
    name = m.get("label", m["id"])

    def keep(path: str) -> str:
        import shutil

        dirs = _scope_dirs(t.get("scope"), parent.cwd)
        if len(dirs) == 1 and dirs[0].is_dir():
            dst = dirs[0] / Path(path).name
            shutil.copy2(path, dst)
            return f"（成品另存一份到 {dst}）"
        return "（存在這個對話的暫存，刪對話會一起刪；要留就在 scope 指定資料夾）"
    if media.kind_of(m) == "image":
        refs = []
        for raw in re.findall(r"[A-Za-z]:[\\/][^\s\"'<>|*?]+?\.(?:png|jpe?g|webp|gif)|[^\s\"'<>|*?:]+\.(?:png|jpe?g|webp|gif)", extra, re.I):
            fp = Path(raw) if Path(raw).is_absolute() else Path(parent.cwd) / raw
            if fp.is_file():
                refs.append(str(fp))
        await progress(f"用 {name} 生成圖片中…" + (f"（參考圖 {len(refs)} 張）" if refs else ""))
        r = await media.gen_image(woof, m, prompt, images=refs, ratio=ratio, session_id=parent.session_id)
        adj = f"（{'；'.join(r['notes'])}）" if r.get("notes") else ""
        return {"gen": f"已生成圖片（{name}，{r['seconds']} 秒）{adj}：{r['path']}{keep(r['path'])}\n回覆使用者時寫上 MEDIA:{r['path']} 讓介面顯示。"}
    sec = re.search(r"(\d{1,2})\s*(?:秒|s\b|sec)", text)
    res = (re.search(r"\b(360p|480p|720p|1080p|4k)\b", text, re.I) or [None, ""])[1]
    p, _ = media.settle(m, ratio=ratio, seconds=int(sec.group(1)) if sec else None, resolution=res)

    async def prog(pct, st):
        await progress(f"進度 {pct}%（{st}）")

    await progress(f"用 {name} 生成影片中（{p.get('seconds')} 秒、{p.get('ratio')}，通常 1–3 分鐘）…")
    r = await media.gen_video(woof, m, prompt, seconds=p.get("seconds"), aspect=p.get("ratio") or "", resolution=res, progress=prog,
                            session_id=parent.session_id)
    adj = f"（{'；'.join(r['notes'])}）" if r.get("notes") else ""
    return {"gen": f"已生成影片（{name}，{r['video_seconds']} 秒長）{adj}：{r['path']}{keep(r['path'])}\n回覆使用者時寫上 MEDIA:{r['path']} 讓介面顯示。"}


def register(woof):
    from woof.agent import loop as _loop


    async def delegate_task(args: dict, tctx) -> dict:
        from woof.agent.loop import run_subagent_bg

        parent = getattr(tctx, "turn", None)
        if parent is None:
            return {"output": "這個環境不能開子代理", "is_error": True}
        follow = str(args.get("session_id") or "").strip()
        if args.get("stop"):
            return _stop(follow, tctx)
        tasks = args.get("tasks") or []
        if args.get("goal"):
            tasks = [{"goal": args["goal"], "context": args.get("context", "")}] + list(tasks)
        tasks = [{"mode": args.get("mode"), "scope": args.get("scope"), "model": args.get("model"), **t} for t in tasks if isinstance(t, dict)]
        tasks = [t for t in tasks if str(t.get("goal") or "").strip()]
        for t in tasks:
            t["mode"] = _mode(t)
        if not tasks:
            return {"output": "至少要給一個 goal", "is_error": True}
        for t in tasks:
            g = _gen_target(woof, t)
            if g and g.get("missing"):
                from woof import media
                return {"output": f"沒有可用的{media.KIND_LABEL[g['missing']]}模型：請使用者到「設定 → 模型商」加一個", "is_error": True}
            if g:
                t["gen"] = g
                continue
            t["model"], err = _pick_model(woof, t)
            if err:
                return {"output": err, "is_error": True}
        # 名額只有一本帳：這個主對話底下還在跑或排隊的子代理（重開接回的、子代理再派的、等著接回的都算）。
        # 檢查和佔名額一次做完；滿了就退回，不偷偷排隊，主模型才知道要等誰。
        # 對還在跑的子代理追加要求是插話，不佔名額；生圖生影片不開子代理，也不佔
        from woof.agent.loop import sub_alive, sub_give_back, sub_limit, sub_live, sub_take

        root = getattr(parent, "root_session_id", parent.session_id)
        new = sum(1 for i, t in enumerate(tasks)
                  if not t.get("gen") and not (follow and i == 0 and sub_alive(follow)))
        ok, inuse = sub_take(root, new)
        if not ok:
            from woof import store as _store

            cap = sub_limit()
            names = "、".join(f"{k}（{((_store.get_session(k) or {}).get('title') or '')[:24]}）" for k in sub_live(root)[:12])
            free = max(0, cap - inuse)
            return {"output": f"子代理名額不夠：現在有 {inuse} 個在跑或排隊，同時最多 {cap} 個，這次要再開 {new} 個"
                              f"（只剩 {free} 個名額）。在跑的：{names or '（正在派出）'}。\n"
                              + (f"可以只派 {free} 個，其餘等前面的做完再派；" if free else "")
                              + "它們做完會自己回報，回報到了再派下一批；不需要的用 delegate_task 帶 session_id 加 stop=true 停掉就空出名額。"
                              "要改它們的方向，帶 session_id 追加要求（不佔名額）。同時最多幾個在「設定 → 模型用途 → 子代理」",
                    "is_error": True}

        async def progress(line: str) -> None:
            await tctx.output(line + "\n")

        async def one(i: int, t: dict):
            label = f"[{i + 1}] " if len(tasks) > 1 else ""
            try:
                if t.get("gen"):
                    return await _generate(woof, t["gen"], t, parent, lambda s: progress(label + s))
                ctx_text = (_rules(t) + "\n" + str(t.get("context") or "")).strip()
                kw = _kw(t, parent)
                sid = follow if (follow and i == 0) else ""
                if sid:
                    # 它還在跑：這句話是插話（催進度／追加要求），不要再開一輪
                    from woof.agent.loop import push_steer, sub_alive
                    if sub_alive(sid):
                        from woof.agent import internal as _internal
                        push_steer(sid, [{"type": "text", "text": str(t["goal"])}],
                                   _internal.meta("subagent_followup"))
                        from woof.agent import tasks as _t
                        last = (_t.last_text(sid, "assistant") or "").strip().replace("\n", " ")[:160]
                        return {"steered": True, "session_id": sid, "last": last}
                write = t.get("mode") == "write"
                if not sid:
                    dup = _same_job(str(t["goal"]))
                    if dup:
                        return {"dup": dup}
                # 寫入範圍重疊也同時跑（各寫各的檔）；同一個檔同時寫由寫檔工具逐次排隊，不會互蓋
                r = await run_subagent_bg(
                    parent, str(t["goal"]), ctx_text, str(t.get("model") or ""), None, label,
                    read_only=not write, write_scope=kw.get("write_scope"), session_id=sid,
                    kind="subagent_followup" if sid else "subagent_task")
                if isinstance(r, dict):
                    r["mode"] = "write" if write else "read"
                return r
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await progress(f"{label}失敗：{e}")
                return {"error": f"{type(e).__name__}: {e}", "goal": t["goal"]}

        def _same_job(goal: str) -> str:
            # 同一件事已經有子代理在跑或排隊（例如重開後忘了它還在）：不再派一個重複的
            from woof.agent.loop import bg_children
            from woof import store as _store

            g = " ".join(goal.split())
            for k in bg_children(getattr(parent, "root_session_id", parent.session_id)):
                first = next((m for m in _store.get_messages(k) if m["role"] == "user"), None)
                txt = " ".join("".join(b.get("text", "") for b in (first or {}).get("content", [])
                                       if b.get("type") == "text").split())
                if g and g in txt:
                    return k
            return ""

        from woof.agent.loop import SUB_BATCH
        import uuid as _uuid
        SUB_BATCH.set(_uuid.uuid4().hex[:8])   # 這一次派出的算同一批；gather 會把這個值帶進每個子任務
        try:
            results = await asyncio.gather(*[one(i, t) for i, t in enumerate(tasks)])
        finally:
            sub_give_back(root, new)   # 派出去的已經登記在帳上，正在派的這份還回去
        parts, bad = [], 0
        for i, r in enumerate(results):
            head = f"## 子任務 {i + 1}" if len(results) > 1 else "## 子代理"
            if "error" in r:
                bad += 1
                parts.append(f"{head}（派不出去）\n{r['error']}")
            elif r.get("dup"):
                parts.append(f"{head}沒有重派：同樣的工作子代理 {r['dup']} 還在跑或排隊，做完會回報。"
                             "要改方向帶 session_id 追加要求；不需要了帶 session_id 加 stop=true 停")
            elif r.get("steered"):
                parts.append(f"{head}它還在跑，這句話已經送進去了（對話 {r.get('session_id')}）："
                             "它這一步跑完就會看到，不用重派、也不用再催。要它的結論就等它自己回報。"
                             + (f"\n它最後在做的：{r['last']}" if r.get("last") else ""))
            elif r.get("background") or r.get("pending"):
                sid = r.get("session_id") or ""
                ro = r.get("mode") == "read"
                parts.append(f"{head}已派出，不用等它。\n"
                             + ("這是唯讀子代理：只能讀、查、搜尋，不會建檔改檔。如果這個任務其實要寫檔，現在就用 mode=write 加 scope 重派"
                                "（它回報「需要寫入權限」時也一樣，帶同一個 session_id 就能接著做）。\n" if ro else "")
                             + f"對話 {sid}（{r.get('model') or '子代理模型'}）在背景跑，跑完會自己把報告送回來。"
                             f"你可以先用一句話跟使用者說「派出去了，我在這裡陪你等」，或繼續別的動作；"
                             f"不要停下來等它，也不要反覆呼叫 delegate_task 去催。"
                             f"使用者也可以直接在那條子對話裡打字跟它講話。")
            elif r.get("gen"):
                parts.append(f"{head}\n{r['gen']}")
            else:
                parts.append(f"{head}\n{r}")
        return {"output": "\n\n".join(parts), "is_error": bad == len(results)}

    def _stop(sid: str, ctx) -> dict:
        """delegate_task 帶 stop=true：停下自己派出、還在跑的子代理。"""
        from woof.agent import loop as _l

        mine = {ctx.session_id, getattr(ctx, "root_session_id", ctx.session_id)}
        if not sid:
            kids = _l.bg_children(ctx.session_id)
            return {"output": "要停哪一個：帶 session_id 和 stop=true。還在跑的子代理：" + ("、".join(kids) if kids else "沒有"), "is_error": True}
        from woof import store as _store

        child = _store.get_session(sid)
        if not child or child.get("parent") not in mine:
            return {"output": f"{sid} 不是這個對話派出的子代理", "is_error": True}
        if not _l.stop_agent(sid):
            return {"output": f"子代理 {sid} 已經結束了，不用停；要它再做事就帶這個 session_id 呼叫 delegate_task"}
        return {"output": f"已停下子代理 {sid}，它停在哪、做了什麼會照樣送回報告；要它接著做或改做別的，帶同一個 session_id 呼叫 delegate_task"}

    woof.add_tool(
        "delegate_task",
        "把能獨立做的子任務交給背景子代理（乾淨上下文，做完自動送回報告，不用等、不要催）：大量搜尋讀檔、平行工作、會塞爆上下文的調查；也能生圖、生影"
        "片（見 model）。預設唯讀；要建檔改檔一定要 mode=write 加 scope。它看不到這個對話，goal、context 要寫齊需求、"
        "路徑、限制。帶同一個 session_id 再呼叫＝問進度、追加要求或接著做（還在跑就是插話），不要重派；使用者也可能直接在側欄那條對話裡跟"
        "它說話。帶 session_id 加 stop=true＝停下它，只在它的工作已不需要、方向錯了或跟使用者剛說的衝突時用；使用者插話不代表要"
        "停，要改方向就追加要求。",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "單一子任務：要達成什麼、完成標準"},
            "context": {"type": "string", "description": "背景：路徑、已知事實、限制"},
            "mode": {"type": "string", "enum": ["read", "write"],
                     "description": "read＝唯讀（預設）；write＝可建檔改檔，搭配 scope。範圍重疊也能同時跑，同一個檔一次寫一個"},
            "scope": {"type": "string", "description": "write 用：可改的資料夾或檔案，; 分隔，範圍外不准動。平行寫入各給各的資料夾"},
            "model": {"type": "string", "description": "選填，預設子代理模型；只能用設定開放的（填錯會列出可用的），簡單量大挑便宜的。image／video＝生圖／影片：goal 寫畫面（可含比例、秒數），context 放參考圖路徑，scope 填存檔資料夾"},
            "session_id": {"type": "string", "description": "之前回傳的子代理 id：接續、追加要求或停下"},
            "stop": {"type": "boolean", "description": "true＝停下 session_id 這個子代理，它的進度照樣回報"},
            "tasks": {"type": "array", "description": "一次派多個，欄位同上",
                      "items": {"type": "object", "properties": {
                          "goal": {"type": "string"}, "context": {"type": "string"},
                          "mode": {"type": "string", "enum": ["read", "write"]}, "scope": {"type": "string"},
                          "model": {"type": "string"}},
                          "required": ["goal"]}},
        }},
        delegate_task,
    )
