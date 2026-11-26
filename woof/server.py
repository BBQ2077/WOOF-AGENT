"""FastAPI 伺服器：REST、WebSocket、本機檔案、前端靜態檔。"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import shutil
import time
import tomllib
import uuid
import zipfile
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cron, keypool, procs, remote, store
from .agent.loop import (AGENT_RUNS, COMPACTING, sub_alive, flush_steer, mark_interrupted, peek_steer, pending_approvals, pop_steer,
                         push_steer, requeue_steer, reset_allow_all,
                         resolve_approval, resume_session, run_turn)
from . import config
from .config import DATA, PLUGINS, WEB_DIST, load_config, load_keys, save_config, save_keys
from .loader import REG
from .prompts import get as _prompt_get

log = logging.getLogger("woof.server")
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(exist_ok=True)
FILES = DATA / "files"   # 使用者附加的檔案（xlsx、pdf、程式碼…），跟貼圖一樣跟對話一起刪
FILES.mkdir(exist_ok=True)

app = FastAPI(title="WOOF")
TOKEN: str = ""
CLIENTS: set[WebSocket] = set()
RUNS: dict[str, asyncio.Task] = {}
TURN_LOCK = asyncio.Lock()


def _check(req_token: str | None) -> None:
    st = remote.state()
    if not remote.check(bool(st["open_mode"]), "/", req_token, TOKEN):
        raise HTTPException(401, "token 錯誤")


def _authed(request: Request) -> bool:
    """只看「有沒有過關」，不限定用哪一種憑證。"""
    st = remote.state()
    tok = request.headers.get("x-woof-token") or request.query_params.get("token")
    return remote.check(bool(st["open_mode"]), request.url.path, tok, TOKEN)


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/api/") and not _authed(request):
        return JSONResponse({"detail": "token 錯誤"}, status_code=401)
    return await call_next(request)


# 桌面版的畫面從 Electron 的 woof://app/ 載入（不是從這個後端），呼叫 API 屬於跨來源，只放行這一個來源
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware, allow_origins=["woof://app"], allow_methods=["*"], allow_headers=["*"])


async def _watch_bg_tasks() -> None:
    """每 2 秒看一次背景任務；結束的廣播 task_end（只通知背景任務，前景的已在工具結果裡）。"""
    from woof import procs
    done = {t.id for t in procs.TASKS.values() if t.ended is not None}
    while True:
        await asyncio.sleep(2)
        for t in list(procs.TASKS.values()):
            if t.ended is None or t.id in done:
                continue
            done.add(t.id)
            if t.background:
                await broadcast({"type": "task_end", "id": t.id, "command": t.command[:160],
                                 "exit_code": t.exit_code, "killed": bool(t.killed), "session_id": t.session_id,
                                 "ms": int((t.ended - t.started) * 1000)})


def _busy(sid: str | None) -> bool:
    """這個對話是不是「還有事在跑」：回合、目標／方案、對抗審核子代理、等人回答的提問。
    前端用它決定要不要顯示「上一回合沒做完」——還在跑就不該顯示，會讓人以為卡死。"""
    if not sid:
        return False
    from woof.agent import tasks as _tasks

    if sid in RUNS and not RUNS[sid].done():
        return True
    if sub_alive(sid):
        return True   # 子代理在跑、排隊等名額、重開後剛接回都算，側欄照樣亮
    try:
        if _tasks.is_running(sid) or _tasks.has_ask(sid):
            return True
    except Exception:
        pass
    return False


def _flow(sid: str | None) -> bool:
    """方案／目標流程還在跑、但不在回合裡也不是在等人回答：兩步之間（拓展、審核、準備下一步）。
    前端靠它在這段空檔繼續顯示「進行中」，不然看起來像卡住。"""
    if not sid:
        return False
    from woof.agent import tasks as _tasks

    try:
        return _tasks.is_running(sid) and not _tasks.has_ask(sid)
    except Exception:
        return False


async def broadcast(ev: dict) -> None:
    sid = ev.get("session_id")
    if sid and "busy" not in ev:
        ev["busy"] = _busy(sid)   # 每個事件都順便告訴前端「這個對話還在不在跑」
        ev["flow"] = _flow(sid)
    if sid and "compacting" not in ev:
        ev["compacting"] = sid in COMPACTING
    dead = []
    for ws in list(CLIENTS):
        try:
            await ws.send_json(ev)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CLIENTS.discard(ws)


# ---------------------------------------------------------------- REST
_LOGIN_FAILS: dict[str, list[float]] = {}   # 來源 IP → 最近幾次失敗的時間（每 5 分鐘最多 8 次）
_LOGIN_LOCK: dict[str, float] = {}          # 來源 IP → 鎖到什麼時候


def _client_ip(request: Request) -> str:
    """trust_proxy 打開時才看 X-Forwarded-For（反向代理會帶真實來源）。"""
    if remote.state()["trust_proxy"]:
        fwd = request.headers.get("x-forwarded-for") or ""
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _login_blocked(ip: str) -> float:
    """回傳還要等幾秒；0＝可以試。"""
    until = _LOGIN_LOCK.get(ip, 0)
    return max(0, int(until - time.time()))


@app.get("/api/session")
async def session_info():
    """介面開場先問這個：要不要登入。不需登入時也回目前連線方式。"""
    st = remote.state()
    return {"open": bool(st["open_mode"]), "bind": st["bind"],
            "password": bool(st["password_hash"])}


@app.post("/api/login")
async def login(body: dict, request: Request):
    st = remote.state()
    if not st["open_mode"]:
        return {"token": "", "expires": 0}
    ip = _client_ip(request)
    wait = _login_blocked(ip)
    if wait:
        raise HTTPException(429, f"登入失敗次數太多，請 {wait} 秒後再試")
    if not remote.verify_password(str(body.get("password") or ""), st["password_hash"]):
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if t > time.time() - 300]
        fails.append(time.time())
        _LOGIN_FAILS[ip] = fails
        if len(fails) >= 8:
            _LOGIN_LOCK[ip] = time.time() + 300
            _LOGIN_FAILS.pop(ip, None)
        raise HTTPException(401, "密碼不對")
    _LOGIN_FAILS.pop(ip, None)
    _LOGIN_LOCK.pop(ip, None)
    token, exp = remote.issue(int(st["session_days"]))
    return {"token": token, "expires": exp}


@app.get("/api/state")
async def state():
    return {
        "profile": _profile_state(),
        "models": REG.models(),
        "vendors": REG.vendors(),
        "backends": sorted(set(REG.providers) | set(REG.engines)),
        "plugins": _plugin_rows(),
        "tools": [{"name": t.name, "plugin": t.plugin} for t in REG.tools.values()],
        "keys": keypool.snapshot(),
        "running": [sid for sid, t in RUNS.items() if not t.done()],
        "approvals": pending_approvals(),
    }


@app.get("/api/version")
async def app_version():
    """程式本身的版本（pyproject.toml）。只換 B 包時外殼版本不變，畫面上的版本號看這個。"""
    return {"version": _app_version()}


_VER: list[str] = []


def _app_version() -> str:
    if not _VER:
        try:
            with open(config.ROOT / "pyproject.toml", "rb") as f:
                _VER.append(str(tomllib.load(f)["project"]["version"]))
        except Exception:
            _VER.append("")
    return _VER[0]


# ---------------------------------------------------------------- 設定組
def _profile_state() -> dict:
    from . import profiles

    return {"id": config.PROFILE, "name": profiles.current().name, "count": len(profiles.all_profiles())}


@app.get("/api/profiles")
async def profile_list():
    from . import profiles

    cur = profiles.current()
    return {"current": cur.id, "can_switch": bool(os.environ.get("WOOF_DESKTOP")),
            "profiles": [{**p.as_dict(), "current": p.id == cur.id} for p in profiles.all_profiles()]}


@app.post("/api/profiles")
async def profile_create(body: dict):
    from . import profiles

    try:
        p = profiles.create(str(body.get("name") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**p.as_dict(), "current": False}


@app.patch("/api/profiles/{pid}")
async def profile_rename(pid: str, body: dict):
    from . import profiles

    try:
        p = profiles.rename(pid, str(body.get("name") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**p.as_dict(), "current": p.id == config.PROFILE}


@app.delete("/api/profiles/{pid}")
async def profile_delete(pid: str):
    from . import profiles

    if pid == config.PROFILE:
        raise HTTPException(400, "不能刪除目前正在用的這組，先切換到別組再刪")
    try:
        profiles.remove(pid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/profiles/{pid}/switch")
async def profile_switch(pid: str):
    """切換設定組：這裡只記下「下次開的是哪一組」，實際重啟後端由桌面程式執行。"""
    from . import profiles

    if pid == config.PROFILE:
        return {"ok": True, "restart": False, "profile": _profile_state()}
    if not any(p.id == pid for p in profiles.all_profiles()):
        raise HTTPException(404, "找不到這組設定")
    try:
        profiles.set_last(pid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "restart": True, "profile": {"id": pid, "name": profiles.get(pid).name, "count": len(profiles.all_profiles())}}


@app.get("/api/usage")
async def usage_api(range: str = "today", sid: str = ""):
    from datetime import datetime
    from woof import config as wcfg, cost

    u = store.usage_summary(range, sid or None)
    cfg = wcfg.load_config()
    mid = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since = {"today": mid, "7d": mid - 6 * 86400, "30d": mid - 29 * 86400}.get(range, 0.0)
    u["budget"] = cost.status(cfg)
    if not u["budget"]["enabled"]:
        return u
    sp = cost.spent(cfg, since, sid or None)
    u["cost"] = sp["total"]
    u["unpriced"] = sp["unpriced"]
    for m in u["by_model"]:
        m["cost"] = sp["by_model"].get(m["model"])
    for d in u["daily"]:
        d["cost"] = sp["by_day"].get(d["day"], 0.0)
    return u


@app.post("/api/budget")
async def budget_save(body: dict):
    from woof import config as wcfg, cost

    cfg = wcfg.load_config()
    try:
        b = cost.clean_budget({**(cfg.get("budget") or {}), **body})  # 只送有改的欄位（例如設定頁只切開關）
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg["budget"] = b
    save_config(cfg)
    REG.config["budget"] = b
    return cost.status(cfg)


@app.get("/api/quota")
async def quota_api(force: int = 0):
    """各商剩餘額度（只列有 key 的商）。帳單日取 config 的 billing_day，有提供實際重置日的以商家為準。"""
    from woof import config as wcfg, keypool, quota
    cfg = wcfg.load_config()
    rows = []
    seen = set()
    for v in cfg.get("vendors", []) or []:
        pool_name = v.get("key_pool")
        if not pool_name or pool_name in seen or not keypool.pool(pool_name).entries:
            continue
        seen.add(pool_name)
        r = await quota.query(pool_name, force=bool(force))
        row = {"vendor": v.get("id"), "label": v.get("label", v.get("id")), "pool": pool_name, **r}
        if v.get("billing_day"):
            row["billing_day"] = int(v["billing_day"])
            row["next_billing"] = quota.next_billing_day(int(v["billing_day"]))
        rows.append(row)
    return {"vendors": rows}


@app.get("/api/prompt-files")
async def prompt_files_list():
    from woof import promptfiles
    return {"files": [promptfiles.info(k) for k in promptfiles.TABS], "order": promptfiles.TABS}


@app.put("/api/prompt-files/{key}")
async def prompt_file_save(key: str, body: dict):
    from woof import promptfiles
    if key not in promptfiles.KEYS:
        raise HTTPException(404)
    promptfiles.write(key, str(body.get("text") or ""))
    return promptfiles.info(key)


@app.put("/api/prompt-files/{key}/limit")
async def prompt_file_limit(key: str, body: dict):
    """改字數上限（存在 data/prompt_limits.json，預設值在 woof/promptfiles.py）。"""
    from woof import promptfiles
    if key not in promptfiles.KEYS:
        raise HTTPException(404)
    try:
        promptfiles.set_limit(key, body.get("limit"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return promptfiles.info(key)


@app.get("/api/sessions/{sid}/prompt")
async def get_session_prompt(sid: str):
    """總提示詞面板：系統提示詞分段、本回合工具清單、輔助模型、WEB 搜尋順序。
    和實際送出用同一套組裝（build_parts／tool_overview），只是不跑 prepare_turn（會觸發壓縮與 MCP 連線）。"""
    from woof.agent.loop import base_prompt, build_parts, find_model, preview_ctx, prompt_override, tool_overview

    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    from woof.agent.loop import prompt_fp, prompt_gen

    ctx = preview_ctx(s, find_model(s.get("model")))
    parts = await build_parts(ctx)
    ov = prompt_override(s)
    now_text = "\n\n".join(p["text"] for p in parts)
    snap = store.get_prompt_snapshot(sid)
    # 這個對話送出過之後，系統提示詞固定成快照；設定改了要等壓縮、新對話或按「立即套用」
    valid = bool(snap and snap["fp"] == prompt_fp(ctx) and int(snap["gen"] or 0) == prompt_gen(sid))
    frozen = {"at": snap["at"], "stale": now_text != snap["text"], "text": snap["text"]} if valid else None
    aux = REG.config.get("aux") or {}
    aux_rows = []
    for task in AUX_TASKS:
        want = aux.get(task) or ""
        m = find_model(want) if want else None
        aux_rows.append({"task": task, "model_id": want,
                         "label": (m or {}).get("label") or want or ("不使用" if task in ("fallback", "review") else "自動（沿用對話模型）")})
    web = REG.plugins.get("web_search")
    web_order = (web.settings.get("order") if web is not None else "") or ""
    return {
        "default": base_prompt(),
        "override": ov,
        "effective": frozen["text"] if frozen else now_text,
        "current": now_text,
        "frozen": frozen,
        "parts": parts,
        "tools": await tool_overview(ctx),
        "aux": aux_rows,
        "web_order": web_order,
        "focus": (s.get("engine_state") or {}).get("focus_last"),
        "told": (s.get("engine_state") or {}).get("told"),
    }


@app.delete("/api/sessions/{sid}/prompt-snapshot")
async def drop_prompt_snapshot(sid: str):
    """立即套用新的系統提示詞：丟掉這個對話固定的那份，下一次送出時重新組（模型的前綴快取會重來一次）。"""
    if store.get_session(sid) is None:
        raise HTTPException(404)
    store.clear_prompt_snapshot(sid)
    return {"ok": True}


@app.get("/api/sessions/{sid}/context")
async def session_context(sid: str):
    """上下文用量：上次模型實際回報＋之後新增訊息的估計。"""
    from . import tokens
    if store.get_session(sid) is None:
        raise HTTPException(404)
    return tokens.current(sid)


@app.get("/api/sessions/{sid}/stats")
async def get_session_stats(sid: str):
    """對話統計：回合、模型呼叫、工具呼叫（含失敗）、用量。"""
    if store.get_session(sid) is None:
        raise HTTPException(404)
    return store.session_stats(sid)




@app.put("/api/sessions/{sid}/engine")
async def session_engine(sid: str, body: dict):
    """對話層級設定：style（輸出風格 id）。"""
    from . import styles

    st = store.get_session(sid)
    if st is None:
        raise HTTPException(404)
    es = dict(st.get("engine_state") or {})
    if "style" in body:
        if styles.get(body["style"]) is None:
            raise HTTPException(400, "風格不存在")
        es["style"] = body["style"]
    s2 = store.update_session(sid, engine_state=es)
    await broadcast({"type": "session_updated", "session": s2})
    return {"style": es.get("style", "default")}


@app.get("/api/styles")
async def list_styles():
    from . import styles

    return {"styles": styles.all_styles()}


@app.post("/api/styles")
async def save_style(body: dict):
    """新增（不帶 id）或修改（帶 id）一個輸出風格。"""
    from . import styles

    try:
        s = styles.save(body.get("id") or None, str(body.get("name") or ""), str(body.get("text") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await broadcast({"type": "styles_changed"})
    return s


@app.delete("/api/styles/{style_id}")
async def delete_style(style_id: str):
    """自訂的刪掉；內建的還原成原本內容。用到這個風格的對話改回預設。"""
    from . import styles

    s = styles.get(style_id)
    if s is None:
        raise HTTPException(404)
    styles.delete(style_id)
    if not s["builtin"]:
        for x in store.list_sessions():
            es = dict(x.get("engine_state") or {})
            if es.get("style") == style_id:
                es["style"] = "default"
                store.update_session(x["id"], engine_state=es)
    await broadcast({"type": "styles_changed"})
    await broadcast({"type": "state_changed"})
    return {"ok": True}


@app.put("/api/styles/{style_id}/pin")
async def pin_style(style_id: str, body: dict):
    from . import styles

    try:
        styles.pin(style_id, bool(body.get("pinned")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await broadcast({"type": "styles_changed"})
    return {"ok": True}


@app.post("/api/sessions/{sid}/rewind")
async def session_rewind(sid: str, body: dict):
    """回溯對話：保留 message_id 那則（含）之前的訊息，之後的全部刪掉。只動對話，不還原檔案。
    摘要若涵蓋到被刪的範圍，一併清掉（之後會重新摘要）。"""
    st = store.get_session(sid)
    if st is None:
        raise HTTPException(404)
    try:
        mid = int(body.get("message_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "message_id 必須是數字")
    if store.get_message(mid) is None or store.get_message(mid).get("session_id") != sid:
        raise HTTPException(404, "找不到這則訊息")
    es = dict(st.get("engine_state") or {})
    removed = store.delete_messages_after(sid, mid)
    summ = es.get("summary") or {}
    if summ and int(summ.get("through") or 0) > mid:
        es.pop("summary", None)
    if int((es.get("prune") or {}).get("through") or 0) > mid:
        es.pop("prune", None)
    store.update_session(sid, engine_state=es)
    if int((es.get("usage") or {}).get("through") or 0) > mid:
        from . import tokens
        u = tokens.reestimate(sid)
        await broadcast({"type": "usage", "session_id": sid, **u})
    await broadcast({"type": "state_changed"})
    return {"ok": True, "removed": removed, "kept_through": mid}


@app.get("/api/sessions/{sid}/snapshots")
async def session_snapshots(sid: str):
    """工作區快照：每則使用者訊息之前，模型改過的檔原本的樣子。"""
    from . import snapshot
    cps = snapshot.checkpoints(sid)
    msgs = {m["id"]: m for m in store.get_messages(sid) if m["role"] == "user"}
    for c in cps:
        m = msgs.get(c["mid"]) if isinstance(c["mid"], int) else None
        c["text"] = "".join(b.get("text", "") for b in (m or {}).get("content", []) if b.get("type") == "text")[:80]
        c["gone"] = isinstance(c["mid"], int) and m is None
    return {"checkpoints": cps}


@app.post("/api/sessions/{sid}/snapshots/restore")
async def session_snapshot_restore(sid: str, body: dict):
    """把檔案還原到某則訊息之前（body: {message_id, path?}）。只動檔案，不動對話。"""
    from . import snapshot
    if sid in RUNS and not RUNS[sid].done():
        raise HTTPException(409, "對話執行中，先停止再還原")
    try:
        mid = int(body.get("message_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "message_id 必須是數字")
    r = snapshot.restore(sid, mid, body.get("path") or None)
    await broadcast({"type": "snapshots_changed", "session_id": sid})
    return r


@app.get("/api/translate")
async def translate_status():
    """離線英翻中的模型：下載了沒、多大、下載進度。"""
    from . import translate

    return translate.status()


@app.post("/api/translate/download")
async def translate_download():
    """在背景下載模型（已有且核對正確的不重抓）；進度用 GET /api/translate 看。"""
    from . import translate

    asyncio.get_running_loop().run_in_executor(None, translate.download)
    return translate.status()


@app.delete("/api/translate")
async def translate_remove():
    from . import translate

    await asyncio.to_thread(translate.remove)
    return translate.status()


@app.get("/api/translate/file/{kind}")
async def translate_file(kind: str):
    from . import translate

    p = translate.path_of(kind)
    if not p:
        raise HTTPException(404, "模型還沒下載")
    return FileResponse(str(p), media_type="application/wasm" if kind == "wasm" else "application/octet-stream")


@app.get("/api/snapshots")
async def snapshots_usage():
    """檔案快照佔多少空間、保留規則。"""
    from . import snapshot

    return await asyncio.to_thread(snapshot.usage)


@app.put("/api/snapshots/settings")
async def snapshots_settings(body: dict):
    from . import snapshot

    try:
        new = snapshot.clean_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = load_config()
    cfg["snapshots"] = new
    save_config(cfg)
    return new


@app.post("/api/snapshots/prune")
async def snapshots_prune():
    from . import snapshot

    live = {s["id"] for s in store.list_sessions()}
    r = await asyncio.to_thread(snapshot.prune, live)
    return {**r, **(await asyncio.to_thread(snapshot.usage))}


@app.post("/api/sessions/{sid}/snapshots/undo")
async def session_snapshot_undo(sid: str):
    from . import snapshot
    r = snapshot.undo(sid)
    await broadcast({"type": "snapshots_changed", "session_id": sid})
    return r


@app.put("/api/sessions/{sid}/prompt")
async def set_session_prompt(sid: str, body: dict):
    """body: {"text": "..."}；text 為空＝還原成預設組合。"""
    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    text = str(body.get("text") or "")
    es = s.get("engine_state") or {}
    if text.strip():
        es["prompt_override"] = text
    else:
        es.pop("prompt_override", None)
    store.update_session(sid, engine_state=es)
    return {"ok": True, "override": es.get("prompt_override")}


@app.post("/api/sessions/{sid}/plugins")
async def session_plugins(sid: str, body: dict):
    """body: {"off": [插件名...]}；只影響這個對話的工具。"""
    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    off = sorted({str(x) for x in body.get("off") or [] if str(x) in REG.plugins})
    es = s.get("engine_state") or {}
    es["plugins_off"] = off
    es["template"] = None  # 手動改過就不再是模板
    store.update_session(sid, engine_state=es)
    return {"ok": True, "off": off}


@app.get("/api/presets")
async def presets_api():
    """模式預設組：一鍵套用「哪些插件可用」＋「工具權限」。"""
    from . import presets

    return {"presets": presets.all_presets(), "plugins": sorted(REG.plugins.keys()),
            "default": presets.default_id()}


@app.post("/api/presets")
async def presets_save(body: dict):
    from . import presets

    presets.save(body.get("presets") or [])
    await broadcast({"type": "state_changed"})
    return {"ok": True, "presets": presets.all_presets()}


@app.delete("/api/presets/{pid}")
async def preset_delete(pid: str):
    from . import presets

    if not presets.delete(pid):
        raise HTTPException(404, "沒有這個模式（內建的不能刪）")
    if presets.default_id() == pid:
        presets.set_default("")
    await broadcast({"type": "state_changed"})
    return {"ok": True, "presets": presets.all_presets(), "default": presets.default_id()}


@app.put("/api/presets/default")
async def preset_default(body: dict):
    """設成新對話的預設模式；id 為空白＝不指定。"""
    from . import presets

    try:
        presets.set_default(str(body.get("id") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await broadcast({"type": "state_changed"})
    return {"ok": True, "default": presets.default_id()}


@app.post("/api/sessions/{sid}/preset")
async def apply_preset(sid: str, body: dict):
    """把整個模式套到這個對話：權限下一則訊息就換；插件開關排到下次壓縮或新對話才換，
    中途換插件會讓這個對話的系統提示詞重組、前綴快取失效。"""
    from . import presets
    from .agent.loop import prompt_gen

    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    p = presets.get(str(body.get("id") or ""))
    if p is None:
        raise HTTPException(404, "沒有這個模式")
    off = presets.off_for(p)
    es = dict(s.get("engine_state") or {})
    sent = bool(store.get_messages(sid))
    if sent:
        es["pending_off"] = off
        es["pending_gen"] = prompt_gen(sid)
    else:
        es["plugins_off"] = off
        es.pop("pending_off", None)
        es.pop("pending_gen", None)
    es["preset"] = p["id"]
    reset_allow_all(sid)
    s2 = store.update_session(sid, perm=p["perm"], engine_state=es) or s
    await broadcast({"type": "session_updated", "session": s2})
    return {"ok": True, "perm": p["perm"], "off": off, "pending": sent, "preset": p["id"]}


@app.get("/api/templates")
async def templates_api():
    return {"templates": load_config().get("templates") or [], "plugins": sorted(REG.plugins.keys())}


@app.post("/api/templates")
async def save_templates(body: dict):
    cfg = load_config()
    clean = []
    for t in body.get("templates") or []:
        if not isinstance(t, dict) or not str(t.get("id") or "").strip():
            continue
        tid = str(t["id"]).strip()
        clean.append({"id": tid, "label": str(t.get("label") or tid).strip(),
                      "plugins": sorted({str(x) for x in t.get("plugins") or [] if str(x) in REG.plugins})})
    cfg["templates"] = clean
    save_config(cfg)
    return {"ok": True, "templates": clean}


@app.post("/api/sessions/{sid}/template")
async def apply_template(sid: str, body: dict):
    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    tid = str(body.get("id") or "")
    tpl = next((t for t in (load_config().get("templates") or []) if t.get("id") == tid), None) if tid else None
    if tid and tpl is None:
        raise HTTPException(404, "沒有這個模板")
    allowed = set(tpl["plugins"]) if tpl else set(REG.plugins.keys())
    off = sorted(set(REG.plugins.keys()) - allowed)
    es = s.get("engine_state") or {}
    es["plugins_off"] = off
    es["template"] = tid or None
    store.update_session(sid, engine_state=es)
    return {"ok": True, "off": off, "template": tid or None}


# ---------------------------------------------------------------- 寵物
@app.get("/api/pet")
async def pet_get():
    from . import pet

    m = pet.pick_model()
    return {"cfg": pet.cfg(), "state": pet.state(), "faces": pet.FACES, "chars": pet.chars(),
            "model": (m or {}).get("id"), "label": (m or {}).get("label"),
            "history": pet.history()[-40:]}


@app.put("/api/pet")
async def pet_put(body: dict):
    from . import config as wcfg, pet

    cfg = wcfg.load_config()
    cur = dict(cfg.get("pet") or {})
    cur.update(pet.clean(body or {}))
    cfg["pet"] = cur
    save_config(cfg)
    REG.config["pet"] = cur
    m = pet.pick_model()
    await broadcast({"type": "pet", "pet": pet.state(), "history": pet.history()[-40:]})
    return {"cfg": pet.cfg(), "state": pet.state(), "faces": pet.FACES, "chars": pet.chars(),
            "model": (m or {}).get("id"), "label": (m or {}).get("label"),
            "history": pet.history()[-40:]}


@app.post("/api/pet/say")
async def pet_say(body: dict):
    from . import pet

    text = str((body or {}).get("text") or "").strip()
    att = pet.save_att((body or {}).get("att") or [])
    if not text and not att:
        raise HTTPException(400, "要說點什麼")
    sid = str((body or {}).get("session_id") or "") or None
    if sid:
        pet.watching(sid)
    r = await pet.talk(text, sid, broadcast, att=att)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    r["history"] = pet.history()[-40:]
    return r


@app.post("/api/pet/glance")
async def pet_glance(body: dict):
    from . import pet

    sid = str((body or {}).get("session_id") or "")
    if not sid:
        raise HTTPException(400, "要指定是哪個對話")
    pet.watching(sid)
    r = await pet.glance(sid, broadcast, force=True)
    return {"state": pet.state(), "spoke": bool(r), "history": pet.history()[-40:]}


@app.post("/api/pet/poke")
async def pet_poke(body: dict):
    """使用者點了他的圖（摸頭）；連點幾下算一次，帶次數。"""
    from . import pet

    n = int((body or {}).get("n") or 1)
    sid = (body or {}).get("session_id") or None
    r = await pet.talk(pet.poke_text(n), sid, broadcast, kind="poke")
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {**r, "history": pet.history()[-40:]}


@app.post("/api/pet/react")
async def pet_react(body: dict):
    """使用者對他的某一則按表情；同一個再按一次就收回。"""
    from . import pet

    mid = str((body or {}).get("id") or "")
    if not mid:
        raise HTTPException(400, "要指定是哪一則")
    r = await pet.user_react(mid, str((body or {}).get("emoji") or ""), broadcast)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return {"state": r, "history": pet.history()[-40:]}


@app.get("/api/pet/history")
async def pet_history(before: str = "", limit: int = 60):
    """寵物紀錄往上捲：before 那則之前的 limit 則（含壓縮掉、只留給畫面看的舊紀錄）。"""
    from . import pet

    return pet.history_before(before, limit)


@app.post("/api/pet/reset")
async def pet_reset():
    from . import pet

    pet.reset_chat()
    await broadcast({"type": "pet", "pet": pet.state(), "history": []})
    return {"state": pet.state(), "history": []}


@app.get("/api/pet/file")
async def pet_file_get():
    from . import pet

    return {"text": pet.persona(), "faces": pet.FACES, "state": pet.state()}


@app.put("/api/pet/file")
async def pet_file_put(body: dict):
    from . import pet

    pet.save_persona((body or {}).get("text") or "")
    return {"ok": True, "text": pet.persona()}


@app.get("/api/pet/sprite/{face}")
async def pet_sprite(face: str, char: str | None = None):
    from fastapi.responses import FileResponse

    from . import pet

    if face not in pet.FACES:
        raise HTTPException(404)
    f = pet.sprite(face, char)
    if not f:
        raise HTTPException(404)
    return FileResponse(str(f), media_type="image/webp")


@app.on_event("startup")
async def _pet_beat():
    from . import pet

    pet.start_heartbeat()


# ---------------------------------------------------------------- 對話群組
@app.get("/api/groups")
async def groups_list():
    return store.list_groups()


@app.post("/api/groups")
async def groups_add(body: dict):
    name = (str(body.get("name") or "")).strip()[:40]
    if not name:
        raise HTTPException(400, "群組要有名稱")
    g = store.create_group(name)
    await broadcast({"type": "groups_changed"})
    return g


@app.patch("/api/groups/{gid}")
async def groups_patch(gid: str, body: dict):
    allowed = {k: v for k, v in body.items() if k in ("name", "collapsed", "pos")}
    if "name" in allowed:
        allowed["name"] = str(allowed["name"] or "").strip()[:40]
        if not allowed["name"]:
            raise HTTPException(400, "群組要有名稱")
    g = store.update_group(gid, **allowed)
    if g is None:
        raise HTTPException(404)
    await broadcast({"type": "groups_changed"})
    return g


@app.delete("/api/groups/{gid}")
async def groups_delete(gid: str, sessions: int = 0):
    """刪群組。sessions=1 連裡面的對話一起刪（介面會先二次確認）。"""
    if not any(g["id"] == gid for g in store.list_groups()):
        raise HTTPException(404)
    ids = store.group_session_ids(gid) if sessions else []
    store.delete_group(gid)
    n = await _purge_sessions(ids) if ids else 0
    await broadcast({"type": "groups_changed"})
    return {"ok": True, "deleted": n}


@app.get("/api/sessions")
async def sessions():
    rows = store.list_sessions()
    for r in rows:
        r["busy"] = _busy(r.get("id"))
        r["flow"] = _flow(r.get("id"))
        r["compacting"] = r.get("id") in COMPACTING
    return rows


@app.post("/api/sessions")
async def create_session(body: dict):
    model = body.get("model")
    cfg = REG.model_cfg(model)
    effort = body.get("effort") or (cfg or {}).get("default_effort")
    s = store.create_session(model or (cfg or {}).get("id"), effort)
    from . import perms, presets
    perm = body.get("perm") if body.get("perm") in perms.MODES else perms.settings()["default_mode"]
    s = store.update_session(s["id"], perm=perm) or s
    if body.get("preset") is None:  # 前端沒指定就用設定裡的新對話預設模式
        p = presets.for_new_session()
        if p:
            s = presets.apply_to_new(s, p) or s
    await REG.events.emit("on_session", "create", s)
    return s


@app.patch("/api/sessions/{sid}")
async def patch_session(sid: str, body: dict):
    allowed = {k: v for k, v in body.items() if k in ("title", "model", "effort", "pinned", "perm", "archived", "grp")}
    if "parent" in body and not body.get("parent"):
        allowed["parent"] = None   # 把子代理拖出來變成獨立對話（只能拆開，不能掛到別人底下）
    if "grp" in allowed:
        gid = str(allowed["grp"] or "")
        allowed["grp"] = gid if gid and any(g["id"] == gid for g in store.list_groups()) else None
    for k in ("pinned", "archived"):
        if k in allowed:
            allowed[k] = 1 if allowed[k] else 0
    if "perm" in allowed:
        from . import perms
        allowed["perm"] = allowed["perm"] if allowed["perm"] in perms.MODES else perms.settings()["default_mode"]
        reset_allow_all(sid)
    s = store.update_session(sid, **allowed)
    if s is None:
        raise HTTPException(404)
    await broadcast({"type": "session_updated", "session": s})
    return s


async def _purge_sessions(ids: list[str]) -> int:
    """把這幾個對話（含子代理）整個刪掉：先停掉在跑的東西、關程序、清還原點。"""
    from woof.agent import tasks as _tasks

    for x in list(ids):
        for cid in store.child_ids(x):
            if cid not in ids:
                ids.append(cid)
    for x in ids:
        t = RUNS.get(x)
        if t and not t.done():
            t.cancel()
        procs.stop_session_tasks(x, include_background=True)
        for tid in [k for k, v in procs.TASKS.items() if v.session_id == x]:
            procs.TASKS.pop(tid, None)
        _tasks.stop_goal(x)
    from . import pty, snapshot
    for x in ids:
        pty.close_session(x)
    from . import media, uploads
    for x in ids:
        s = store.get_session(x)
        pics = uploads.files_of(x)      # 要在刪訊息之前問，刪完就查不到誰在用
        store.delete_session(x)
        uploads.remove(pics)
        media.drop(x)              # 這個對話生成的圖片影片（成品另存在 save_to／scope 的不受影響）
        snapshot.drop(x)
        if s:
            await REG.events.emit("on_session", "delete", s)
    return len(ids)


@app.get("/api/retention")
async def retention_stats():
    """設定 → 一般 → 暫存與紀錄：現在有多少暫存、用量紀錄，保留天數。"""
    from . import retention

    return await asyncio.to_thread(retention.stats)


@app.post("/api/retention/purge")
async def retention_purge():
    """一鍵清除：暫存與用量紀錄全刪、重整資料庫。對話本身不動。"""
    from . import retention

    log = logging.getLogger("woof")
    log.info("一鍵清除：開始")
    try:
        r = await asyncio.to_thread(retention.purge_all, set())
    except Exception:
        log.exception("一鍵清除失敗")
        raise
    log.info("一鍵清除：暫存 %d 個（%.1f MB）、用量紀錄 %d 筆、已刪對話殘留 %d 筆；資料庫 %.1f → %.1f MB%s",
             r["files"], r["bytes"] / 1e6, r["usage_rows"], r["orphan_rows"], r["db_before"] / 1e6, r["db_after"] / 1e6,
             f"；重整沒做成：{r['vacuum_error']}" if r["vacuum_error"] else "")
    return r


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    # 刪主對話時，底下的子代理對話（含更深一層）一起刪
    return {"ok": True, "deleted": await _purge_sessions([sid])}


@app.post("/api/sessions/{sid}/fork")
async def fork_session(sid: str, body: dict):
    src = store.get_session(sid)
    if src is None:
        raise HTTPException(404)
    s = store.fork_session(sid, int(body.get("upto") or 0), f"{src['title']}（分支）")
    await REG.events.emit("on_session", "create", s)
    await broadcast({"type": "session_updated", "session": s})
    return s


@app.get("/api/search")
async def search_all(q: str = "", limit: int = 40, sid: str | None = None):
    """跨對話全文檢索（側邊欄搜尋用）：每個對話只留最相關的幾則。"""
    hits = store.search_messages(q, limit=max(1, min(limit, 200)) * 3, sid=sid)
    per: dict[str, int] = {}
    out = []
    for h in hits:
        if per.get(h["session_id"], 0) >= 3:
            continue
        per[h["session_id"]] = per.get(h["session_id"], 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


@app.get("/api/sessions/{sid}/messages")
async def messages(sid: str):
    from . import pet

    pet.watching(sid)
    return store.get_messages(sid)


@app.get("/api/sessions/{sid}/export")
async def export(sid: str):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    lines = [f"# {s['title']}\n"]
    for m in store.get_messages(sid):
        for b in m["content"]:
            if b["type"] == "text":
                lines.append(f"**{'你' if m['role'] == 'user' else 'WOOF'}**：\n\n{b['text']}\n")
            elif b["type"] == "tool_call":
                lines.append(f"> 工具 `{b['name']}`")
    return {"markdown": "\n".join(lines)}


AUX_TASKS = ("fallback", "vision", "compact", "subagent", "review")


@app.get("/api/tools")
async def list_tools():
    from woof.agent.loop import tool_cost
    off = set(REG.config.get("disabled_tools") or [])
    specs = {t["name"]: t for t in REG.tool_specs()}
    out = []
    for t in sorted(REG.tools.values(), key=lambda t: (t.plugin, t.name)):
        if getattr(t, "bridge", False):
            continue  # 工具延遲載入的橋接工具，由插件自己決定送不送
        sp = specs.get(t.name) or {}
        chars, tok = tool_cost(t.name, t.description, sp.get("schema"))
        out.append({"name": t.name, "plugin": t.plugin, "description": t.description,
                    "enabled": t.name not in off, "chars": chars, "tokens_est": tok})
    return out


@app.post("/api/tools")
async def set_tools(body: dict):
    """body: {"disabled": [...]}；只改 disabled_tools，立即生效。"""
    cfg = load_config()
    cfg["disabled_tools"] = sorted({str(x) for x in body.get("disabled") or [] if x})
    save_config(cfg)
    REG.config["disabled_tools"] = cfg["disabled_tools"]
    await broadcast({"type": "state_changed"})
    return await list_tools()


def _flow_now() -> dict:
    from woof.agent.tasks import flow_settings

    return flow_settings()


@app.get("/api/settings")
async def get_settings():
    from . import config as _cfgmod, dpapi

    cfg = load_config()
    keys = []
    for pool, entries in load_keys().items():
        for e in entries:
            k = e["key"]
            keys.append({"pool": pool, "label": e.get("label") or "",
                         "hint": (k[:6] + "…" + k[-4:]) if len(k) > 12 else "…"})
    for pk, why in _cfgmod.KEY_ERRORS.items():
        pool, _, label = pk.partition("/")
        keys.append({"pool": pool, "label": label, "hint": "", "error": why})
    return {
        "workdir": cfg.get("workdir") or "",
        "terminal_shell": cfg.get("terminal_shell") or "auto",
        "api_retries": _api_retries_cfg(cfg),
        "first_timeout": _clamp_int(cfg.get("first_timeout", 60), 0, 3600, 60),
        "idle_timeout": _clamp_int(cfg.get("idle_timeout", 120), 0, 3600, 120),
        "cmd_timeout": _cmd_timeout(),
        "allowed_dirs": cfg.get("allowed_dirs") or [],
        "vendors": cfg.get("vendors") or [],
        "discovery": await vendor_discovery(),
        "prompts": {"mode": ((cfg.get("prompts") or {}).get("mode") or "normal")},
        "flow": _flow_now(),
        "models": cfg.get("models") or [],
        "keys": keys,
        "backends": sorted(set(REG.providers) | set(REG.engines)),
        "aux": cfg.get("aux") or {},
        "default_model": cfg.get("default_model") or "",
        "disabled_tools": cfg.get("disabled_tools") or [],
        "redact_secrets": cfg.get("redact_secrets", True) is not False,
        "keys_encrypted": dpapi.available(),
        "cost_on": bool((cfg.get("budget") or {}).get("enabled")),
    }


def _read_keys_raw() -> dict:
    import tomllib

    from .config import KEYS_PATH

    try:
        return tomllib.loads(KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _clamp_int(v, lo: int, hi: int, dflt: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return dflt


def _api_retries_cfg(cfg: dict) -> int:
    return _clamp_int(cfg.get("api_retries", 3), 0, 10, 3)


CMD_TIMEOUT_MAX = 10_000_000   # 指令逾時不設上限（超過只是轉背景，不會卡住對話）；這個數只防亂填


def _cmd_timeout() -> int:
    p = REG.plugins.get("terminal")
    s = (getattr(p, "settings", None) or {}) if p else {}
    return _clamp_int(s.get("default_timeout", 180), 10, CMD_TIMEOUT_MAX, 180)


@app.post("/api/settings")
async def put_settings(body: dict):
    cfg = load_config()
    dropped: list[str] = []
    if "workdir" in body:
        cfg["workdir"] = str(body["workdir"]).strip()
    if "redact_secrets" in body:
        cfg["redact_secrets"] = bool(body["redact_secrets"])
    if "terminal_shell" in body:
        sh = str(body["terminal_shell"] or "auto")
        cfg["terminal_shell"] = sh if sh in ("auto", "bash", "powershell", "pwsh", "cmd") else "auto"
    if "api_retries" in body:
        cfg["api_retries"] = _clamp_int(body["api_retries"], 0, 10, 3)
    if "first_timeout" in body:   # 等模型回應的上限（0＝不限），見 loop.wait_limits
        cfg["first_timeout"] = _clamp_int(body["first_timeout"], 0, 3600, 60)
    if "idle_timeout" in body:
        cfg["idle_timeout"] = _clamp_int(body["idle_timeout"], 0, 3600, 120)
    if "cmd_timeout" in body:
        v = _clamp_int(body["cmd_timeout"], 10, CMD_TIMEOUT_MAX, 180)
        if v != _cmd_timeout():   # 存在 terminal 插件的設定裡（插件頁那格也是它），記憶體裡的同一份一起改，馬上生效
            cur = dict((store.get_plugin_state("terminal") or {}).get("settings") or {})
            cur["default_timeout"] = v
            store.set_plugin_state("terminal", settings=cur)
            p = REG.plugins.get("terminal")
            if p is not None and isinstance(getattr(p, "settings", None), dict):
                p.settings["default_timeout"] = v
    if "allowed_dirs" in body:
        cfg["allowed_dirs"] = [str(d).strip() for d in body["allowed_dirs"] if str(d).strip()]
    if "vendors" in body:
        vendors = []
        for v in body["vendors"]:
            if not isinstance(v, dict) or not v.get("id"):
                continue
            vendors.append({k: x for k, x in v.items() if x not in ("", None, [])})
        cfg["vendors"] = vendors
    if "models" in body:
        models = []
        for m in body["models"]:
            if not isinstance(m, dict) or not m.get("id") or m.get("auto"):
                continue
            models.append({k: v for k, v in m.items() if v not in ("", None, [])})
        ids = [m["id"] for m in models]
        dups = sorted({x for x in ids if ids.count(x) > 1})
        if dups:
            raise HTTPException(400, f"模型代號重複：{', '.join(dups)}")
        vids = {v.get("id") for v in cfg.get("vendors", [])}
        bad = sorted({m["vendor"] for m in models if m.get("vendor") and m["vendor"] not in vids})
        if bad:
            raise HTTPException(400, f"模型的模型商不存在：{', '.join(bad)}（請先在模型商加入，或刪掉這些模型）")
        cfg["models"] = models
    if isinstance(body.get("flow"), dict):
        from woof.agent.tasks import FLOW_DEFAULTS, FLOW_LIMITS

        fl = {}
        for k, (lo, hi) in FLOW_LIMITS.items():
            try:
                fl[k] = max(lo, min(hi, int(body["flow"].get(k, FLOW_DEFAULTS[k]))))
            except (TypeError, ValueError):
                fl[k] = FLOW_DEFAULTS[k]
        fl["goal_gate"] = bool(body["flow"].get("goal_gate", True))
        ids = {m.get("id") for m in (cfg.get("models") or [])}
        for k in ("expand_model", "review_model"):
            v = str(body["flow"].get(k) or "")
            if v and v in ids:
                fl[k] = v
        cfg["flow"] = fl
    if "prompts" in body:
        from woof.prompts import MODES
        mode = str((body.get("prompts") or {}).get("mode") or "normal")
        cfg["prompts"] = {"mode": mode if mode in MODES else "normal"}
    if "aux" in body and isinstance(body["aux"], dict):
        cfg["aux"] = {k: str(v) for k, v in body["aux"].items() if k in AUX_TASKS and v}
    if "default_model" in body:
        dv = str(body.get("default_model") or "").strip()
        if dv:
            if dv not in {m.get("id") for m in cfg.get("models", [])}:
                raise HTTPException(400, f"預設模型不存在：{dv}")
            cfg["default_model"] = dv
        else:
            cfg.pop("default_model", None)
    # 輔助模型指到已刪除的模型 → 清掉該項指派，並回報給前端
    live = {m.get("id") for m in cfg.get("models", [])}
    for task in list((cfg.get("aux") or {}).keys()):
        if cfg["aux"][task] not in live:
            dropped.append(f"{task}（原指向 {cfg['aux'][task]}）")
            del cfg["aux"][task]
    if "disabled_tools" in body:
        cfg["disabled_tools"] = sorted({str(x) for x in body["disabled_tools"] if x})
    save_config(cfg)

    if "keys" in body:
        existing = load_keys()
        pools: dict[str, list[dict]] = {}
        for item in body["keys"]:
            pool = (item.get("pool") or "").strip()
            if not pool:
                continue
            key = item.get("key") or ""
            if not key:
                # 沒重填就沿用同一池、同一標籤的舊值；解不開的（換過電腦）也原樣留著，不偷偷刪掉
                want = item.get("orig_label") or item.get("label")
                old = next((e for e in existing.get(pool, []) if e.get("label") == want), None)
                if old:
                    key = old["key"]
                else:
                    raw = _read_keys_raw().get(pool) or []
                    key = next((str(e.get("key") or "") for e in raw if isinstance(e, dict) and e.get("label") == want), "")
            if key:
                pools.setdefault(pool, []).append({"label": item.get("label") or "主要", "key": key})
        save_keys(pools)

    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "dropped_aux": dropped}


def _plugin_rows() -> list[dict]:
    rows = []
    for p in REG.plugins.values():
        d = p.public()
        d["path"] = str(p.path)
        d["provides"] = {
            "tools": sorted(t.name for t in REG.tools.values() if t.plugin == p.name),
            "backends": sorted([k for k, v in REG.providers.items() if getattr(v, "_woof_plugin", None) == p.name]
                               + [k for k, v in REG.engines.items() if getattr(v, "_woof_plugin", None) == p.name]),
            "hooks": sorted({h.name for hs in REG.events.hooks.values() for h in hs if h.plugin == p.name}),
        }
        rows.append(d)
    return rows


@app.get("/api/skills")
async def skills_list():
    """Skills 分區：列出所有 skill 與勾選狀態（預設全部關閉）。"""
    lister = getattr(REG, "skills_lister", None)
    if lister is None:
        return {"dirs": [], "skills": [], "enabled": [], "plugin_loaded": False}
    return {**lister(), "plugin_loaded": True}


@app.put("/api/skills/enabled")
async def skills_set_enabled(body: dict):
    """寫回已勾選的 skill 清單（存到 skills 插件設定 enabled_skills，逗號分隔）。"""
    names = body.get("enabled") or []
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        raise HTTPException(400, "enabled 必須是字串陣列")
    cur = store.get_plugin_state("skills") or {"enabled": None, "settings": {}}
    settings = dict(cur.get("settings") or {})
    settings["enabled_skills"] = ",".join(sorted({x.strip() for x in names if x.strip()}))
    store.set_plugin_state("skills", settings=settings)
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "enabled": sorted({x.strip() for x in names if x.strip()})}


@app.get("/api/plugins")
async def plugins():
    return _plugin_rows()


@app.post("/api/plugins/reload")
async def reload_plugins():
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return _plugin_rows()


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _toml_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


@app.post("/api/plugins/create")
async def create_plugin(body: dict):
    name = str(body.get("name") or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "名稱只能用英文小寫、數字、底線，且以英文開頭")
    dest = PLUGINS / name
    if dest.exists():
        raise HTTPException(400, f"plugins/{name} 已經存在")
    kinds = [k for k in body.get("kinds") or ["tool"] if k in ("tool", "provider", "engine", "hook")] or ["tool"]
    shutil.copytree(PLUGINS / "_template", dest)
    (dest / "plugin.toml").write_text(
        f"name = {_toml_str(name)}\nversion = \"0.1.0\"\nkinds = [{', '.join(_toml_str(k) for k in kinds)}]\n"
        f"description = {_toml_str(body.get('description') or '新插件')}\nenabled = true\n",
        encoding="utf-8")
    init = dest / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace('"hello",', f'"{name}_hello",', 1), encoding="utf-8")
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "name": name, "path": str(dest)}


def _find_manifest(root: Path) -> Path | None:
    if (root / "plugin.toml").is_file():
        return root
    subs = [d for d in root.iterdir() if d.is_dir() and (d / "plugin.toml").is_file()]
    return subs[0] if len(subs) == 1 else None


@app.post("/api/plugins/install")
async def install_plugin(body: dict):
    src = Path(str(body.get("path") or "").strip().strip('"'))
    if not src.exists():
        raise HTTPException(400, "找不到這個路徑")
    tmp = None
    try:
        if src.is_file():
            if src.suffix.lower() != ".zip":
                raise HTTPException(400, "只能安裝資料夾或 .zip")
            tmp = DATA / "tmp" / uuid.uuid4().hex[:8]
            tmp.mkdir(parents=True)
            with zipfile.ZipFile(src) as z:
                for n in z.namelist():
                    if n.startswith("/") or ".." in Path(n).parts:
                        raise HTTPException(400, "壓縮檔內含不安全的路徑")
                z.extractall(tmp)
            root = _find_manifest(tmp)
        else:
            root = _find_manifest(src)
        if root is None:
            raise HTTPException(400, "裡面找不到 plugin.toml")
        try:
            manifest = tomllib.loads((root / "plugin.toml").read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(400, f"plugin.toml 格式錯誤：{e}") from e
        name = str(manifest.get("name") or root.name).strip()
        if not _NAME_RE.match(name):
            raise HTTPException(400, f"插件名稱「{name}」不合規則（英文小寫、數字、底線）")
        if not (root / "__init__.py").is_file():
            raise HTTPException(400, "缺少 __init__.py")
        dest = PLUGINS / name
        if dest.exists():
            raise HTTPException(400, f"已經有同名插件 {name}，先移除舊的再裝")
        shutil.copytree(root, dest, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    REG.load_all()
    await broadcast({"type": "state_changed"})
    info = REG.plugins.get(name)
    return {"ok": True, "name": name, "error": info.error if info else None}


@app.delete("/api/plugins/{name}")
async def remove_plugin(name: str):
    info = REG.plugins.get(name)
    if info is None:
        raise HTTPException(404)
    trash = DATA / "trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{info.path.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    REG.unload_all()
    shutil.move(str(info.path), str(dest))
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "trash": str(dest)}


# ---------------------------------------------------------------- 模型商連線測試
@app.post("/api/vendors/probe")
async def probe_vendor(body: dict):
    """測試連線並取得模型清單。key 留空時用金鑰池裡的第一把。"""
    provider = body.get("provider")
    if provider == "claude_cli":
        path = shutil.which("claude")
        return {"ok": bool(path), "models": [], "message": f"找到 {path}" if path else "找不到 claude 指令，請先安裝 Claude Code CLI"}
    key = (body.get("key") or "").strip()
    if not key and body.get("key_pool"):
        entries = load_keys().get(body["key_pool"]) or []
        key = entries[0]["key"] if entries else ""
    if provider == "anthropic":
        url = (body.get("base_url") or "https://api.anthropic.com").rstrip("/") + "/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        base = (body.get("base_url") or "").rstrip("/")
        if not base:
            raise HTTPException(400, "請先填 API 位址")
        url = base + "/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
    except Exception as e:
        return {"ok": False, "models": [], "message": f"連不上 {url}：{type(e).__name__} {e}"}
    ms = int((time.time() - t0) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "models": [], "status": r.status_code,
                "message": f"HTTP {r.status_code}：{r.text[:300]}"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "models": [], "message": "回傳的不是 JSON，位址可能填錯"}
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None:
        items = data.get("models")
    models = []
    for it in items or []:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                models.append({"id": str(mid), "name": str(it.get("display_name") or it.get("name") or mid),
                               "context": it.get("context_length") or it.get("context_window"),
                               # 目錄沒寫輸入類型就是 None（不知道＝預設會看圖），有寫才判斷
                               "vision": (lambda mods: ("image" in str(mods)) if mods else None)(
                                   (it.get("architecture") or {}).get("input_modalities") or it.get("input_modalities"))})
        elif isinstance(it, str):
            models.append({"id": it, "name": it})
    return {"ok": True, "models": models, "message": f"連線成功 · {len(models)} 個模型 · {ms}ms"}


async def _discover_all(force: bool) -> None:
    """開啟時自動檢索：沒有金鑰的商略過，不會紅字。只更新快取。"""
    from . import discover

    for v in REG.vendors():
        if not discover.wants_auto(v) or not discover.has_key(v):
            continue
        c = discover.read_cache(v["id"])
        if not force and c.get("items") and time.time() - float(c.get("at") or 0) < discover.TTL:
            continue
        await discover.refresh(v)
    await broadcast({"type": "state_changed"})


_MCP: dict = {"running": False, "error": "", "port": 0}


async def _mcp_run(sid: str, text: str) -> str:
    """MCP 的 send_message：跑完整一回合，回傳助手最後的文字。"""
    if not store.get_session(sid):
        raise ValueError("找不到對話")
    if sid in RUNS and not RUNS[sid].done():
        raise ValueError("這個對話還在執行中，稍後再試")
    async with TURN_LOCK:
        RUNS[sid] = asyncio.create_task(_turn(sid, [{"type": "text", "text": text}]))
    await RUNS[sid]
    last = next((m for m in reversed(store.get_messages(sid)) if m["role"] == "assistant"), None)
    return "".join(b.get("text", "") for b in (last or {}).get("content", []) if b.get("type") == "text")


@app.on_event("shutdown")
async def _close_ptys():
    from . import pty
    from .agent import live

    live.SHUTTING = True   # 之後被收掉的回合、方案、子代理都不算結束，下次開程式接回
    pty.close_all()


@app.on_event("startup")
async def _start_mcp():
    from . import mcp_server

    mc = load_config().get("mcp") or {}
    if mc.get("enabled") is False:
        _MCP["error"] = "已在 config.toml 關閉 MCP"
        return
    port = int(mc.get("port") or mcp_server.DEFAULT_PORT)
    _MCP["port"] = port
    app_mcp = mcp_server.asgi_app(REG, _mcp_run)
    asyncio.create_task(mcp_server.serve(app_mcp, port, _MCP))


@app.get("/api/mcp/info")
async def mcp_info():
    from . import mcp_server

    return {"url": f"http://127.0.0.1:{_MCP['port']}/mcp", "token": mcp_server.token(),
            "running": bool(_MCP.get("running")), "error": _MCP.get("error") or ""}


@app.on_event("startup")
async def _auto_discover_on_start():
    asyncio.create_task(_recover_runs())   # 接回上次沒做完的（含排隊訊息、背景指令通知）
    asyncio.create_task(_discover_all(force=False))
    asyncio.create_task(_watch_bg_tasks())
    asyncio.create_task(cron.loop(_cron_run, broadcast))
    asyncio.create_task(_snapshot_housekeeping())


async def _snapshot_housekeeping():
    """開程式後整理一次檔案快照（照設定的保留天數與容量上限），之後每 6 小時一次。"""
    from . import snapshot

    while True:
        try:
            live = {s["id"] for s in store.list_sessions()}
            await asyncio.to_thread(snapshot.prune, live)
            from . import uploads

            n, size = await asyncio.to_thread(uploads.gc)
            if n:
                logging.getLogger("woof").info("回收沒人用的貼圖 %d 張（%.1f MB）", n, size / 1e6)
        except Exception:
            logging.getLogger("woof").exception("整理快照失敗")
        try:
            from . import retention

            r = await asyncio.to_thread(retention.prune, {s["id"] for s in store.list_sessions()})
            if r["files"] or r["usage_rows"] or r["orphan_rows"]:
                logging.getLogger("woof").info("清掉過期暫存 %d 個（%.1f MB）、用量紀錄 %d 筆、已刪對話殘留 %d 筆",
                                               r["files"], r["bytes"] / 1e6, r["usage_rows"], r["orphan_rows"])
        except Exception:
            logging.getLogger("woof").exception("清理過期資料失敗")
        await asyncio.sleep(6 * 3600)


# ------------------------------------------------------------------ 排程工作
async def _cron_run(job: dict) -> tuple[str, str, str]:
    """排程到了：開一個新對話跑這段提示詞，回傳 (對話 id, 狀態, 錯誤)。"""
    model = job.get("model") or REG.default_model_id()
    cfg = REG.model_cfg(model) if model else None
    if model and not cfg:
        return "", "error", f"找不到模型 {model}"
    effort = job.get("effort") or (cfg or {}).get("default_effort")
    stamp = time.strftime("%m/%d %H:%M")
    s = store.create_session(model or (cfg or {}).get("id"), effort, f"⏱ {job['name']} · {stamp}")
    # 排程沒人在旁邊：要「先問」的工具照權限設定的「無人值守」處理（預設擋下）
    s = store.update_session(s["id"], engine_state={**(s.get("engine_state") or {}), "headless": True}) or s
    await REG.events.emit("on_session", "create", s)
    await broadcast({"type": "session_updated", "session": s})
    sid = s["id"]
    async with TURN_LOCK:
        task = RUNS[sid] = asyncio.create_task(_turn(sid, [{"type": "text", "text": job["prompt"]}]))
    await cron.set_session(job["id"], sid)
    await asyncio.wait({task})
    status = "stopped" if task.cancelled() else (task.result() if not task.exception() else "error")
    err = ""
    if status == "error":
        err = "執行失敗，詳情在對話裡"
    return sid, status or "done", err


@app.get("/api/cron")
async def cron_list():
    return {"jobs": [cron.public(j) for j in cron.load()], "presets": cron.PRESETS}


@app.post("/api/cron/preview")
async def cron_preview(body: dict):
    expr = " ".join(str(cron.PRESETS.get(body.get("schedule"), body.get("schedule")) or "").split())
    try:
        t, nxt = time.time(), []
        for _ in range(3):
            t = cron.next_after(expr, t)
            nxt.append(t)
        return {"ok": True, "label": cron.describe(expr), "next": nxt}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/cron")
async def cron_create(body: dict):
    try:
        return cron.public(await cron.add(body))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/cron/{jid}")
async def cron_update(jid: str, body: dict):
    try:
        j = await cron.edit(jid, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not j:
        raise HTTPException(404, "找不到排程")
    return cron.public(j)


@app.delete("/api/cron/{jid}")
async def cron_delete(jid: str):
    if not await cron.remove(jid):
        raise HTTPException(404, "找不到排程")
    return {"ok": True}


@app.post("/api/cron/{jid}/run")
async def cron_run(jid: str):
    if not any(j["id"] == jid for j in cron.load()):
        raise HTTPException(404, "找不到排程")
    if jid in cron.RUNNING:
        raise HTTPException(409, "這個排程正在執行")
    asyncio.create_task(cron.run_now(jid, "manual"))
    return {"ok": True}


@app.get("/api/ui-prefs")
async def ui_prefs_get():
    """用瀏覽器開 WOOF 時讀介面偏好（桌面版由 Electron 主程序直接讀檔）。"""
    from woof import ui_prefs
    return ui_prefs.read()


@app.put("/api/ui-prefs")
async def ui_prefs_put(body: dict):
    from woof import ui_prefs
    return ui_prefs.write(body.get("values") or {})


@app.post("/api/cron/{jid}/stop")
async def cron_stop(jid: str):
    sid = cron.RUN_SID.get(jid)
    if not sid:
        raise HTTPException(409, "這個排程目前沒在執行")
    await _stop_session(sid)
    return {"ok": True}


@app.get("/api/cron/{jid}/runs")
async def cron_runs(jid: str):
    """執行記錄（最新在前）：每筆帶對話標題，對話被刪掉的標 gone。"""
    h = cron.history(jid)
    if h is None:
        raise HTTPException(404, "找不到排程")
    for r in h:
        ss = store.get_session(r["session"]) if r.get("session") else None
        r["title"] = (ss or {}).get("title")
        r["gone"] = bool(r.get("session")) and not ss
    return {"runs": h}


@app.get("/api/vendors/discovery")
async def vendor_discovery():
    from . import discover

    out = {}
    for v in REG.vendors():
        if discover.wants_auto(v):
            c = discover.read_cache(v["id"])
            out[v["id"]] = {"count": len(c.get("items") or []), "at": c.get("at") or 0,
                            "error": c.get("error") or "", "has_key": discover.has_key(v)}
    return out


@app.post("/api/vendors/{vid}/refresh")
async def refresh_vendor(vid: str):
    """重新檢索：只更新快取，不改設定。要加哪些模型由使用者勾選（/pick）。"""
    from . import discover

    v = next((x for x in REG.vendors() if x["id"] == vid), None)
    if v is None:
        raise HTTPException(404)
    if not discover.wants_auto(v):
        raise HTTPException(400, "這個模型商不做自動檢索")
    return await discover.refresh(v)


@app.get("/api/vendors/{vid}/discovered")
async def vendor_discovered(vid: str):
    """快取裡檢索到的模型清單，附上 config 裡已加入的。"""
    from . import discover

    c = discover.read_cache(vid)
    have = {m.get("model") for m in (load_config().get("models") or []) if m.get("vendor") == vid}
    items = [{**it, "added": it["model"] in have} for it in (c.get("items") or [])]
    return {"at": c.get("at") or 0, "error": c.get("error") or "", "items": items}


@app.post("/api/vendors/{vid}/pick")
async def vendor_pick(vid: str, body: dict):
    """把勾選的模型寫進 config.toml。"""
    from . import discover

    names = [str(x) for x in (body.get("models") or [])]
    if not names:
        raise HTTPException(400, "還沒勾選模型")
    added, skipped = discover.pick(vid, names)
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "added": added, "skipped": skipped}


@app.get("/api/internal_prompts")
async def internal_prompts():
    """WOOF 自己寫給模型的內部提示詞（審核、接續、壓縮…），設定頁檢視、覆寫用。"""
    from woof import prompts

    return prompts.catalog()


@app.put("/api/internal_prompts/{key}")
async def internal_prompt_set(key: str, body: dict):
    from woof import prompts

    try:
        prompts.set_override(key, body.get("text"))
    except KeyError:
        raise HTTPException(404, f"沒有這段提示詞：{key}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/internal_prompts/{key}")
async def internal_prompt_reset(key: str):
    from woof import prompts

    try:
        prompts.set_override(key, None)
    except KeyError:
        raise HTTPException(404, f"沒有這段提示詞：{key}")
    return {"ok": True}


@app.get("/api/model_caps")
async def model_caps_list():
    from woof import caps

    return caps._load()


@app.delete("/api/model_caps/{model_id:path}")
async def model_caps_forget(model_id: str):
    """清掉自動偵測記住的能力（不能看圖、不收思考參數…），下次送出重新偵測。"""
    from woof import caps

    caps.forget(None if model_id == "*" else model_id)
    REG.refresh_discovered()
    await broadcast({"type": "state_changed"})
    return {"ok": True}


@app.post("/api/plugins/{name}/act/{action}")
async def plugin_act(name: str, action: str, body: dict):
    """插件前端頁面呼叫插件後端（插件用 woof.add_action 註冊）。"""
    fn = REG.actions.get((name, action))
    if fn is None:
        raise HTTPException(404, f"插件 {name} 沒有「{action}」（插件沒開或載入失敗）")
    r = await fn(body or {})
    return r if isinstance(r, dict) else {"result": r}


@app.get("/api/plugins/{name}/web/{path:path}")
async def plugin_web(name: str, path: str):
    """插件 web/ 資料夾裡的前端檔（plugin.toml 的 [ui] script、style 指到這裡）。"""
    info = REG.plugins.get(name)
    if not info or not info.loaded:
        raise HTTPException(404)
    root = (info.path / "web").resolve()
    f = (root / path).resolve()
    if not f.is_relative_to(root) or not f.is_file():
        raise HTTPException(404)
    mime = {".js": "text/javascript", ".css": "text/css"}.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0]
    return FileResponse(f, media_type=mime or "application/octet-stream", headers={"cache-control": "no-cache"})


@app.post("/api/plugins/{name}")
async def update_plugin(name: str, body: dict):
    if name not in REG.plugins:
        raise HTTPException(404)
    store.set_plugin_state(name, enabled=body.get("enabled"), settings=body.get("settings"))
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return REG.plugins[name].public()


@app.get("/api/tasks")
async def tasks():
    return [t.info() for t in procs.TASKS.values()]


@app.post("/api/tasks/{tid}/stop")
async def stop_task(tid: str):
    return {"ok": procs.stop_task(tid)}


def _allowed(path: Path) -> bool:
    from . import workspace
    return workspace.allowed(path)


# ---------------------------------------------------------------- 工作目錄、檔案樹、預覽
TEXT_MAX = 1_048_576


def _sess_wd(sid: str | None) -> str:
    from . import workspace
    return workspace.of_session(store.get_session(sid) if sid else None)


@app.get("/api/workspace")
async def workspace_info(session_id: str = ""):
    from . import workspace
    s = store.get_session(session_id) if session_id else None
    own = ((s or {}).get("engine_state") or {}).get("workdir") or ""
    return {"workdir": workspace.of_session(s), "own": own, "default": workspace.default_workdir(),
            "own_missing": bool(own) and not Path(own).is_dir()}


_ABS_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/[a-z]/)[^\s`\"'<>|*?()（），。、；：「」]+")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist-electron"}


def _resolve_rel(workdir: str, rel: str, known: list[str]) -> str | None:
    """回覆裡只寫了檔名（`pelican-plane.svg`）：找出它實際在哪。
    依序看工作目錄、這個對話工具用過的路徑（寫過的檔、cd 過的資料夾）、工作目錄往下 3 層。"""
    rel = rel.strip().replace("\\", "/").lstrip("./")
    if not rel or ".." in rel.split("/"):
        return None
    base = Path(workdir) if workdir else None
    if base and (base / rel).is_file():
        return str(base / rel).replace("\\", "/")
    low = "/" + rel.lower()
    for p in reversed(known):   # 越後面越新
        q = p.replace("\\", "/").rstrip("/")
        if q.lower().endswith(low) and Path(q).is_file():
            return q
        for d in (q, q.rsplit("/", 1)[0]):
            # 只用夠深的資料夾（C:/、C:/Users 這種太廣，會撿到不相干的同名檔）
            if len(Path(d).parts) >= 4 and (Path(d) / rel).is_file():
                return (d + "/" + rel)
    if base and base.is_dir():
        # 往下找只認唯一一個：README.md、index.html 這種每個專案都有的，猜一個反而開錯檔
        hits, seen = [], 0
        for root, dirs, files in os.walk(base):
            depth = len(Path(root).relative_to(base).parts)
            dirs[:] = [] if depth >= 3 else [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            seen += len(files)
            if seen > 20000:
                break
            cand = Path(root) / rel
            if cand.is_file():
                hits.append(str(cand).replace("\\", "/"))
                if len(hits) > 1:
                    return None
        if hits:
            return hits[0]
    return None


@app.post("/api/sessions/{sid}/resolve")
async def resolve_paths(sid: str, body: dict):
    """回覆裡的相對檔名換成實際路徑（點連結、開資料夾、用預設程式開都要真的路徑）。找不到回 null。"""
    import json

    from . import workspace

    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    rels = [str(r) for r in (body.get("rels") or []) if str(r).strip()][:60]
    known: list[str] = []
    for m in store.get_messages(sid):
        if m["role"] not in ("assistant", "tool"):
            continue
        for b in m.get("content") or []:
            t = b.get("type")
            src = json.dumps(b.get("args") or {}, ensure_ascii=False) if t == "tool_call" else \
                str(b.get("output") or "") if t == "tool_result" else ""
            if src:
                known += [x.rstrip(".,;:!?)]}\\") for x in _ABS_RE.findall(src.replace("\\\\", "/"))]
    known = [("%s:/%s" % (k[1].upper(), k[3:]) if re.match(r"/[a-z]/", k) else k) for k in known][-3000:]
    wd = workspace.of_session(s)
    return {"paths": {r: await asyncio.to_thread(_resolve_rel, wd, r, known) for r in rels}}


@app.put("/api/sessions/{sid}/workdir")
async def set_session_workdir(sid: str, body: dict):
    s = store.get_session(sid)
    if s is None:
        raise HTTPException(404)
    path = str(body.get("path") or "").strip().strip('"')
    if path:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"資料夾不存在：{path}")
        path = str(p.resolve())
    es = dict(s.get("engine_state") or {})
    if path:
        es["workdir"] = path
    else:
        es.pop("workdir", None)
    s = store.update_session(sid, engine_state=es)
    await broadcast({"type": "session_updated", "session": s})
    return await workspace_info(sid)


@app.get("/api/fs/list")
async def fs_list(path: str, top: str = "", ignored: int = 0):
    from . import workspace
    p = Path(path)
    if not _allowed(p):
        raise HTTPException(403, "這個資料夾不在工作目錄或允許清單內")
    try:
        return await asyncio.to_thread(workspace.list_dir, path, top or None, bool(ignored))
    except FileNotFoundError:
        raise HTTPException(404, "資料夾不存在")
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.post("/api/fs/stat")
async def fs_stat(body: dict):
    """產物面板用：一次查多個路徑（相對路徑以對話的工作目錄為準）。"""
    wd = Path(_sess_wd(body.get("session_id")))
    out = []
    for raw in (body.get("paths") or [])[:500]:
        p = Path(os.path.expanduser(str(raw)))
        if not p.is_absolute():
            p = wd / p
        try:
            st = p.stat()
            out.append({"req": raw, "path": str(p), "exists": p.is_file(), "dir": p.is_dir(), "size": st.st_size,
                        "mtime": st.st_mtime, "allowed": _allowed(p)})
        except OSError:
            out.append({"req": raw, "path": str(p), "exists": False, "size": 0, "mtime": 0, "allowed": False})
    return out


@app.get("/api/fs/read")
async def fs_read(path: str):
    """預覽用：文字檔回傳內容（最多 1 MB），二進位只回傳類型。"""
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "檔案不存在")
    if not _allowed(p):
        raise HTTPException(403, "這個檔案不在工作目錄或允許清單內")
    size = p.stat().st_size
    mime = mimetypes.guess_type(p.name)[0] or ""
    with p.open("rb") as f:
        head = f.read(TEXT_MAX)
    binary = b"\x00" in head[:8192]
    text = ""
    if not binary:
        for enc in ("utf-8", "utf-16", "cp950"):
            try:
                text = head.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = head.decode("utf-8", errors="replace")
    return {"path": str(p), "size": size, "mime": mime, "binary": binary, "text": text,
            "truncated": size > TEXT_MAX, "mtime": p.stat().st_mtime}


# ---------------------------------------------------------------- 權限
@app.get("/api/permissions")
async def perm_info():
    from . import perms
    return perms.public()


@app.post("/api/permissions")
async def perm_save(body: dict):
    from . import perms
    cfg = load_config()
    cur = dict(cfg.get("permissions") or {})
    if "custom" in body:
        cur["custom"] = {k: v for k, v in (body["custom"] or {}).items() if k in perms.CATS and v in perms.DECISIONS}
    if body.get("default_mode") in perms.MODES:
        cur["default_mode"] = body["default_mode"]
    if "headless" in body:
        cur["headless"] = "approve" if body["headless"] == "approve" else "deny"
    if "ask_timeout" in body:
        cur["ask_timeout"] = perms._ask_timeout(body["ask_timeout"])
    for k in ("allow_cmds", "deny_cmds"):
        if k in body:
            cur[k] = [str(x).strip() for x in body[k] or [] if str(x).strip()][:200]
    cfg["permissions"] = cur
    save_config(cfg)
    REG.config["permissions"] = cur
    return perms.public()


@app.post("/api/permissions/check")
async def perm_check(body: dict):
    """設定頁的「試試看」：這個模式下，這個工具／指令會怎樣。"""
    from . import perms
    name = str(body.get("tool") or "terminal")
    args = body.get("args") or ({"command": body.get("command")} if body.get("command") else {})
    return perms.decide(str(body.get("mode") or "auto"), name, args, bool(body.get("headless")))


# ---------------------------------------------------------------- 終端機面板
@app.get("/api/pty")
async def pty_list(session_id: str = ""):
    from . import pty
    return {"terms": [t.info() for t in pty.TERMS.values() if t.session_id == session_id],
            "shells": pty.available_shells(), "default_shell": REG.config.get("terminal_shell") or "auto"}


@app.post("/api/pty")
async def pty_create(body: dict):
    from . import pty
    sid = str(body.get("session_id") or "")
    cwd = str(body.get("cwd") or "") or _sess_wd(sid)
    shell = str(body.get("shell") or REG.config.get("terminal_shell") or "auto")
    try:
        t = pty.create(sid, cwd, shell, int(body.get("cols") or 100), int(body.get("rows") or 28))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"開不了終端機：{e}")
    return t.info()


@app.delete("/api/pty/{tid}")
async def pty_close(tid: str):
    from . import pty
    return {"ok": pty.close(tid)}


@app.websocket("/ws/pty/{tid}")
async def pty_ws(ws: WebSocket, tid: str):
    from . import pty
    if not remote.check(bool(remote.state()["open_mode"]), "/ws", ws.query_params.get("token"), TOKEN):
        await ws.close(code=4401)
        return
    t = pty.TERMS.get(tid)
    if t is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    if t.buf:
        # \x01 開頭＝補播舊輸出：前端播放時不回應裡面的終端查詢，免得回應被當成鍵盤輸入
        await ws.send_text("\x01" + t.buf)
    t.listeners.add(q)

    async def pump():
        while True:
            data = await q.get()
            if data is None:
                await ws.close()
                return
            # 把排隊的輸出合併成一包，大量輸出時不會一個字一個字送
            while not q.empty():
                nxt = q.get_nowait()
                if nxt is None:
                    await ws.send_text(data)
                    await ws.close()
                    return
                data += nxt
            await ws.send_text(data)

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                s = msg["text"]
                if s.startswith("\x00{"):
                    try:
                        c = __import__("json").loads(s[1:])
                        if c.get("resize"):
                            pty.resize(tid, *c["resize"])
                    except Exception:
                        pass
                else:
                    pty.write(tid, s)
    except WebSocketDisconnect:
        pass
    finally:
        t.listeners.discard(q)
        task.cancel()


@app.get("/api/file")
async def get_file(path: str):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "檔案不存在")
    if not _allowed(p):
        raise HTTPException(403, "這個資料夾不在允許清單內")
    return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")


# ---- HTML 預覽：iframe 載真正的網址，頁面裡的相對路徑（圖片、js、css）才找得到，大檔也不會被截斷
# 網址裡放的是預覽金鑰，不是 WOOF 的 token：頁面的 js 看得到自己的網址，金鑰只能讀允許資料夾裡的檔，不能呼叫 API
_PV: dict[str, tuple[Path, float]] = {}
_PV_MIME = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json",
            ".svg": "image/svg+xml", ".wasm": "application/wasm", ".html": "text/html", ".htm": "text/html"}


@app.get("/api/fs/page")
async def page_url(path: str):
    import secrets

    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "檔案不存在")
    if not _allowed(p):
        raise HTTPException(403, "這個資料夾不在允許清單內")
    now = time.time()
    for k in [k for k, (_, exp) in _PV.items() if exp < now] + list(_PV)[:-63]:
        _PV.pop(k, None)
    key = secrets.token_urlsafe(16)
    _PV[key] = (p.resolve().parent, now + 12 * 3600)
    return {"url": f"/pv/{key}/" + p.resolve().as_posix()}


@app.get("/pv/{key}/{rest:path}")
async def page_file(key: str, rest: str):
    root, exp = _PV.get(key, (None, 0))
    if root is None or exp < time.time():
        raise HTTPException(404, "預覽已過期，重新打開預覽")
    p = Path(rest)
    if not p.is_absolute() or not p.is_file() or not _allowed(p):
        raise HTTPException(404, "找不到")
    # 頁面在 sandbox 裡（來源是 null）：同資料夾內的檔放行跨來源讀取，fetch、模組 js 才能用；資料夾外的只能當圖片、樣式載入
    hdr = {"Cache-Control": "no-store"}
    if p.resolve().is_relative_to(root):
        hdr["Access-Control-Allow-Origin"] = "*"
    mime = _PV_MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mime, headers=hdr)


@app.post("/api/paste")
async def save_paste(body: dict):
    """貼上的長文存成檔案（跟貼圖同一條生命週期：對話刪掉一起刪）。"""
    from . import uploads

    text = str(body.get("text") or "")
    if not text:
        raise HTTPException(400, "沒有內容")
    p = await asyncio.to_thread(uploads.save_paste, text)
    return {"path": str(p), "name": p.name, "bytes": len(text.encode("utf-8"))}


# ---------------------------------------------------------------- WebSocket
def _save_files(items: list[dict]) -> list[dict]:
    """附加檔案（不是圖）：原檔存進 data/files，訊息裡留位置給模型自己讀。"""
    blocks = []
    for it in items or []:
        raw = it.get("data") or ""
        if not raw:
            continue
        name = str(it.get("name") or "附件")
        ext = Path(name).suffix[:10] or mimetypes.guess_extension(it.get("media_type") or "") or ".bin"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,9}", ext):
            ext = ".bin"
        try:
            data = base64.b64decode(raw)
        except Exception:
            continue
        path = FILES / f"{uuid.uuid4().hex[:12]}{ext}"
        path.write_bytes(data)
        blocks.append({"type": "file", "path": str(path), "name": name, "bytes": len(data)})
    return blocks


def _file_notes(blocks: list[dict]) -> list[dict]:
    """把檔案位置寫成一行人看得懂、模型也讀得懂的說明。"""
    out = []
    for b in blocks:
        if b.get("type") != "file":
            continue
        kb = b.get("bytes") or 0
        size = f"{kb / 1048576:.1f} MB" if kb >= 1048576 else f"{max(1, round(kb / 1024))} KB"
        out.append({"type": "text", "text": f"（附件：{b['name']}（{size}），已存成檔案：{b['path']}；"
                                            "需要內容時用 read_file 讀，二進位檔用 terminal 轉文字）"})
    return out


def _save_images(images: list[dict]) -> list[dict]:
    blocks = []
    for im in images or []:
        mt = im.get("media_type") or "image/png"
        ext = mimetypes.guess_extension(mt) or ".png"
        path = UPLOADS / f"{uuid.uuid4().hex[:12]}{ext}"
        path.write_bytes(base64.b64decode(im["data"]))
        blocks.append({"type": "image", "path": str(path), "media_type": mt})
    return blocks


async def _drain_steer(sid: str) -> None:
    while await flush_steer(sid, broadcast):
        await resume_session(sid, broadcast)


def _media_session(sid: str) -> bool:
    """對話模型是生圖／影片模型：沒有工具鏈可接續，出錯就停，重試在 media 模組裡做。"""
    from woof.media import kind_of

    return kind_of(REG.model_cfg((store.get_session(sid) or {}).get("model"))) != "chat"


async def _close_or_resume(sid: str, status: str, resume) -> None:
    async with TURN_LOCK:
        more = pop_steer(sid) if status == "done" else []
        if more:
            requeue_steer(sid, more)
            RUNS[sid] = asyncio.create_task(resume(sid))
            return
        RUNS.pop(sid, None)
    await broadcast({"type": "run_end", "session_id": sid, "status": status})


# ---------------------------------------------------------------- 接續（做到一半停下時）
AUTO_RESUMED: dict[str, int] = {}
_TRANSIENT = ("timeout", "timed out", "connection", "connect", "remote protocol", "server disconnected",
              "overloaded", "rate limit", "429", "500", "502", "503", "504", "529", "temporarily",
              "unavailable", "reset by peer", "eof", "incomplete chunked", "stream")


def _cont_setting(key: str, dflt):
    info = REG.plugins.get("continuation")
    if info is None or not info.enabled:
        return None
    v = (info.settings or {}).get(key)
    return dflt if v is None or v == "" else v


def _is_transient(e: Exception) -> bool:
    from .agent.loop import is_overflow_error
    s = f"{type(e).__name__} {e}".lower()
    if is_overflow_error(e) or any(x in s for x in ("401", "403", "invalid api key", "unauthorized", "permission")):
        return False
    return any(x in s for x in _TRANSIENT)


async def _note_interrupted(sid: str, reason: str, kind: str) -> None:
    s2 = mark_interrupted(sid, reason, kind)
    if s2:
        await broadcast({"type": "session_updated", "session": s2})




async def _continue_session(sid: str, reason: str) -> None:
    from .agent import tasks as _tasks
    from .agent.internal import meta as _imeta

    await flush_steer(sid, broadcast)   # 中斷前排隊的插話先寫進對話，接續訊息放在最後
    text = _prompt_get("resume", reason=reason)
    try:
        digest = _tasks.resume_digest(sid)
    except Exception:
        log.exception("組接續摘要失敗")
        digest = ""
    if digest:
        text = text.rstrip() + "\n\n" + digest
    m = store.add_message(sid, "user", [{"type": "text", "text": text}], _imeta("resume"))
    await broadcast({"type": "message", "session_id": sid, "message": m})
    await resume_session(sid, broadcast)
    await asyncio.sleep(0)
    await _drain_steer(sid)


async def _guarded(sid: str, body, label: str) -> str:
    """跑一段回合；出錯時記下中斷原因，連線類錯誤自動接續（次數見接續插件設定）。
    回合記在磁碟上：程式重開（含當掉、強制關閉）後自動接回；做完、你按停止、出錯才拿掉。"""
    from .agent import live

    live.mark(sid, "turn")
    try:
        return await _guarded_body(sid, body, label)
    finally:
        live.unmark(sid, "turn")


async def _guarded_body(sid: str, body, label: str) -> str:
    from .agent import live

    status = "done"
    while True:
        try:
            await body()
            break
        except asyncio.CancelledError:
            status = "stopped"
            if live.SHUTTING:
                break   # 程式關閉途中被收掉：不是你按停止，下次開程式自動接回
            await flush_steer(sid, broadcast)
            await _note_interrupted(sid, "你按了停止", "stopped")
            break
        except Exception as e:
            log.exception(label)
            err = f"{type(e).__name__}: {e}"
            await broadcast({"type": "error", "session_id": sid, "text": err})
            limit = int(_cont_setting("auto_resume", 1) or 0)
            if _is_transient(e) and AUTO_RESUMED.get(sid, 0) < limit and not _media_session(sid):
                AUTO_RESUMED[sid] = AUTO_RESUMED.get(sid, 0) + 1
                await broadcast({"type": "notice", "session_id": sid,
                                 "text": f"連線或伺服器出錯，5 秒後自動接續（第 {AUTO_RESUMED[sid]}/{limit} 次）…"})
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    status = "stopped"
                    break
                reason = "連線或伺服器錯誤：" + str(e)[:120]
                body = (lambda r=reason: _continue_session(sid, r))
                continue
            status = "error"
            await _note_interrupted(sid, "出錯：" + str(e)[:160], "error")
            break
    return status


async def _turn(sid: str, blocks: list[dict]) -> None:
    await broadcast({"type": "run_start", "session_id": sid})
    AUTO_RESUMED.pop(sid, None)

    async def body():
        await run_turn(sid, blocks, broadcast)
        await asyncio.sleep(0)
        await _drain_steer(sid)

    status = await _guarded(sid, body, "執行失敗")
    await _close_or_resume(sid, status, _resume_turn)
    asyncio.create_task(_pet_after(sid))
    return status


async def _pet_after(sid: str) -> None:
    """一回合結束後，讓寵物看要不要講一句。壞了不影響對話。"""
    from . import pet

    try:
        await pet.after_turn(sid, broadcast)
    except Exception:
        pass


async def _resume_turn(sid: str) -> None:
    status = await _guarded(sid, lambda: _drain_steer(sid), "接續失敗")
    await _close_or_resume(sid, status, _resume_turn)


async def _after_flow(sid: str) -> None:
    """方案／目標流程做完時還有沒送進去的插話（驗收那段送的、或最後一步剛結束才送到）：接著開一回合處理，不能就這樣不見"""
    async with TURN_LOCK:
        if (not peek_steer(sid) or (sid in RUNS and not RUNS[sid].done()) or _flow_running(sid)
                or sub_alive(sid) or sid in _RECOVERING):
            return   # 子代理自己那條在跑：它下一步就會看到，不另開一回合
        RUNS[sid] = asyncio.create_task(_steer_turn(sid))


def _flow_running(sid: str) -> bool:
    from .agent import tasks as _tasks

    return _tasks.is_running(sid)


async def _steer_turn(sid: str) -> None:
    await broadcast({"type": "run_start", "session_id": sid})
    await _resume_turn(sid)


async def _gated_send(sid: str, blocks: list[dict], text: str, gate: str, extra: str = "") -> bool:
    """方案模式下的一則要求：存進對話，交給方案流程（拓展／審核 → 照做 → 驗收）。成功開始回 True。"""
    from .agent import tasks as _tasks

    m = store.add_message(sid, "user", blocks)
    await broadcast({"type": "message", "session_id": sid, "message": m})
    s_new = store.auto_title(sid, text)
    if s_new:
        await broadcast({"type": "session_updated", "session": s_new})
    try:
        _tasks.start_gated(sid, text, broadcast, gate, extra=extra)
        return True
    except RuntimeError as e:
        await broadcast({"type": "error", "session_id": sid, "text": str(e)})
        return False


async def _continue_turn(sid: str, reason: str) -> None:
    await broadcast({"type": "run_start", "session_id": sid})
    status = await _guarded(sid, lambda: _continue_session(sid, reason), "接續失敗")
    await _close_or_resume(sid, status, _resume_turn)


async def inject_internal(sid: str, kind: str, text: str) -> None:
    """系統事件送回對話（例如背景指令結束）：閒置就開新的一步讓模型處理；執行中就排隊，這一步結束後送進去。"""
    from .agent.internal import meta as _imeta
    if not store.get_session(sid):
        return
    blocks = [{"type": "text", "text": text}]
    from .agent import tasks as _tasks

    async with TURN_LOCK:
        if (sid in RUNS and not RUNS[sid].done()) or _tasks.is_running(sid) or sid in _RECOVERING:
            # 方案流程在跑、或開程式正要接回這個對話：排隊，不另開一回合搶跑
            push_steer(sid, blocks, _imeta(kind))
            await broadcast({"type": "queue", "session_id": sid,
                             "items": [{"text": it["blocks"][0].get("text", "") if it["blocks"] else "",
                                        "internal": (it.get("meta") or {}).get("internal")} for it in peek_steer(sid)]})
            return
        m = store.add_message(sid, "user", blocks, _imeta(kind))
        await broadcast({"type": "message", "session_id": sid, "message": m})
        RUNS[sid] = asyncio.create_task(_inject_turn(sid))


async def _inject_turn(sid: str) -> None:
    await broadcast({"type": "run_start", "session_id": sid})

    async def body():
        await resume_session(sid, broadcast)
        await asyncio.sleep(0)
        await _drain_steer(sid)

    status = await _guarded(sid, body, "背景通知回合失敗")
    await _close_or_resume(sid, status, _resume_turn)


REG.inject = inject_internal


_RECOVERING: set[str] = set()   # 開程式時等著接回的對話：這段時間的子代理回報、背景通知先排隊，接回後一起處理


async def _tell_parent(child_id: str, bg: dict, why: str) -> None:
    """子代理沒接回：一定要告訴派它的對話，不然主模型會一直等一個不會來的回報。"""
    parent = bg.get("parent") or ""
    if not parent or not store.get_session(parent):
        return
    title = str(bg.get("title") or (store.get_session(child_id) or {}).get("title") or "")[:40]
    await inject_internal(parent, "bg_done",
                          f"子代理 {child_id}（{title}）程式重開後沒有接回：{why}。它的任務還沒做完，不會再回報。"
                          "要它接著做，帶同一個 session_id 呼叫 delegate_task；不需要了就不用管。")


async def _recover_procs() -> None:
    """上次開著的背景指令（要通知對話的）在程式重開時跟著被結束了：照實告訴各自的對話。"""
    from . import procs

    for tid, r in procs.take_pending().items():
        sid = r.get("session_id") or ""
        if not sid or not store.get_session(sid):
            continue
        try:
            out = Path(r.get("log") or "").read_text(encoding="utf-8", errors="replace")[-3000:].strip()
        except Exception:
            out = ""
        secs = int(time.time() - float(r.get("started") or time.time()))
        text = _prompt_get("bg_done", id=tid, state="程式重開時被一起結束，沒跑完",
                           command=str(r.get("command") or "")[:300], output=out or "(無輸出)",
                           elapsed=f"{secs // 60} 分 {secs % 60} 秒")
        await inject_internal(sid, "bg_done", text)


async def _recover_runs() -> None:
    """開程式時接回上次沒做完的：先接回背景子代理（主對話接回時要知道它們還在跑，驗收前也要等它們回報），
    再接回方案流程和回合。接回期間的子代理回報、背景通知先排隊，等對話接回後一起送進去，不另開一回合搶在前面。
    沒接回的子代理一定告訴派它的對話；上次沒送出的排隊訊息，對話沒在跑就開一回合處理，不會被晾著。
    同一個工作連續接回好幾次都沒有進度（可能每次都讓程式當掉），就不再自動接，留「繼續」給你按。"""
    from .agent import live
    from .agent import tasks as _tasks
    from .agent.loop import BG_RESERVED, resume_bg, restore_steer

    async def _stable(sid: str) -> None:
        # 接回後撐過 2 分鐘沒當掉：計數歸零。上限只擋「一接回就讓程式當掉」的循環，不擋正常的長工作
        await asyncio.sleep(120)
        live.progress(sid)

    lg = logging.getLogger("woof")
    queued = restore_steer()   # 上次重開前還在排隊的插話／子代理回報放回佇列（照原本順序）
    recs = live.take()
    # 子代理同時記著 bg 跟 turn 時只走子代理那條（帶著寫入範圍接回），不再當一般回合重複接一次
    for r in recs.values():
        if r.get("bg"):
            r.pop("turn", None)
            r.pop("flow", None)
    _RECOVERING.update(sid for sid, r in recs.items() if r.get("flow") or r.get("turn"))
    # 要接回的子代理先佔著名額：接回途中主對話或你新派的，不會因為還沒算到它們而超過上限
    for sid, r in recs.items():
        if r.get("bg") and int(r.get("n") or 0) < live.MAX_RECOVER:
            BG_RESERVED[sid] = r["bg"].get("root") or r["bg"].get("parent") or ""
    await asyncio.sleep(1)   # 等其他開程式的工作（插件、模型清單）就位
    reason = "程式重開"
    try:
        for sid, rec in recs.items():
            if not rec.get("bg"):
                continue
            n = int(rec.get("n") or 0)
            if not store.get_session(sid):
                live.drop(sid)
                continue
            why = ""
            if n >= live.MAX_RECOVER:
                why = f"已經連續自動接回 {n} 次都沒做完，不再自動接"
                lg.warning("子代理 %s %s", sid, why)
                await _note_interrupted(sid, why, "error")
            else:
                live.remark(sid, {"bg": rec["bg"], "n": n})
                asyncio.create_task(_stable(sid))
                try:
                    if not await resume_bg(sid, rec["bg"], broadcast):
                        why = "派它的對話或它用的模型已經不在了"
                except Exception as e:
                    lg.exception("接回子代理 %s 失敗", sid)
                    why = f"接回時出錯（{type(e).__name__}: {str(e)[:120]}）"
            BG_RESERVED.pop(sid, None)   # 接回了就改由它自己的任務佔名額；沒接回就空出來
            if why:
                live.drop(sid, "bg")
                await _tell_parent(sid, rec["bg"], why)
        for sid, rec in recs.items():
            if not (rec.get("flow") or rec.get("turn")):
                continue
            if not store.get_session(sid):
                live.drop(sid)
                continue
            n = int(rec.get("n") or 0)
            if n >= live.MAX_RECOVER:
                lg.warning("對話 %s 重開後已自動接回 %s 次都沒做完，不再自動接", sid, n)
                live.drop(sid, "flow", "turn")
                await _note_interrupted(sid, f"重開後自動接回 {n} 次都沒做完", "error")
                continue
            async with TURN_LOCK:
                _RECOVERING.discard(sid)
                if (sid in RUNS and not RUNS[sid].done()) or _tasks.is_running(sid):
                    continue   # 你在開程式後已經送了新的要求：那一輪自己管紀錄
                if rec.get("flow"):
                    if _tasks.resume_goal(sid, broadcast, reason):
                        live.remark(sid, {"flow": True, "n": n})
                        asyncio.create_task(_stable(sid))
                        await broadcast({"type": "notice", "session_id": sid, "text": "程式重開：自動接回方案"})
                        continue
                    if not rec.get("turn"):
                        live.drop(sid, "flow")
                        continue   # 方案已經結束、或之後你下了新指令：沒有可接的，不另外開回合
                live.remark(sid, {"turn": True, "n": n})
                asyncio.create_task(_stable(sid))
                RUNS[sid] = asyncio.create_task(_continue_turn(sid, reason))
                await broadcast({"type": "notice", "session_id": sid, "text": "程式重開：自動接著做"})
    finally:
        for sid in recs:
            BG_RESERVED.pop(sid, None)
        left = set(_RECOVERING)
        _RECOVERING.clear()
    try:
        await _recover_procs()
    except Exception:
        lg.exception("通知重開前的背景指令失敗")
    # 排隊的訊息（上次沒送出的、接回期間到的回報）：對話沒在跑就開一回合處理
    for sid in set(queued) | left:
        if peek_steer(sid):
            await _after_flow(sid)


def _hook_after_flow() -> None:
    from .agent import tasks as _tasks

    _tasks.AFTER_FLOW = _after_flow


_hook_after_flow()


@app.get("/api/mcp")
async def mcp_list():
    rows = []
    for part in await REG.events.collect("mcp_status"):
        if isinstance(part, list):
            rows.extend(part)
    return rows


@app.get("/api/mcp/servers")
async def mcp_servers_get():
    info = REG.plugins.get("mcp_client")
    return {"servers": (info.settings.get("servers") if info else "") or ""}


@app.put("/api/mcp/servers")
async def mcp_servers_put(body: dict):
    """整段寫回 servers（一行一台：名稱 | 指令 參數 或 名稱 | http://… | 標頭:值）。先驗證格式與重名。"""
    info = REG.plugins.get("mcp_client")
    if info is None:
        raise HTTPException(404)
    lines = [ln.rstrip() for ln in str(body.get("servers") or "").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise HTTPException(400, f"格式錯誤：{ln}")
        if parts[0] in names:
            raise HTTPException(400, f"名稱重複：{parts[0]}")
        names.add(parts[0])
    settings = {**info.settings, "servers": "\n".join(lines)}
    store.set_plugin_state("mcp_client", settings=settings)
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "count": len(lines)}


