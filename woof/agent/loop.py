"""WOOF 工具迴圈：驅動「模型轉換器」插件，執行工具，直到模型給出最終回答。

自帶工具迴圈的引擎插件（engine）不走這裡，直接由 run_turn 交給引擎處理。

中立訊息格式（content 為區塊陣列）：
  {"type": "text", "text": str}
  {"type": "image", "path": str, "media_type": str}
  {"type": "thinking", "text": str, "signature"?: str}
  {"type": "tool_call", "id": str, "name": str, "args": dict}
  {"type": "tool_result", "id": str, "name": str, "output": str, "is_error": bool}
角色：user / assistant / tool
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import store
from ..config import DATA
from ..loader import REG
from ..prompts import get as _P
from .. import redact as _redact

Emit = Callable[[dict], Awaitable[None]]

MAX_STEPS = 0  # 0＝不限；continuation 插件可設上限
# 單次工具結果送進上下文的上限（字元）：頭 2/3、尾 1/4；超過的全文存 spill 檔，需要時用 read_file 讀
MAX_TOOL_CHARS = 12_000
SPILL = DATA / "spill"
SPILL.mkdir(exist_ok=True)


def _clean_spill(days: int = 30) -> None:
    """工具長輸出的全文存檔放 30 天就刪，資料夾不會無限長大。"""
    cut = time.time() - days * 86400
    for p in SPILL.glob("*.txt"):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
        except OSError:
            pass


_clean_spill()

TPE = datetime.now().astimezone().tzinfo  # 這台電腦的時區

from ..promptfiles import DEFAULT_IDENTITY as BASE_PROMPT, DEFAULT_IDENTITY_EN as BASE_PROMPT_EN


def base_prompt(lang: str | None = None) -> str:
    """身份與環境：設定 → 提示詞與記憶 → 身份與環境（data/prompts/IDENTITY.md）。
    檔案是空的才用程式裡的預設；介面語言是英文且沒改過內容，就用英文預設。"""
    from woof import promptfiles
    from woof.lang import ui_lang

    en = (lang or ui_lang()) == "en"
    try:
        txt = promptfiles.read("identity")
    except Exception:
        txt = ""
    if not txt.strip():
        return BASE_PROMPT_EN if en else BASE_PROMPT
    if en and txt.strip() == BASE_PROMPT.strip():
        return BASE_PROMPT_EN
    return txt


class ToolContext:
    def __init__(self, session_id: str, cwd: str, emit: Emit, call_id: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self._emit = emit
        self.call_id = call_id

    async def output(self, chunk: str) -> None:
        if _redact.enabled():
            chunk = _redact.redact(chunk)
        await self._emit({"type": "tool_output", "session_id": self.session_id, "id": self.call_id, "chunk": chunk})

    async def emit(self, event: dict) -> None:
        event.setdefault("session_id", self.session_id)
        await self._emit(event)


class TurnContext:
    def __init__(self, session: dict, model_cfg: dict, effort: str | None, emit: Emit) -> None:
        self.session = session
        self.session_id = session["id"]
        self.model_cfg = model_cfg
        # 使用者選的是想要的強度；依模型支援的檔位向下兼容（woof/effort.py）
        from woof.effort import resolve as _resolve_effort
        self.effort_want = effort
        self.effort = _resolve_effort(model_cfg.get("efforts"), effort, model_cfg.get("default_effort"))
        self.emit = emit
        # 對話自己設的工作目錄優先（woof/workspace.py），沒設就用預設工作目錄
        from woof.workspace import of_session as _wd_of
        self.cwd = _wd_of(session)
        self.registry = REG
        # 子代理用：權限跟著哪個對話、哪些工具不給用、額外的系統提示
        self.root_session_id = session["id"]
        self.tool_exclude: set[str] = set()
        # 這個對話手動開的工具（全域「設定中停用」的才算），送出時排在工具清單最後面
        self.tool_include: set[str] = set((session.get("engine_state") or {}).get("tools_on") or [])
        # 這個對話手動關的工具（全域開著的）
        self.tool_off: set[str] = set((session.get("engine_state") or {}).get("tools_off") or [])
        self.extra_system = ""
        # 寫入子代理可改的範圍（None＝不限；唯讀子代理用不到）
        self.write_scope: list | None = None
        # 這一回合的使用者訊息 id：工作區快照（woof/snapshot.py）用它當檢查點
        self.turn_mid: int = 0


async def build_parts(ctx: TurnContext) -> list[dict]:
    """系統提示詞拆成段落，送出與面板預覽共用。覆寫只取代「基礎提示詞」，插件片段一律保留。"""
    # 系統提示詞在對話第一次送出時固定（system_for），這裡的日期就是對話開始那天；精確時間用 current_time 工具
    now = datetime.now(TPE).strftime("%Y-%m-%d（%A，UTC%z）")
    ov = prompt_override(ctx.session)
    parts = [{"key": "base", "source": "基礎提示詞", "text": ov if ov is not None else base_prompt(),
              "overridden": ov is not None}]
    parts.append({"key": "env", "source": "系統", "text": f"{model_identity(ctx.model_cfg)}\n對話開始日期：{now}；現在的日期與時間請用 current_time 工具查詢。\n工作目錄：{ctx.cwd}（相對路徑以這裡為準）"})
    # 提示詞檔（人物檔、設定檔、使用者檔、記憶檔）是核心的，不掛在任何插件上：
    # 關掉某個插件不該讓它們跟著從系統提示消失
    from woof import promptfiles as _pf

    _pf_text = _pf.build_prompt_gated()
    if _pf_text:
        parts.append({"key": "promptfiles", "source": "提示詞檔", "text": _pf_text})
    for plugin, text in await REG.events.collect_named("build_prompt", ctx):
        if isinstance(text, str) and text.strip():
            parts.append({"key": "plugin", "source": plugin, "text": text})
    if ctx.extra_system:
        parts.append({"key": "mode", "source": "計畫模式／子代理", "text": ctx.extra_system})
    return parts


async def build_system(ctx: TurnContext) -> str:
    return "\n\n".join(p["text"] for p in await build_parts(ctx))


def preview_ctx(session: dict, model_cfg: dict | None) -> "TurnContext":
    """面板預覽用：套用和 run_turn 相同的對話設定（計畫模式、停用插件），但不跑 prepare_turn（會觸發壓縮與 MCP 連線）。"""
    async def _noop(ev):
        return None
    ctx = TurnContext(session, model_cfg or {}, None, _noop)
    es = session.get("engine_state") or {}
    if es.get("plan"):
        from woof.agent.tasks import PLAN_TOOLS
        from woof.prompts import get as _prompt
        ctx.tool_exclude |= PLAN_TOOLS
        ctx.extra_system = _prompt("plan_mode")
    off = set(es.get("plugins_off") or [])
    if off:
        ctx.tool_exclude |= {n for n, t in REG.tools.items() if t.plugin in off}
    return ctx


def tool_cost(name: str, description: str, schema) -> tuple[int, int]:
    """工具送給模型時的成本粗估：回傳 (字元數, 約略 token)。
    字元數＝名稱＋說明＋參數結構的 JSON。英文約 4 字元／token，中日韓字約 1.5 字元／token。"""
    txt = json.dumps({"name": name, "description": description, "parameters": schema}, ensure_ascii=False)
    asc = sum(1 for ch in txt if ord(ch) < 128)
    return len(txt), round(asc / 4 + (len(txt) - asc) / 1.5)


async def tool_specs_for(ctx: TurnContext) -> list[dict]:
    """這一步實際送給模型的工具：先排除停用的，再交給 tool_specs 掛鉤（例如工具延遲載入）。"""
    live = store.get_session(ctx.session_id) if getattr(ctx, "session_id", None) else None
    if live is not None:  # 回合中途壓縮時換掉的工具，這一步就照新的送（不等下一回合，免得再重算一次）
        les = live.get("engine_state") or {}
        ctx.tool_include = set(les.get("tools_on") or [])
        ctx.tool_off = set(les.get("tools_off") or [])
    return await REG.events.pipe("tool_specs", active_tool_specs(ctx.tool_exclude | getattr(ctx, "tool_off", set()),
                                                                 getattr(ctx, "tool_include", None)), ctx)


async def tool_overview(ctx: TurnContext) -> list[dict]:
    """本回合會送給模型的工具，附上被排除的原因。排除規則與 run_turn 相同。"""
    from woof.agent.tasks import PLAN_TOOLS
    es = ctx.session.get("engine_state") or {}
    reasons: dict[str, str] = {}
    inc = set(getattr(ctx, "tool_include", None) or ()) & disabled_tools()
    for n in disabled_tools() - inc:
        reasons[n] = "設定中停用"
    off = set(es.get("plugins_off") or [])
    for n, t in REG.tools.items():
        if t.plugin in off:
            reasons.setdefault(n, "插件已停用")
    if es.get("plan"):
        for n in PLAN_TOOLS:
            reasons.setdefault(n, "計畫模式不給")
    for ex in await REG.events.collect("tool_exclude", ctx):
        for n in ex:
            reasons.setdefault(n, "權限設定關閉")
    for ex in [ctx.tool_exclude]:
        for n in ex:
            reasons.setdefault(n, "這個對話停用")
    for n in getattr(ctx, "tool_off", ()) or ():
        reasons.setdefault(n, "這個對話關了")
    pre = [t for t in REG.tool_specs() if t["name"] not in reasons]
    pre = [t for t in pre if t["name"] not in inc] + [t for t in pre if t["name"] in inc]
    sent = {t["name"]: t for t in await REG.events.pipe("tool_specs", pre, ctx)}
    out = []
    for t in REG.tool_specs():
        tool = REG.tools.get(t["name"])
        n = t["name"]
        if getattr(tool, "bridge", False) and n not in sent:
            continue
        t = sent.get(n, t)
        chars, tok = tool_cost(n, t["description"], t.get("schema"))
        deferred = n not in reasons and n not in sent
        out.append({"name": n, "plugin": getattr(tool, "plugin", ""), "description": t["description"],
                    "enabled": n not in reasons, "reason": reasons.get(n, "") or ("延遲載入：用到時才載入說明" if deferred else ""),
                    "deferred": deferred, "chars": chars, "tokens_est": 0 if deferred else tok,
                    "session_on": n in inc and n not in reasons,
                    "session_off": reasons.get(n) == "這個對話關了",
                    "drop_pending": n in set(es.get("tools_drop") or ())})
    for n, t in sent.items():
        if n not in REG.tools or n in reasons:
            chars, tok = tool_cost(n, t["description"], t.get("schema"))
            out.append({"name": n, "plugin": "tool_search", "description": t["description"], "enabled": True,
                        "reason": "", "bridge": True, "chars": chars, "tokens_est": tok})
    return out


# 子代理禁用的工具：唯讀不給任何會改東西的工具；寫入模式不給終端機（無法限定範圍）
READ_ONLY_EXCLUDE = {"write_file", "patch", "terminal", "process", "skill_write"}
WRITE_EXCLUDE = {"process", "skill_write"}  # 寫入子代理可以用 terminal 跑測試，限制在 terminal 插件裡（範圍內、不能背景）


def mutating_tools(read_only: bool) -> set[str]:
    names = set(READ_ONLY_EXCLUDE if read_only else WRITE_EXCLUDE)
    # 外部 MCP 工具會不會改東西無從得知，子代理一律不給
    names |= {n for n, t in REG.tools.items() if getattr(t, "plugin", "") == "mcp_client"}
    return names


def model_identity(m: dict | None) -> str:
    """告訴模型它是誰：模型自己常答錯（訓練資料裡的舊名稱），以 WOOF 實際連的為準。"""
    if not m:
        return ""
    label = m.get("label") or m.get("id")
    wire = m.get("model") or m.get("id")
    vid = m.get("vendor") or ""
    v = next((x for x in REG.config.get("vendors", []) if x.get("id") == vid), None) if vid else None
    vendor = (v or {}).get("label") or vid
    return _P("model_identity", label=label, vendor=vendor or "（未分類）", wire=wire)


def prompt_override(session: dict) -> str | None:
    """這個對話手動覆寫的系統提示詞；沒有就用預設組合。"""
    t = (session.get("engine_state") or {}).get("prompt_override")
    return t if isinstance(t, str) and t.strip() else None


def prompt_fp(ctx: "TurnContext") -> str:
    """這個對話「明確設定」的指紋：提示詞覆寫、計畫模式／子代理附加段、輸出風格、停用的插件、工作目錄、連線類型。
    這些改了就重新組系統提示詞；記憶、Skills、專案指示檔、MCP 清單的變動等到壓縮或新對話才套用。"""
    es = ctx.session.get("engine_state") or {}
    # 輸出風格、權限不放進指紋：中途改了改用「設定變更」附在下一則訊息最後（settings_note），系統提示詞不重組，快取不斷
    # 模型 id 也放進來：系統提示詞寫了「你是哪個模型」；換模型本來就沒有快取可沿用（快取是各模型分開的），重組不多花
    key = [prompt_override(ctx.session) or "", ctx.extra_system or "",
           sorted(es.get("plugins_off") or []), ctx.cwd, ctx.model_cfg.get("provider") or "", ctx.model_cfg.get("id") or ""]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def prompt_gen(session_id: str) -> int:
    st = store.get_session(session_id) or {}
    return int((st.get("engine_state") or {}).get("compress_count") or 0)


async def system_for(ctx: "TurnContext") -> str:
    """送給模型的系統提示詞。第一次送出時組好存起來，之後同一個對話一直用同一份，
    模型那邊的前綴快取（Anthropic、DeepSeek、OpenAI 等）才不會每回合失效。"""
    fp, gen = prompt_fp(ctx), prompt_gen(ctx.session_id)
    snap = store.get_prompt_snapshot(ctx.session_id)
    if snap and snap["fp"] == fp and int(snap["gen"] or 0) == gen and snap["text"]:
        return snap["text"]
    text = await build_system(ctx)
    store.set_prompt_snapshot(ctx.session_id, fp, gen, text)
    # 系統提示詞裡已經是目前的風格：記下來，之後只有再改才需要附設定變更
    s = store.get_session(ctx.session_id)
    if s is not None:
        es = dict(s.get("engine_state") or {})
        from woof.lang import ui_lang
        es["told"] = {**(es.get("told") or {}), "style": es.get("style") or "default", "model": ctx.model_cfg.get("id"),
                      "lang": ui_lang()}
        store.update_session(ctx.session_id, engine_state=es)
    return text


def settings_note(session: dict) -> tuple[str, dict]:
    """這個對話的權限、輸出風格跟上次告訴模型的不一樣 → 回傳要附在使用者訊息最後的說明，和新的 told。"""
    from woof import perms, styles

    es = session.get("engine_state") or {}
    told = dict(es.get("told") or {})
    lines = []
    perm = session.get("perm") or "auto"
    prev = told.get("perm", "auto")
    if perm != prev:
        lines.append(perms.describe(perm))
        freed = perms.loosened(prev, perm)
        if freed:
            lines.append(_P("perm_freed", cats="、".join(freed)))
        told["perm"] = perm
    mid = session.get("model") or ""
    if told.get("model") and mid and mid != told["model"]:
        new, old = REG.model_cfg(mid) or {}, REG.model_cfg(told["model"]) or {}
        lines.append(_P("model_switch", label=new.get("label") or mid, wire=new.get("model") or mid,
                        prev=old.get("label") or told["model"]))
        told["model"] = mid
    from woof.lang import reply_name, ui_lang
    lang = ui_lang()
    prev_lang = told.get("lang") or ("zh" if "style" in told else None)  # 舊對話沒記過：當時系統提示詞是中文
    if prev_lang and lang != prev_lang:
        lines.append("From now on, reply in English (the user switched the interface to English)." if lang == "en"
                     else _P("lang_switch", lang=reply_name(lang)))
        told["lang"] = lang
    sty = es.get("style") or "default"
    if "style" not in told:  # 舊對話沒記過：當作系統提示詞裡就是目前的風格
        told["style"] = sty
    elif sty != told["style"]:
        if sty == "default":
            lines.append(_P("style_reset"))
        else:
            st = next((x for x in styles.all_styles() if x["id"] == sty), None)
            lines.append(_P("style_note", name=st["name"] if st else sty, text=(st or {}).get("text") or ""))
        told["style"] = sty
    if not lines:
        return "", told
    # 編號＋時間：之後再改，模型看得出哪一則比較新，不會被舊的那則綁住
    told["seq"] = int(told.get("seq") or 0) + 1
    told["at"] = time.time()
    when = datetime.now(TPE).strftime("%m/%d %H:%M")
    return _P("settings_note", n=told["seq"], when=when, lines="\n".join("- " + x for x in lines)), told


# ---------------------------------------------------------------- 模型與工具挑選
def find_model(model_id: str | None) -> dict | None:
    for m in REG.config.get("models", []):
        if m["id"] == model_id:
            return m
    return None


def aux_model(task: str, fallback: dict | None) -> dict | None:
    """輔助工作（vision / compact / subagent / fallback / review）用的模型；沒設定就用 fallback。"""
    want = (REG.config.get("aux") or {}).get(task) or ""
    if want:
        return find_model(want)
    if task in ("fallback", "review"):  # 這兩個沒設定就是不啟用
        return fallback
    # 跟隨主要模型（設定頁「主要模型」）；主要模型不存在才退回目前對話的模型
    return find_model(REG.config.get("default_model") or "") or fallback


def vision_chain(turn_model: dict | None) -> list[dict]:
    """看圖用的模型，依序試：輔助模型的看圖 → 這個對話的模型 → 主要模型。
    跳過不能看圖的（手動關掉或自動偵測到）、服務商暫停中的（caps.down）、不是 API 連線的。不會自己挑別的付費模型。"""
    from woof import caps

    out: list[dict] = []
    for m in (aux_model("vision", None) if (REG.config.get("aux") or {}).get("vision") else None,
              turn_model, find_model(REG.config.get("default_model") or "")):
        if not m or any(x["id"] == m["id"] for x in out):
            continue
        cfg = REG.model_cfg(m["id"]) or m
        if cfg.get("vision") is False or caps.down(m["id"]):   # vision 已含自動偵測，手動指定能看的不跳過
            continue
        if cfg.get("provider") not in REG.providers:
            continue
        out.append(cfg)
    return out


def disabled_tools() -> set[str]:
    s = set(REG.config.get("disabled_tools") or [])
    if "memory" in s:  # 舊設定：memory 拆成 memory_view（只讀）與 memory_write 之前停用的，當成停用寫入
        s.add("memory_write")
    return s


def active_tool_specs(exclude: set[str] | None = None, include: set[str] | None = None) -> list[dict]:
    """include＝這個對話手動開的（全域停用的工具）：排在最後面，前面的工具順序不變。"""
    ex = exclude or set()
    inc = set(include or ()) & disabled_tools()
    off = disabled_tools() | ex
    specs = REG.tool_specs()
    return [t for t in specs if t["name"] not in off] + [t for t in specs if t["name"] in inc and t["name"] not in ex]


async def complete(model_cfg: dict, system: str, messages: list[dict], max_text: int = 0) -> str:
    """不帶工具、一次性的模型呼叫（壓縮摘要、看圖等輔助工作用）。"""
    backend = model_cfg.get("provider")
    provider = REG.providers.get(backend)
    if provider is None:
        raise RuntimeError(f"「{model_cfg.get('label', model_cfg['id'])}」不能做輔助工作（{backend} 不是 API 型連線），"
                           "請在「設定 → 輔助模型」換一個")
    final = None
    async for ev in provider.stream(model=model_cfg, system=system, messages=messages, tools=[], effort=None):
        if ev.get("type") == "message":
            final = ev
        elif ev.get("type") == "usage":
            # 輔助呼叫（壓縮摘要、看圖、審核、標題）也記進用量，用量面板才不會少算
            try:
                store.log_usage("aux", model_cfg.get("id"), ev)
            except Exception:
                pass
    if final is None:
        raise RuntimeError("模型沒有回傳完整訊息")
    return "".join(b.get("text", "") for b in final["content"] if b.get("type") == "text").strip()


def _clip(text: str, call_id: str) -> str:
    if len(text) <= MAX_TOOL_CHARS:
        return text
    path = SPILL / f"{call_id}.txt"
    path.write_text(text, encoding="utf-8")
    head, tail = text[: MAX_TOOL_CHARS * 2 // 3], text[-MAX_TOOL_CHARS // 4:]
    return (f"{head}\n\n…（中間省略 {len(text) - len(head) - len(tail)} 字，全文存於 {path.as_posix()}，"
            f"需要時用 read_file 讀取）…\n\n{tail}")


# 「先問我」模式下不用確認的唯讀工具
SAFE_TOOLS = {"read_file", "search_files", "list_tree", "web_search", "web_extract", "vision_analyze", "skill_view", "skills_list",
              # 工具延遲載入的橋接：tool_call 拆開後每一筆照常確認
              "tool_search", "tool_describe", "tool_call"}
_approvals: dict[str, dict] = {}
_allow_all: set[str] = set()


def pending_approvals() -> list[dict]:
    return [{k: v for k, v in a.items() if k != "future"} for a in _approvals.values()]


def resolve_approval(call_id: str, decision: str) -> bool:
    a = _approvals.get(call_id)
    if not a or a["future"].done():
        return False
    a["future"].set_result(decision)
    return True


def reset_allow_all(session_id: str) -> None:
    _allow_all.discard(session_id)


async def _auto_review(name: str, args: dict, ctx: TurnContext) -> str:
    """設定了「自動審核」模型時，先問它這個動作安不安全。回放行理由；空字串＝要問使用者。
    只給工具名稱、參數、工作目錄，不給對話內容。"""
    model = aux_model("review", None)
    if not model or model.get("provider") not in REG.providers:
        return ""
    raw = json.dumps(args, ensure_ascii=False, indent=1, default=str)
    if len(raw) > 6000:
        return ""  # 參數太長看不完整，交給人
    text = f"工具：{name}\n工作目錄：{ctx.cwd}\n參數：\n{raw}"
    try:
        out = await asyncio.wait_for(
            complete(model, _P("auto_review"), [{"role": "user", "content": [{"type": "text", "text": text}]}]), 30)
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""
    line = (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""
    if line.upper().startswith("SAFE"):
        try:
            store.log_op(ctx.session_id, "review", name, 0, None, False)
        except Exception:
            pass
        return line[4:].strip(" ：:-—") or "判定安全"
    return ""


async def _approved(call: dict, ctx: TurnContext) -> tuple[bool, str]:
    """權限檢查（woof/perms.py）：回傳（可否執行, 擋下時給模型看的原因）。"""
    from woof import perms

    name, cid = call["name"], call["id"]
    sid = getattr(ctx, "root_session_id", ctx.session_id)
    st = store.get_session(sid) or {}
    if name in ("tool_search", "tool_describe", "tool_call"):  # 延遲載入的橋接；拆開後每一筆照常檢查
        return True, ""
    headless = bool((st.get("engine_state") or {}).get("headless"))
    r = perms.decide(st.get("perm") or "auto", name, call.get("args") or {}, headless)
    if r["decision"] == "deny":
        return False, _P("perm_denied", reason=r["reason"])
    if r["decision"] == "auto" or (sid in _allow_all and not r["reason"].startswith("危險指令")):
        return True, ""
    if not headless and not r["reason"].startswith("危險指令"):
        verdict = await _auto_review(name, call.get("args") or {}, ctx)
        if verdict:
            await ctx.emit({"type": "notice", "session_id": sid, "text": f"自動審核放行 {name}：{verdict}"})
            return True, ""
    fut = asyncio.get_running_loop().create_future()
    _approvals[cid] = {"session_id": sid, "id": cid, "name": name, "args": call.get("args") or {}, "future": fut,
                       "reason": r["reason"]}
    await ctx.emit({"type": "approval", "session_id": sid, "id": cid, "name": name, "args": call.get("args") or {},
                    "reason": r["reason"]})
    try:
        decision = await fut
    finally:
        _approvals.pop(cid, None)
        await ctx.emit({"type": "approval_done", "session_id": sid, "id": cid})
    if decision == "always":
        _allow_all.add(sid)
    return decision in ("allow", "always"), "使用者拒絕執行這個工具。"


async def _run_tool(call: dict, ctx: TurnContext) -> dict:
    name, args, cid = call["name"], call.get("args") or {}, call["id"]
    t0 = time.time()
    await ctx.emit({"type": "tool_start", "session_id": ctx.session_id, "id": cid, "name": name, "args": args})
    tool = REG.tools.get(name)
    is_error = False
    # 原地打轉：同一個工具、同樣參數在這一回合已經失敗 2 次，第 3 次不再執行，請模型換方法
    fails = ctx.__dict__.setdefault("tool_fails", {})
    fp = name + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)[:4000]
    if fails.get(fp, 0) >= 2:
        output, is_error = _P("repeat_fail", name=name, n=fails[fp]), True
        fails[fp] += 1
        ms = 0
        await ctx.emit({"type": "tool_end", "session_id": ctx.session_id, "id": cid, "name": name,
                        "output": output, "is_error": True, "ms": ms})
        return {"type": "tool_result", "id": cid, "name": name, "output": output, "is_error": True}
    if tool is None:
        output, is_error = f"找不到工具：{name}", True
    elif isinstance(args, dict) and "_raw" in args:
        output, is_error = _P("raw_args"), True
    elif ((name in disabled_tools() and name not in getattr(ctx, "tool_include", ())) or name in getattr(ctx, "tool_exclude", ())
          or name in getattr(ctx, "tool_off", ())):
        output, is_error = f"工具 {name} 已被停用", True
    elif not (ok := await _approved(call, ctx))[0]:
        output, is_error = ok[1], True
    else:
        try:
            args = await REG.events.pipe("before_tool", args, name, ctx)
            tctx = ToolContext(ctx.session_id, ctx.cwd, ctx.emit, cid)
            tctx.turn = ctx
            result = await tool.handler(args, tctx)
            result = await REG.events.pipe("after_tool", result, name, args, ctx)
            if isinstance(result, dict):
                is_error = bool(result.get("is_error"))
                output = result.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False, indent=1)
            else:
                output = str(result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            output, is_error = f"{type(e).__name__}: {e}", True
    if _redact.enabled():
        output = _redact.redact(output)  # 金鑰遮罩：送模型、存對話、畫面都看不到完整的 key
    output = _clip(output, cid)
    if is_error:
        fails[fp] = fails.get(fp, 0) + 1
    else:
        fails.pop(fp, None)
    ms = int((time.time() - t0) * 1000)
    await ctx.emit({"type": "tool_end", "session_id": ctx.session_id, "id": cid, "name": name,
                    "output": output, "is_error": is_error, "ms": ms})
    try:
        store.log_op(ctx.session_id, "tool", name, ms, None, is_error)
    except Exception:
        pass
    return {"type": "tool_result", "id": cid, "name": name, "output": output, "is_error": is_error}



# 正在跑的子代理對話（run_subagent 開的）。使用者在那些子對話裡打字＝插話給它，不是開新回合。
AGENT_RUNS: set[str] = set()

_steer: dict[str, list[list[dict]]] = {}
_steer_lock = threading.Lock()


def push_steer(session_id: str, blocks: list[dict], meta: dict | None = None) -> None:
    """執行中排隊的訊息：使用者插話，或內部通知（例如背景指令結束，meta 帶 internal）。
    這一步結束後才寫進資料庫，順序才對。"""
    with _steer_lock:
        _steer.setdefault(session_id, []).append({"blocks": blocks, "meta": meta})


def requeue_steer(session_id: str, items: list[dict]) -> None:
    with _steer_lock:
        _steer.setdefault(session_id, [])[:0] = items


def peek_steer(session_id: str) -> list[dict]:
    with _steer_lock:
        return list(_steer.get(session_id, []))


def pop_steer(session_id: str) -> list[dict]:
    with _steer_lock:
        return _steer.pop(session_id, [])


async def flush_steer(session_id: str, emit) -> bool:
    """把排隊的訊息寫進資料庫。放在目前這步的回覆之後，順序才對。"""
    batch = pop_steer(session_id)
    if not batch:
        return False
    for it in batch:
        m = store.add_message(session_id, "user", it["blocks"], it.get("meta"))
        await emit({"type": "message", "session_id": session_id, "message": m})
    await emit({"type": "queue", "session_id": session_id, "items": []})
    return True


def history(session_id: str) -> list[dict]:
    from woof.agent import internal

    out = []
    rows = store.get_messages(session_id)
    for i, m in enumerate(rows):
        content, meta = m["content"], m.get("meta") or {}
        if meta.get("internal") and m["role"] == "user":
            content = internal.for_model(meta["internal"], content)
        elif meta.get("note") and m["role"] == "user":
            content = [*content, {"type": "text", "text": meta["note"]}]  # 設定變更附在最後
        out.append({"id": m["id"], "role": m["role"], "content": content, "meta": meta})
        # 工具呼叫沒有對應結果（工具執行中程式被關掉）：補上「沒有結果」，否則 API 會拒絕整段對話
        if m["role"] == "assistant":
            ids = [b["id"] for b in content if b.get("type") == "tool_call"]
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            got = {b.get("id") for b in (nxt or {}).get("content") or [] if b.get("type") == "tool_result"} \
                if nxt and nxt["role"] == "tool" else set()
            miss = [c for c in content if c.get("type") == "tool_call" and c["id"] not in got]
            if ids and miss:
                fill = [{"type": "tool_result", "id": c["id"], "name": c.get("name", ""), "is_error": True,
                         "output": "沒有結果：工具執行到一半，程式被關閉或中斷。需要的話重新執行。"} for c in miss]
                if got:
                    nxt["content"] = list(nxt["content"]) + fill  # 只缺一部分：併進原本的工具訊息（rows 是新讀的，不影響資料庫）
                else:
                    out.append({"id": m["id"], "role": "tool", "content": fill, "meta": {"synthetic": True}})
    return out


# 模型說「上下文太長」的錯誤字樣（各家說法不同）
_OVERFLOW_PAT = (
    "context length", "context size", "maximum context", "context window", "context_length_exceeded",
    "prompt is too long", "prompt too long", "input is too long", "too many tokens", "reduce the length",
    "max_model_len", "maximum model length", "exceeds the maximum number of input tokens", "token limit",
    "超过最大长度", "上下文长度", "超過最大長度",
)


def is_overflow_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(p in s for p in _OVERFLOW_PAT)


async def _compact_now(ctx: TurnContext, force: bool, reason: str = "") -> bool:
    """回合進行中壓縮（context 插件的 mid_turn 掛鉤）。force＝已經超出上下文，不看門檻一定壓。"""
    ctx.mid_turn, ctx.force_compact, ctx.compact_reason = True, force, reason
    try:
        done = await REG.events.collect("mid_turn", ctx)
    finally:
        ctx.mid_turn, ctx.force_compact = False, False
    return any(done)


async def _add_internal(ctx: TurnContext, kind: str, text: str) -> None:
    from woof.agent.internal import meta as _imeta
    nm = store.add_message(ctx.session_id, "user", [{"type": "text", "text": text}], _imeta(kind))
    await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": nm})


def mark_interrupted(session_id: str, reason: str, kind: str) -> dict | None:
    """記下「這一回合沒做完」：前端顯示「繼續」，下一回合開始時清掉。"""
    st = store.get_session(session_id)
    if not st:
        return None
    es = dict(st.get("engine_state") or {})
    es["interrupted"] = {"reason": reason, "kind": kind, "at": time.time()}
    return store.update_session(session_id, engine_state=es)


def clear_interrupted(session_id: str) -> dict | None:
    st = store.get_session(session_id)
    es = dict((st or {}).get("engine_state") or {})
    if "interrupted" not in es:
        return None
    es.pop("interrupted", None)
    return store.update_session(session_id, engine_state=es)


def apply_pending(session: dict | None) -> dict | None:
    """套用排隊中的插件開關：只在這個對話的系統提示詞本來就會重組時做（壓縮過、提示詞覆寫或模型換過）。

    中途改 plugins_off 會改到 prompt_fp，讓模型的前綴快取整段失效；改成排隊，
    等下一次重組（ compress_count 改變、或 fp 本來就不同）再一次套用，不多花一次快取。
    """
    es = dict((session or {}).get("engine_state") or {})
    off = es.pop("pending_off", None)
    gen = es.pop("pending_gen", None)
    if off is None:
        return session
    es["plugins_off"] = [str(x) for x in off]
    es["preset_gen"] = gen
    return store.update_session(session["id"], engine_state=es) or session


def pending_off(session: dict | None) -> list[str]:
    """這個對話還沒套用的插件開關（面板顯示用）。"""
    return [str(x) for x in ((session or {}).get("engine_state") or {}).get("pending_off") or []]


def _speed(t_req: float, t_first: float | None, out_tok, text_buf: dict) -> dict:
    """這一步的生成速度，存在助理訊息的 meta：out_tok（輸出 token，含思考）、gen_s（第一個字到最後一個字）、
    ttft_s（送出到第一個字）。服務商沒回用量時用字數估，標 tok_est。"""
    end = time.time()
    if t_first is None:
        return {}
    d = {"gen_s": round(max(end - t_first, 0.0), 2), "ttft_s": round(t_first - t_req, 2)}
    if out_tok:
        d["out_tok"] = int(out_tok)
    else:
        from woof.tokens import estimate_text

        d["out_tok"] = estimate_text(text_buf.get("text", "") + text_buf.get("thinking", ""))
        d["tok_est"] = True
    return d


async def run_model_loop(ctx: TurnContext) -> None:
    provider = REG.providers[ctx.model_cfg["provider"]]
    nudged = False  # 工具結果後模型給空內容，只請它文字總結一次
    overflow_retry = 0
    ctx.fell_back = getattr(ctx, "fell_back", False)
    ctx.cont_counts = getattr(ctx, "cont_counts", None) or {}
    max_steps = MAX_STEPS
    for p in await REG.events.collect("max_steps", ctx):
        max_steps = int(p) if int(p) > 0 else 0  # 0＝不限
    ctx.max_steps = max_steps
    for _step in (range(max_steps) if max_steps else itertools.count()):
        ctx.step = _step
        if _step:
            # 回合進行中也看用量：工具結果一直疊上去時，先壓縮再送，不等爆掉
            await _compact_now(ctx, False)
        if max_steps >= 20 and _step == max_steps - 5:
            # 快到步數上限：先提醒模型收尾，不要做到一半被硬切
            await _add_internal(ctx, "steps_low", _P("steps_low", left=5, total=max_steps))
        system = await system_for(ctx)
        hist = history(ctx.session_id)
        through = hist[-1]["id"] if hist else 0
        messages = await REG.events.pipe("before_request", hist, ctx)
        specs = await tool_specs_for(ctx)
        final: dict | None = None
        partial: list[dict] = []
        text_buf = {"text": "", "thinking": ""}
        t_req, t_first, out_tok = time.time(), None, None
        try:
            async for ev in provider.stream(
                model=ctx.model_cfg, system=system, messages=messages,
                tools=specs, effort=ctx.effort,
            ):
                t = ev.get("type")
                if t_first is None and t in ("text_delta", "thinking_delta"):
                    t_first = time.time()
                if t in ("text_delta", "thinking_delta"):
                    kind = "text" if t == "text_delta" else "thinking"
                    text_buf[kind] += ev["text"]
                    await ctx.emit({"type": "delta", "session_id": ctx.session_id, "kind": kind, "text": ev["text"]})
                elif t == "usage":
                    usage = {k: ev.get(k) for k in ("input_tokens", "output_tokens", "cache_read", "cache_write")}
                    out_tok = ev.get("output_tokens") or out_tok
                    from woof.tokens import window_of
                    usage["context_window"] = window_of(ctx.model_cfg) or None
                    # input_tokens 已含快取讀寫；上下文用量＝輸入＋輸出。through＝這次請求包含到哪一則（用量錨點）
                    usage["context_used"] = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                    usage["through"] = through
                    await ctx.emit({"type": "usage", "session_id": ctx.session_id, **usage})
                    st = store.get_session(ctx.session_id) or {}
                    es = st.get("engine_state") or {}
                    es["usage"] = usage
                    store.update_session(ctx.session_id, engine_state=es)
                    store.log_usage(ctx.session_id, ctx.model_cfg.get("id"), usage)
                elif t == "notice":
                    await ctx.emit({"type": "notice", "session_id": ctx.session_id, "text": ev.get("text", "")})
                elif t == "message":
                    final = ev
        except asyncio.CancelledError:
            if text_buf["thinking"]:
                partial.append({"type": "thinking", "text": text_buf["thinking"]})
            if text_buf["text"]:
                partial.append({"type": "text", "text": text_buf["text"] + "\n\n（已停止）"})
            if partial:
                m = store.add_message(ctx.session_id, "assistant", partial, {"model": ctx.model_cfg["id"], "stopped": True})
                await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
            raise
        except Exception as e:
            # 上下文超出模型上限：壓縮後重送這一步（最多兩次），不讓整個回合中斷
            if is_overflow_error(e) and not text_buf["text"] and overflow_retry < 2:
                overflow_retry += 1
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": "上下文超出模型上限，先壓縮再重送這一步…"})
                if await _compact_now(ctx, True, "上下文超出模型上限"):
                    continue
            # 這一步還沒輸出正文就失敗（服務商自己的重試、換 key、限流等待都用完了）：
            # 回合中途也改用備用模型接著做，前面的工具結果都保留；每回合只換一次
            fb = None if ctx.fell_back or text_buf["text"] else aux_model("fallback", None)
            if fb and fb["id"] != ctx.model_cfg["id"] and fb.get("provider") in REG.providers:
                old = ctx.model_cfg.get("label", ctx.model_cfg["id"])
                ctx.fell_back = True
                ctx.model_cfg, ctx.effort = fb, fb.get("default_effort")
                provider = REG.providers[fb["provider"]]
                await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                                "text": f"{old} 失敗（{str(e)[:160]}），這一回合改用備用模型 {fb.get('label', fb['id'])} 接著做"})
                continue
            raise

        store.log_op(ctx.session_id, "model", ctx.model_cfg.get("id"), (time.time() - t_req) * 1000,
                     (t_first - t_req) * 1000 if t_first else None)
        if final is None:
            raise RuntimeError("模型沒有回傳完整訊息")
        content = final["content"]
        fmeta = final.get("meta") or {}
        calls = [b for b in content if b.get("type") == "tool_call"]
        has_text = any(b.get("type") == "text" and str(b.get("text", "")).strip() for b in content)
        if not calls and not has_text:
            # 模型回了空內容：不存空訊息。先請它用文字回覆一次；還是空就明確告知
            if await flush_steer(ctx.session_id, ctx.emit):
                continue
            if fmeta.get("stop_reason") in ("max_tokens", "length"):
                # 輸出額度全被思考用掉、正文還沒開始就被截斷：請它精簡思考重來一次；還是一樣就明說原因
                if not ctx.cont_counts.get("think_out"):
                    ctx.cont_counts["think_out"] = 1
                    await _add_internal(ctx, "truncated", _P("think_out"))
                    continue
                content = [{"type": "text", "text": "（模型的思考用完了輸出上限，正文還沒開始就被截斷。可以調高這個供應商的輸出上限 max_tokens，"
                                                    "或把思考強度調低後再試。）"}]
                m = store.add_message(ctx.session_id, "assistant", content, {"model": ctx.model_cfg["id"], "empty": True})
                await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
                return
            if not nudged:
                nudged = True
                last = hist[-1] if hist else {}
                ask = _P("nudge_tool") if last.get("role") == "tool" else _P("nudge_empty")
                await _add_internal(ctx, "nudge", ask)
                continue
            content = [{"type": "text", "text": "（模型這一步沒有給出文字回覆。可以再問一次，或換一個模型。）"}]
            m = store.add_message(ctx.session_id, "assistant", content, {"model": ctx.model_cfg["id"], "empty": True})
            await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
            return
        meta = {"model": ctx.model_cfg["id"], **fmeta, **_speed(t_req, t_first, out_tok, text_buf)}
        m = store.add_message(ctx.session_id, "assistant", content, meta)
        await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
        await REG.events.emit("on_message", m, ctx)

        if not calls:
            if await flush_steer(ctx.session_id, ctx.emit):
                continue
            # 做到一半停下（輸出被截斷、說要做卻沒呼叫工具、待辦沒做完）：插件判斷要不要補一句請它接著做
            nudge = None
            for r in await REG.events.collect("continue_turn", ctx, m, specs):
                if isinstance(r, tuple) and len(r) == 2 and r[1]:
                    nudge = r
                    break
            if nudge:
                await _add_internal(ctx, nudge[0], nudge[1])
                continue
            return
        try:
            results = await asyncio.gather(*[_run_tool(c, ctx) for c in calls])
        except asyncio.CancelledError:
            # 工具階段被停止：前面那則 assistant（模型剛說的話）要標成已停止，前端才會顯示
            store.mark_message_stopped(m["id"])
            stopped = [{"type": "tool_result", "id": c["id"], "name": c["name"],
                        "output": "使用者已停止執行。", "is_error": True} for c in calls]
            m = store.add_message(ctx.session_id, "tool", stopped)
            await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
            raise
        m = store.add_message(ctx.session_id, "tool", list(results))
        await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
        # 工具結果之後、下一次呼叫模型之前：把執行中插入的句子併入，模型這一步就看得到
        await flush_steer(ctx.session_id, ctx.emit)
        # 子代理：使用者在主對話插話了，先停下把進度交回去，主代理看到插話再決定要不要重派
        if ctx.session.get("parent") and any(not (it.get("meta") or {}).get("internal")
                                             for it in peek_steer(getattr(ctx, "root_session_id", ""))):
            done = [x for x in store.get_messages(ctx.session_id) if x["role"] == "assistant"]
            said = "\n".join(b.get("text", "") for x in done for b in x["content"] if b.get("type") == "text").strip()
            steps = sum(1 for x in store.get_messages(ctx.session_id) if x["role"] == "tool")
            text = (f"（使用者在主對話插話，子代理先停在這裡。已執行 {steps} 步工具，任務可能還沒完成。）"
                    + (f"\n\n目前為止的進度：\n{said[-3000:]}" if said else ""))
            m = store.add_message(ctx.session_id, "assistant", [{"type": "text", "text": text}],
                                  {"model": ctx.model_cfg["id"], "interrupted_by_user": True})
            await ctx.emit({"type": "message", "session_id": ctx.session_id, "message": m})
            return
    s2 = mark_interrupted(ctx.session_id, f"已達單次最多 {max_steps} 步", "max_steps")
    if s2:
        await ctx.emit({"type": "session_updated", "session": s2})
    await ctx.emit({"type": "notice", "session_id": ctx.session_id,
                    "text": f"已達單次最多 {max_steps} 步，先暫停。按輸入框上方的「繼續」接著做。"})


async def run_turn(session_id: str, blocks: list[dict], emit: Emit) -> None:
    session = store.get_session(session_id)
    if session is None:
        raise RuntimeError("對話不存在")
    model_cfg = REG.model_cfg(session.get("model"))
    if model_cfg is None:
        if session.get("model"):
            raise RuntimeError(f"這個對話用的模型「{session.get('model')}」已經不在模型清單裡（被刪除或改名），"
                               "請在輸入框右下角重新選一個模型再送")
        raise RuntimeError("config.toml 裡還沒有設定任何模型")
    effort = session.get("effort") or model_cfg.get("default_effort")

    # 預算：超過每日／每月上限時依設定只警告、暫停，或這一回合改用備用模型（排程、子代理同樣算在內）
    from woof import cost
    bst = cost.status(REG.config) if (REG.config.get("budget") or {}).get("enabled") else None
    if bst and bst["over"] and cost.price_of(model_cfg) is not None:
        why = "；".join(bst["over"])
        if bst["action"] == "pause":
            raise RuntimeError(f"{why}。已暫停送出（設定在右側面板「用量」的預算）")
        fb = aux_model("fallback", None) if bst["action"] == "fallback" else None
        if fb and fb["id"] != model_cfg["id"] and fb.get("provider") in REG.providers:
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"{why}，這一回合改用備用模型 {fb.get('label', fb['id'])}"})
            model_cfg, effort = fb, fb.get("default_effort")
        else:
            await emit({"type": "notice", "session_id": session_id, "text": f"{why}（只提醒，照常送出）"})

    # 排隊的插件開關：這個對話已經送出過訊息，現在換會改到 prompt_fp、讓系統提示詞重組、
    # 前綴快取整段失效。所以等 compress_count 或提示詞指紋本來就要變的那一次才套用。
    pend = pending_off(session)
    if pend:
        es0 = dict(session.get("engine_state") or {})
        gen0 = int(es0.get("compress_count") or 0)
        pg = es0.get("pending_gen")
        pg = -1 if pg is None else int(pg)  # 0 是「還沒壓縮過」，不能用 or 當成沒設
        ctx_probe = TurnContext(session, model_cfg, effort, emit)
        snap = store.get_prompt_snapshot(session_id)
        if gen0 != pg or not snap or snap["fp"] != prompt_fp(ctx_probe):
            session = apply_pending(session) or session
            if not pending_off(session):
                await emit({"type": "notice", "session_id": session_id,
                            "text": "已套用排隊的插件開關（系統提示詞重新組裝了一次）。"})

    note, told = settings_note(session)
    if note:
        es = dict(session.get("engine_state") or {})
        es["told"] = told
        session = store.update_session(session_id, engine_state=es) or session
    m = store.add_message(session_id, "user", blocks, {"note": note} if note else None)
    await emit({"type": "message", "session_id": session_id, "message": m})
    s_clr = clear_interrupted(session_id)
    if s_clr:
        await emit({"type": "session_updated", "session": s_clr})
    if session.get("title") in (None, "", "新對話"):
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        s = store.auto_title(session_id, text)
        if s:
            await emit({"type": "session_updated", "session": s})

    ctx = TurnContext(session, model_cfg, effort, emit)
    ctx.turn_mid = m["id"]
    if (session.get("engine_state") or {}).get("plan"):
        from woof.agent.tasks import PLAN_TOOLS
        from woof.prompts import get as _prompt
        ctx.tool_exclude |= PLAN_TOOLS
        ctx.extra_system = _prompt("plan_mode")
    off_plugins = set((session.get("engine_state") or {}).get("plugins_off") or [])
    if off_plugins:
        ctx.tool_exclude |= {n for n, t in REG.tools.items() if t.plugin in off_plugins}
    await REG.events.emit("prepare_turn", ctx)
    backend = model_cfg.get("provider")
    if backend in REG.engines:
        await REG.engines[backend].run(ctx, blocks)
    elif backend in REG.providers:
        before = m["id"]
        try:
            await run_model_loop(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 主要模型一開始就失敗（還沒產生任何回覆）時，改用備用模型重跑一次
            fb = aux_model("fallback", None)
            produced = any(x["id"] > before for x in store.get_messages(session_id))
            if getattr(ctx, "fell_back", False) or not fb or fb["id"] == model_cfg["id"] or produced or fb.get("provider") not in REG.providers:
                raise
            await emit({"type": "notice", "session_id": session_id,
                        "text": f"{model_cfg.get('label', model_cfg['id'])} 失敗（{str(e)[:120]}），改用備用模型 {fb.get('label', fb['id'])}"})
            ctx2 = TurnContext(session, fb, fb.get("default_effort"), emit)
            ctx2.turn_mid = ctx.turn_mid
            ctx2.tool_exclude = set(ctx.tool_exclude)
            ctx2.extra_system = ctx.extra_system
            await run_model_loop(ctx2)
    else:
        raise RuntimeError(f"模型「{model_cfg.get('label', model_cfg['id'])}」需要的插件 {backend} 沒有載入")
    await REG.events.emit("after_turn", ctx)


# ---------------------------------------------------------------- 子代理
import contextvars as _cv

# 同一次 delegate_task 派出的子代理共用一個批次 id（側欄依批次分層、可整批刪）
SUB_BATCH: "_cv.ContextVar[str]" = _cv.ContextVar("woof_sub_batch", default="")
SUB_KIND = {"review_request": "review", "expand_request": "review"}

async def _prepare_subagent(parent: TurnContext, goal: str, context: str = "", model_id: str = "",
                            progress: Callable[[str], Awaitable[None]] | None = None, label: str = "",
                            title_hint: str = "", kind: str = "subagent_task", read_only: bool = True,
                            write_scope: list | None = None,
                            session_id: str = "") -> tuple[dict, dict, TurnContext]:
    """建一個子對話（或接續既有的那一個）並把任務寫進去。回 (對話, 模型設定, ctx)。"""
    if parent.session.get("parent"):  # 工具清單已經拿掉 delegate_task，這裡再擋一次，萬一別的插件繞過來呼叫
        raise RuntimeError("子代理不能再派子代理")
    model_cfg = find_model(model_id) if model_id else aux_model("subagent", parent.model_cfg)
    if model_cfg is None:
        raise RuntimeError(f"找不到模型 {model_id}")
    if model_cfg.get("provider") not in REG.providers:
        raise RuntimeError(f"「{model_cfg.get('label', model_cfg['id'])}」不能當子代理（需要 API 型連線），"
                           "請在「設定 → 輔助模型」指定子代理模型")
    if session_id:
        # 接續同一個子代理：同一個對話、同一份上下文，不要再開新的
        child = store.get_session(session_id)
        if not child:
            raise RuntimeError(f"找不到子代理對話 {session_id}")
        owner = child.get("parent") or ""
        if owner not in (parent.session_id, getattr(parent, "root_session_id", "")):
            raise RuntimeError("那個對話不是這個對話的子代理，不能接續")
        title = child.get("title") or "子代理"
        own = REG.model_cfg(child.get("model"))
        if own and own.get("provider") in REG.providers:
            model_cfg = own
    else:
        title = title_hint or f"子代理：{goal.strip().splitlines()[0][:24]}"
        child = store.create_session(model_cfg["id"], model_cfg.get("default_effort"), title)
        # 側欄分層用：審核／拓展跟一般子代理分開放；同一次 delegate_task 派出的算同一批
        sub = {"kind": SUB_KIND.get(kind, "task"), "batch": SUB_BATCH.get() or ""}
        store.update_session(child["id"], parent=parent.session_id, engine_state={"sub": sub})
        child = store.get_session(child["id"])
        await parent.emit({"type": "session_updated", "session": child})

    async def emit(ev: dict) -> None:
        await parent.emit(ev)
        if progress and ev.get("type") == "tool_start":
            a = ev.get("args") or {}
            brief = a.get("command") or a.get("path") or a.get("query") or a.get("url") or ""
            await progress(f"{label}{ev.get('name')} {str(brief)[:120]}".rstrip())

    ctx = TurnContext(child, model_cfg, model_cfg.get("default_effort"), emit)
    ctx.root_session_id = getattr(parent, "root_session_id", parent.session_id)
    ctx.turn_mid = getattr(parent, "turn_mid", 0)
    ctx.tool_exclude = set(parent.tool_exclude) | {"delegate_task"}
    ctx.tool_exclude |= mutating_tools(read_only)
    ctx.write_scope = None if read_only else list(write_scope or [])
    ctx.cwd = parent.cwd
    from woof.prompts import get as _prompt
    ctx.extra_system = _prompt("subagent")
    text = goal.strip() + (_prompt("subagent_ctx", context=context.strip()) if context.strip() else "")
    from woof.agent import internal

    m = store.add_message(child["id"], "user", [{"type": "text", "text": text}], meta=internal.meta(kind))
    await emit({"type": "message", "session_id": child["id"], "message": m})
    return child, model_cfg, ctx


def _subagent_report(child_id: str, title: str, model_cfg: dict) -> dict:
    """從子對話整理出結構化回報：結論、步數、改過的檔、跑過的指令、出錯的工具。"""
    msgs = store.get_messages(child_id)
    last = next((x for x in reversed(msgs) if x["role"] == "assistant"), None)
    answer = "".join(b.get("text", "") for b in (last or {}).get("content", []) if b.get("type") == "text").strip()
    steps = sum(1 for x in msgs if x["role"] == "tool")
    files, cmds, errs, names = [], [], [], {}
    for x in msgs:
        for b in x["content"]:
            if b.get("type") == "tool_call":
                names[b.get("id")] = b.get("name")
                a = b.get("args") or {}
                if b.get("name") in ("write_file", "patch", "edit_file") and a.get("path") and a["path"] not in files:
                    files.append(str(a["path"]))
                elif b.get("name") == "terminal" and a.get("command"):
                    cmds.append(str(a["command"])[:160])
            elif b.get("type") == "tool_result" and b.get("is_error"):
                errs.append(f"{b.get('name') or names.get(b.get('id'))}：{str(b.get('output') or '')[:120]}")
    return {"session_id": child_id, "title": title, "model": model_cfg.get("label", model_cfg.get("id", "")),
            "answer": answer or "（子代理沒有給出文字回覆）", "steps": steps,
            "files_touched": files, "commands_run": cmds[-10:], "errors": errs[-5:]}


def report_text(r: dict) -> str:
    """把回報變成一段文字（送回主對話的內部訊息）。"""
    facts = []
    if r.get("files_touched"):
        facts.append("改過的檔：" + "、".join(r["files_touched"]))
    if r.get("commands_run"):
        facts.append("跑過的指令：" + "；".join(r["commands_run"]))
    if r.get("errors"):
        facts.append("出錯的工具：" + "；".join(r["errors"]))
    if r.get("error"):
        facts.append("失敗原因：" + str(r["error"]))
    head = f"子代理〔{r.get('title') or ''}〕（對話 {str(r.get('session_id') or '')[:12]}，{r.get('model') or ''}，{r.get('steps', 0)} 次工具）回報："
    tail = ("\n\n［實際紀錄］\n" + "\n".join(facts)) if facts else ""
    return head + "\n" + str(r.get("answer") or "") + tail


async def _drive(ctx: TurnContext) -> None:
    AGENT_RUNS.add(ctx.session_id)
    try:
        await run_model_loop(ctx)
    finally:
        AGENT_RUNS.discard(ctx.session_id)
        # 子代理最後一個事件送出時還在 AGENT_RUNS（busy=True），收尾補一個，前端才不會一直顯示「還在跑：審核…」
        try:
            await asyncio.shield(ctx.emit({"type": "flow_end", "session_id": ctx.session_id}))
        except Exception:
            pass


async def run_subagent(parent: TurnContext, goal: str, context: str = "", model_id: str = "",
                       progress: Callable[[str], Awaitable[None]] | None = None, label: str = "",
                       title_hint: str = "", kind: str = "subagent_task",
                       read_only: bool = True, write_scope: list | None = None,
                       session_id: str = "") -> dict:
    """跑到完（對抗、審核這種要拿到結果才能往下走的地方用）。"""
    child, model_cfg, ctx = await _prepare_subagent(
        parent, goal, context, model_id, progress, label, title_hint, kind, read_only, write_scope, session_id)
    await _drive(ctx)
    return _subagent_report(child["id"], child.get("title") or title_hint, model_cfg)


# 背景子代理：放著讓它跑，跑完自己把結果送回來（不要擋住主對話）
BG_TASKS: set[asyncio.Task] = set()
BG_BY_SID: dict[str, asyncio.Task] = {}   # 子代理對話 id → 背景任務（要能單獨停掉那一個）


def stop_agent(session_id: str) -> bool:
    """停掉某個子代理對話的背景任務（不影響主對話）。回傳有沒有真的停到。"""
    t = BG_BY_SID.get(session_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


async def run_subagent_bg(parent: TurnContext, goal: str, context: str = "", model_id: str = "",
                          progress: Callable[[str], Awaitable[None]] | None = None, label: str = "",
                          title_hint: str = "", kind: str = "subagent_task", read_only: bool = True,
                          write_scope: list | None = None, session_id: str = "",
                          done: Callable[[dict], Awaitable[None]] | None = None,
                          lock: "asyncio.Lock | None" = None) -> dict:
    """派出子代理但「不等它」：立刻回傳子代理對話，主對話可以繼續做別的事或跟使用者對話。
    跑完把回報送進主對話（閒置就開一步處理，執行中就在下一步看到）。"""
    child, model_cfg, ctx = await _prepare_subagent(
        parent, goal, context, model_id, progress, label, title_hint, kind, read_only, write_scope, session_id)
    parent_sid = parent.session_id

    async def _bg() -> None:
        status = "done"
        err = ""
        await parent.emit({"type": "run_start", "session_id": ctx.session_id})
        try:
            if lock is None:
                await _drive(ctx)
            else:
                async with lock:      # 寫入模式排隊：等前面那個寫完才開始，但主對話照樣能動
                    await _drive(ctx)
        except asyncio.CancelledError:
            # 被停掉也要回報，否則主對話會一直以為「它還在跑」而卡住
            err, status = "子代理被停止，沒有產出結論", "stopped"
            raise
        except Exception as e:  # 背景任務炸掉不能拖垮主對話
            err, status = f"{type(e).__name__}: {e}", "error"
        finally:
            try:
                await asyncio.shield(parent.emit({"type": "run_end", "session_id": ctx.session_id, "status": status}))
            except Exception:
                pass
            # 不管成功、出錯、被停掉，都要讓主對話聽到結果（不然它會一直等一個不會來的回報）
            try:
                r = _subagent_report(ctx.session_id, child.get("title") or title_hint, model_cfg)
                if err:
                    r["error"] = err
                if done is not None:
                    await asyncio.shield(done(r))
                inject = getattr(REG, "inject", None)
                if inject is not None:
                    await asyncio.shield(inject(parent_sid, "bg_done", report_text(r)))
            except Exception:
                log.exception("子代理回報送出失敗")

    task = asyncio.create_task(_bg())
    BG_TASKS.add(task)
    child_id = child["id"]
    BG_BY_SID[child_id] = task

    def _done(_t):
        BG_TASKS.discard(_t)
        if BG_BY_SID.get(child_id) is _t:
            BG_BY_SID.pop(child_id, None)

    task.add_done_callback(_done)
    return {"session_id": child["id"], "title": child.get("title") or title_hint,
            "model": model_cfg.get("label", model_cfg.get("id", "")), "background": True}


async def resume_session(session_id: str, emit: Emit) -> None:
    """插話已寫進資料庫後，接續同一個對話，不再新增使用者訊息。"""
    session = store.get_session(session_id)
    if session is None:
        return
    model_cfg = REG.model_cfg(session.get("model"))
    if model_cfg is None:
        return
    effort = session.get("effort") or model_cfg.get("default_effort")
    s_clr = clear_interrupted(session_id)
    if s_clr:
        await emit({"type": "session_updated", "session": s_clr})
    ctx = TurnContext(session, model_cfg, effort, emit)
    last_user = next((m for m in reversed(store.get_messages(session_id)) if m["role"] == "user"), None)
    ctx.turn_mid = last_user["id"] if last_user else 0
    backend = model_cfg.get("provider")
    if backend in REG.engines:
        msgs = store.get_messages(session_id)
        last = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if last:
            await REG.engines[backend].run(ctx, last["content"])
    elif backend in REG.providers:
        await run_model_loop(ctx)


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:16]


def load_image_b64(block: dict) -> tuple[str, str]:
    import base64
    data = Path(block["path"]).read_bytes()
    return block.get("media_type") or "image/png", base64.b64encode(data).decode()


__all__: list[Any] = ["run_turn", "TurnContext", "ToolContext", "new_call_id", "load_image_b64", "history"]