@app.post("/api/mcp/{name}")
async def mcp_toggle(name: str, body: dict):
    info = REG.plugins.get("mcp_client")
    if info is None:
        raise HTTPException(404)
    disabled = {x.strip() for x in str(info.settings.get("disabled") or "").split(",") if x.strip()}
    if body.get("enabled"):
        disabled.discard(name)
    else:
        disabled.add(name)
    settings = {**info.settings, "disabled": ",".join(sorted(disabled))}
    store.set_plugin_state("mcp_client", settings=settings)
    REG.load_all()
    await broadcast({"type": "state_changed"})
    return {"ok": True, "disabled": settings["disabled"]}


@app.post("/api/asks/{aid}")
async def answer_ask(aid: str, body: dict):
    from woof.agent import tasks

    if isinstance(body.get("answers"), dict):   # 多題卡片：{qid: 文字或陣列}
        return {"ok": tasks.resolve_ask(aid, {str(k): v for k, v in body["answers"].items()})}
    return {"ok": tasks.resolve_ask(aid, str(body.get("answer") or "").strip() or "（空白）")}


@app.post("/api/asks/{aid}/touch")
async def touch_ask(aid: str):
    """使用者在問題卡片上打字：期限重新算。"""
    from woof.agent import tasks

    dl = tasks.touch_ask(aid)
    return {"ok": dl >= 0, "deadline": max(dl, 0)}


@app.post("/api/asks/{aid}/hold")
async def hold_ask(aid: str):
    """使用者點了問題卡片的輸入框：停止倒數，等他慢慢想。"""
    from woof.agent import tasks

    return {"ok": tasks.hold_ask(aid)}


@app.get("/api/sessions/{sid}/tasks")
async def session_tasks(sid: str):
    st = (store.get_session(sid) or {}).get("engine_state") or {}
    return {"todos": st.get("todos") or [], "goal": st.get("goal") or None, "plan": bool(st.get("plan")), "gate": st.get("gate") or "off"}


@app.post("/api/sessions/{sid}/todos/cancel")
async def cancel_todo_ep(sid: str, body: dict):
    """使用者在畫面上取消（cancel=true）或恢復（false）一項 TODO。
    模型正在做事時排一則通知，這一步結束就看到：取消的停手、不要做；恢復的照常做。閒置時不另開回合。"""
    from woof.agent import tasks
    from woof.agent.internal import meta as _imeta

    if not store.get_session(sid):
        raise HTTPException(404)
    text = str(body.get("text") or "")
    cancel = bool(body.get("cancel", True))
    try:
        idx = int(body.get("index", -1))
    except (TypeError, ValueError):
        idx = -1
    items, old = tasks.cancel_todo(sid, idx, text, cancel)
    if not old:
        raise HTTPException(404, "找不到這項 TODO（可能剛被模型改掉）")
    await broadcast({"type": "todos", "session_id": sid, "items": items})
    changed = old in ("pending", "doing", "failed") if cancel else old == "cancelled"
    if changed and ((sid in RUNS and not RUNS[sid].done()) or tasks.is_running(sid)):
        msg = (f"使用者在畫面上取消了 TODO「{text}」：這項不要做了，正在做就停手，接著做其他項目；不要改回。"
               if cancel else f"使用者在畫面上恢復了 TODO「{text}」：這項照常要做。")
        push_steer(sid, [{"type": "text", "text": msg}], _imeta("todo_cancel"))
        await broadcast({"type": "queue", "session_id": sid,
                         "items": [{"text": it["blocks"][0].get("text", "") if it["blocks"] else "",
                                    "internal": (it.get("meta") or {}).get("internal")} for it in peek_steer(sid)]})
    return {"ok": True, "items": items}


@app.post("/api/sessions/{sid}/ctx_window")
async def set_ctx_window(sid: str, body: dict):
    """這個對話自己的上下文上限（token）：壓縮門檻和用量圈都照它算。0＝照「設定 → 上下文壓縮」。
    比模型本身的上下文窗口大時以模型為準。下一次送出前檢查，超過門檻就先壓縮。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    try:
        n = max(0, int(body.get("window") or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "window 要是整數（0＝照設定）")
    if 0 < n < 8000:
        raise HTTPException(400, "上限至少 8000 token")
    st = dict(s.get("engine_state") or {})
    if n:
        st["ctx_window"] = n
    else:
        st.pop("ctx_window", None)
    store.update_session(sid, engine_state=st)
    from woof.tokens import window_of

    return {"ok": True, "window": n, "effective": window_of(REG.model_cfg(s.get("model")), sid)}


@app.post("/api/sessions/{sid}/img_tok")
async def set_img_tok(sid: str, body: dict):
    """這個對話每張圖最多幾 token（200～4784）。0＝照「設定 → 上下文壓縮 → 圖片」。下一次送出起生效。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    try:
        n = max(0, int(body.get("tok") or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "tok 要是整數（0＝照設定）")
    if n:
        n = max(200, min(n, 4784))
    st = dict(s.get("engine_state") or {})
    if n:
        st["img_tok"] = n
    else:
        st.pop("img_tok", None)
    store.update_session(sid, engine_state=st)
    return {"ok": True, "tok": n}


@app.post("/api/sessions/{sid}/goal")
async def start_goal_ep(sid: str, body: dict):
    from woof.agent import tasks

    if not store.get_session(sid):
        raise HTTPException(404)
    obj = str(body.get("objective") or "").strip()
    if not obj:
        raise HTTPException(400, "目標不能是空的")
    try:
        tasks.start_goal(sid, obj, broadcast)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/sessions/{sid}/gate")
async def set_gate(sid: str, body: dict):
    """先提方案模式：off 關閉；human 人工核准；model 子代理審核；expand 先思維拓展再驗收。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    mode = str(body.get("mode") or "off")
    if mode not in ("off", "human", "model", "expand"):
        raise HTTPException(400, "mode 只能是 off、human、model、expand")
    st = dict(s.get("engine_state") or {})
    st["gate"] = mode
    store.update_session(sid, engine_state=st)
    return {"ok": True, "mode": mode}


@app.post("/api/sessions/{sid}/tools")
async def set_session_tool(sid: str, body: dict):
    """這個對話手動開／關一個工具，不動全域設定。body: {"name", "on", "when"}。
    開：全域停用的放進 tools_on（排在工具清單最後面）；這個對話關過的從 tools_off 拿掉；也會取消待關。
    關：when="compress" 等下次壓縮一起關（不多花）；when="now" 立刻關（下一則整段重算）。"""
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    name = str(body.get("name") or "")
    if name not in REG.tools:
        raise HTTPException(400, f"沒有工具 {name}")
    from woof.agent.loop import disabled_tools

    st = dict(s.get("engine_state") or {})
    on = list(st.get("tools_on") or [])
    off0 = list(st.get("tools_off") or [])
    off = [x for x in off0 if x != name]
    drop = [x for x in (st.get("tools_drop") or []) if x != name]
    add = [x for x in (st.get("tools_add") or []) if x != name]
    glob_off = name in disabled_tools()
    when = body.get("when")
    if body.get("on") and when == "compress":
        # 先用 tool_call、壓縮時正式加入：工具清單不動（快取不斷）
        off = off0
        if (glob_off and name not in on) or name in off0:
            add.append(name)
    elif body.get("on"):
        if glob_off and name not in on:
            on.append(name)
    elif when == "unadd":
        off = off0   # 取消「壓縮時加入」：其他不動
    elif when == "compress":
        drop.append(name)
    else:
        on = [x for x in on if x != name]
        if not glob_off:
            off.append(name)
    st["tools_on"], st["tools_off"], st["tools_drop"], st["tools_add"] = on, off, drop, add
    store.update_session(sid, engine_state=st)
    return {"ok": True, "tools_on": on, "tools_off": off, "tools_drop": drop, "tools_add": add}


@app.get("/api/media/spec")
async def media_spec(model: str):
    """生成模型能調的參數（比例、解析度、秒數）和目前的預設值，給「模型用途 → 生圖・生影片」顯示。"""
    from woof import media

    m = REG.model_cfg(model) or next((x for x in REG.config.get("models", []) if x["id"] == model), None)
    if not m:
        raise HTTPException(404)
    return media.spec(m)


@app.put("/api/media/gen/{mid}")
async def media_gen_set(mid: str, body: dict):
    """存某個生成模型的預設生成參數（models[].gen）。body: {"ratio", "size", "resolution", "seconds"}，空值＝用預設。"""
    from woof import media

    cfg = load_config()
    m = next((x for x in cfg.get("models", []) if x.get("id") == mid), None)
    if not m:
        raise HTTPException(404)
    gen = {k: body[k] for k in ("ratio", "size", "resolution", "seconds") if body.get(k) not in (None, "")}
    if gen:
        m["gen"] = gen
    else:
        m.pop("gen", None)
    save_config(cfg)
    live = next((x for x in REG.config.get("models", []) if x.get("id") == mid), None)
    if live is not None:
        if gen:
            live["gen"] = gen
        else:
            live.pop("gen", None)
    return media.spec(REG.model_cfg(mid) or live or m)


@app.post("/api/sessions/{sid}/goal/stop")
async def stop_goal_ep(sid: str):
    from woof.agent import tasks

    return {"ok": tasks.stop_goal(sid)}


@app.post("/api/sessions/{sid}/review")
async def review_ep(sid: str, body: dict | None = None):
    """scope=last（預設）：審最後一則回覆；scope=changes：審本對話改過的檔案目前內容。"""
    from woof.agent import tasks

    scope = (body or {}).get("scope") or "last"
    try:
        if scope == "changes":
            return await tasks.review_changes(sid, broadcast)
        return await tasks.review_last(sid, broadcast)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sessions/{sid}/plan")
async def set_plan(sid: str, body: dict):
    s = store.get_session(sid)
    if not s:
        raise HTTPException(404)
    st = dict(s.get("engine_state") or {})
    st["plan"] = bool(body.get("on"))
    s2 = store.update_session(sid, engine_state=st)
    await broadcast({"type": "session_updated", "session": s2})
    return {"ok": True, "plan": st["plan"]}


@app.post("/api/sessions/{sid}/compact")
async def compact_session(sid: str):
    session = store.get_session(sid)
    if session is None:
        raise HTTPException(404)
    model_cfg = REG.model_cfg(session.get("model"))
    if model_cfg is None:
        raise HTTPException(400, f"這個對話的模型「{session.get('model') or '（未設定）'}」不在模型清單裡，請重新選一個")

    class Ctx:
        pass

    ctx = Ctx()
    ctx.session_id = sid
    ctx.model_cfg = model_cfg
    ctx.registry = REG
    ctx.manual = True

    async def emit(ev):
        await broadcast(ev)

    ctx.emit = emit
    done = await REG.events.collect("force_compact", ctx)
    return {"ok": any(done), "reason": getattr(ctx, "compact_reason", ""), "count": getattr(ctx, "compact_count", 0)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not remote.check(bool(remote.state()["open_mode"]), "/ws", ws.query_params.get("token"), TOKEN):
        await ws.close(code=4401)
        return
    await ws.accept()
    CLIENTS.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            sid = msg.get("session_id")
            if t == "send" and sid:
                blocks = _save_images(msg.get("images") or [])
                fblocks = _save_files(msg.get("files") or [])
                blocks += fblocks
                blocks += _file_notes(fblocks)
                if (msg.get("text") or "").strip():
                    blocks.append({"type": "text", "text": msg["text"]})
                if not blocks:
                    continue
                _sm = (store.get_session(sid) or {}).get("model")
                if _sm and REG.model_cfg(_sm) is None:
                    # 對話的模型被刪或改名：不換模型硬跑，也不吃掉使用者的訊息
                    await ws.send_json({"type": "error", "session_id": sid,
                                        "text": f"這個對話的模型「{_sm}」已經不在模型清單裡（被刪除或改名），請在右下角重選一個模型再送"})
                    continue
                # 有提問卡在等（一般回合、方案、目標流程都一樣）：這一則訊息就是那一題的回答。
                # 不先結掉的話會走到「已有目標或方案在執行」被擋下，兩邊一起卡住。
                from woof.agent import tasks as _tasksq

                if _tasksq.has_ask(sid):
                    _tasksq.skip_ask(sid, (msg.get("text") or "").strip() or "（使用者直接回話，沒有文字內容）")
                    await broadcast({"type": "notice", "session_id": sid, "text": "已用你這則訊息回答剛才的提問"})
                    continue
                switch_from = None
                steer_note = "已插入，這一步結束後送給模型"
                async with TURN_LOCK:
                    from woof.agent import tasks as _tasks

                    running = sid in RUNS and not RUNS[sid].done()
                    agent_running = (not running) and sub_alive(sid)
                    # 方案／目標流程在跑（規劃、審核、照步驟做、驗收）：插話跟一般回合一樣排進去，不能丟掉或另開一回合搶跑
                    flow_running = (not running) and (not agent_running) and _tasks.is_running(sid)
                    gate = ((store.get_session(sid) or {}).get("engine_state") or {}).get("gate") or "off"

                    if (running and gate in ("human", "model", "expand") and (msg.get("text") or "").strip()
                            and not _tasks.is_running(sid)):
                        # 一般回合在跑、中途切成方案模式後送出要求：停下這一回合，這則交給方案流程，
                        # 不然會被當插話塞進一般回合，整段沒有拓展也沒有審核
                        steered = False
                        switch_from = RUNS[sid]
                        switch_from.cancel()
                    elif running or agent_running or flow_running:
                        # 有提問卡在等：使用者這次送訊息就當成回答，先把它結掉，
                        # 否則回合會卡在等那張卡（插話也要等這一步結束才送得進去）
                        from woof.agent import tasks as _tasksx

                        if _tasksx.skip_ask(sid):
                            await broadcast({"type": "notice", "session_id": sid, "text": "已用你這則訊息回答剛才的提問"})
                        push_steer(sid, blocks, {"steer": True})   # 標成插話：「繼續」時不當成新指令（方案照樣接得回來）
                        steered = True
                        if agent_running:
                            # 子代理還在自己那條對話裡跑：這一步跑完就會看到追加要求，不用重開
                            await broadcast({"type": "notice", "session_id": sid,
                                             "text": "已把追加要求送給子代理，它這一步跑完就會看到"})
                        elif flow_running:
                            steer_note = _tasks.steer_note(sid)
                    else:
                        steered = False
                        if gate in ("human", "model", "expand") and (msg.get("text") or "").strip():
                            await _gated_send(sid, blocks, msg["text"].strip(), gate)
                        else:
                            RUNS[sid] = asyncio.create_task(_turn(sid, blocks))
                if switch_from is not None:
                    await broadcast({"type": "notice", "session_id": sid,
                                     "text": "已切到方案模式：先停下目前這一回合，這則交給方案流程（規劃 → 審核 → 照做 → 驗收）"})
                    await asyncio.wait({switch_from}, timeout=30)
                    async with TURN_LOCK:
                        if sid in RUNS and not RUNS[sid].done():
                            await ws.send_json({"type": "error", "session_id": sid,
                                                "text": "上一回合停不下來，這則沒有送出，稍後再送一次"})
                            continue
                        from woof.agent.loop import clear_interrupted as _clr

                        text0 = msg["text"].strip()
                        note = _tasks.progress_note(sid, "你中途切到方案模式")
                        s2 = _clr(sid)
                        if s2:
                            await broadcast({"type": "session_updated", "session": s2})
                        await _gated_send(sid, blocks, text0, gate, note)
                if steered:
                    await broadcast({"type": "steer", "session_id": sid, "content": blocks})
                    await broadcast({"type": "notice", "session_id": sid, "text": steer_note})
            elif t == "continue" and sid:
                # 「繼續」：上一回合沒做完（出錯、停止、步數上限、程式關閉），請模型從中斷處接著做
                async with TURN_LOCK:
                    if (sid in RUNS and not RUNS[sid].done()) or sub_alive(sid):
                        await ws.send_json({"type": "error", "session_id": sid, "text": "還在執行中"})
                        continue
                    es = ((store.get_session(sid) or {}).get("engine_state") or {})
                    reason = str(msg.get("reason") or (es.get("interrupted") or {}).get("reason") or "程式關閉或連線中斷")
                    AUTO_RESUMED.pop(sid, None)
                    from woof.agent import tasks as _tasks

                    if _tasks.is_running(sid):
                        # 方案／目標本來就在跑（例如從還沒刷新的「繼續」列按的）：不用接，也不要報錯
                        await ws.send_json({"type": "notice", "session_id": sid, "text": "方案還在跑，不用按繼續"})
                        continue
                    if _tasks.resume_goal(sid, broadcast, reason):
                        # 方案做到一半被打斷：接回整個流程（做完中斷那步再往下），不是只續一回合就斷掉
                        continue
                    gate = es.get("gate") or "off"
                    if gate in ("human", "model", "expand"):
                        # 方案模式但沒有方案可接（這段是一般回合做的、或流程已結束）：重走方案流程，只排還沒做完的
                        try:
                            r = _tasks.continue_gated(sid, broadcast, gate, reason)
                        except RuntimeError as e:
                            await ws.send_json({"type": "error", "session_id": sid, "text": str(e)})
                            continue
                        if r == "started":
                            continue
                        if r == "cancelled":
                            await ws.send_json({"type": "notice", "session_id": sid,
                                                "text": "這個方案你按過取消，沒有自動做；要做就再送一次要求"})
                            continue
                    RUNS[sid] = asyncio.create_task(_continue_turn(sid, reason))
            elif t == "rerun" and sid:
                mid = int(msg.get("from_id") or 0)
                old = store.get_message(mid)
                if not old or old["session_id"] != sid or old["role"] != "user":
                    await ws.send_json({"type": "error", "session_id": sid, "text": "找不到要重送的訊息"})
                    continue
                blocks = [b for b in old["content"] if b.get("type") != "text"]
                text = msg.get("text")
                if text is None:
                    text = "\n".join(b.get("text", "") for b in old["content"] if b.get("type") == "text")
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                async with TURN_LOCK:
                    if sid in RUNS and not RUNS[sid].done():
                        await ws.send_json({"type": "error", "session_id": sid, "text": "還在執行中，先停止再重送"})
                        continue
                    from woof.agent import tasks as _tasks

                    gate = ((store.get_session(sid) or {}).get("engine_state") or {}).get("gate") or "off"
                    if gate in ("human", "model", "expand") and text.strip():
                        if _tasks.is_running(sid):
                            await ws.send_json({"type": "error", "session_id": sid, "text": "方案／目標還在跑，先停止再重送"})
                            continue
                        store.delete_from(sid, mid)
                        await broadcast({"type": "truncated", "session_id": sid, "from_id": mid})
                        await _gated_send(sid, blocks, text.strip(), gate)
                        continue
                    store.delete_from(sid, mid)
                    RUNS[sid] = asyncio.create_task(_turn(sid, blocks))
                await broadcast({"type": "truncated", "session_id": sid, "from_id": mid})
            elif t == "approve":
                resolve_approval(str(msg.get("id") or ""), str(msg.get("decision") or "deny"))
            elif t == "stop" and sid:
                await _stop_session(sid)
            elif t == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(ws)


async def _stop_session(sid: str) -> None:
    """停止對話正在跑的回合、背景指令、目標與外部引擎。"""
    task = RUNS.get(sid)
    if task and not task.done():
        task.cancel()
    procs.stop_session_tasks(sid)
    from woof.agent import loop as _loop
    from woof.agent import tasks as _tasks

    _tasks.stop_goal(sid)
    _loop.stop_agent(sid)   # 子代理對話：停掉它自己的背景任務
    for eng in REG.engines.values():
        stop = getattr(eng, "stop", None)
        if stop:
            await stop(sid)


def _index_html() -> str:
    """首頁：app.js、app.css 的 ?v= 自動換成檔案修改時間，改了檔不用手動改版號，也不會吃到舊快取。"""
    import re as _re

    html = (WEB_DIST / "index.html").read_text(encoding="utf-8")

    def ver(m):
        p = WEB_DIST / m.group(1)
        return f"{m.group(1)}?v={int(p.stat().st_mtime)}" if p.exists() else m.group(0)
    return _re.sub(r"([\w./-]+\.(?:js|css))\?v=\w+", ver, html)


def mount_static() -> None:
    if WEB_DIST.exists():
        from fastapi.responses import HTMLResponse

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def _index():
            return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache"})
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
