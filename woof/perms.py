"""工具執行權限：依「類別」決定自動執行、先問、或禁止。

模式（每個對話各自選，輸入框下方的權限鈕；新對話的預設值是設定裡的 default_mode，沒設定時為 ask）：
  auto      全自動：全部直接執行（危險指令仍先問，見下方）
  edits     改檔自動：讀、寫檔直接做；跑指令、連網、子代理、MCP 先問
  ask       先問我：讀檔與搜尋直接做，其餘先問
  readonly  唯讀：只能讀與搜尋，寫檔、跑指令一律擋下
  custom    自訂：照設定頁「權限」的表

另外兩層，所有模式都適用：
  - 危險指令（rm -rf、git push --force、format…）在會自動執行的模式裡改成先問；排程等沒人能回答的對話直接擋下
  - 絕對禁止（刪整顆磁碟、fork bomb…）任何模式都擋，按「允許」也沒用
  - 指令白名單（例如 git status、ls）在「先問」時直接放行；黑名單一律擋
"""
from __future__ import annotations

import re

CATS = {
    "read": ("讀取", "讀檔、搜尋檔案、看圖、讀 Skill"),
    "write": ("寫入檔案", "建立、修改檔案，寫 Skill"),
    "exec": ("執行指令", "terminal、背景程序"),
    "network": ("網路", "網頁搜尋、讀網頁、生成圖片影片"),
    "agent": ("子代理與排程", "派子代理、建立排程"),
    "mcp": ("MCP 工具", "外部 MCP 伺服器提供的工具"),
    "other": ("其他", "沒有歸類的工具"),
}
DECISIONS = ("auto", "ask", "deny")

MODES = {
    "auto": ("全自動", "全部直接執行；危險指令仍先問"),
    "edits": ("改檔自動", "讀、寫檔直接做；跑指令、連網先問"),
    "ask": ("先問我", "讀檔、搜尋直接做，其餘先問"),
    "readonly": ("唯讀", "只能讀和搜尋，寫檔、跑指令擋下"),
    "custom": ("自訂", "照設定頁「權限」的表"),
}
_A, _Q, _D = "auto", "ask", "deny"
TABLE = {
    "auto": dict(read=_A, write=_A, exec=_A, network=_A, agent=_A, mcp=_A, other=_A),
    "edits": dict(read=_A, write=_A, exec=_Q, network=_Q, agent=_Q, mcp=_Q, other=_Q),
    "ask": dict(read=_A, write=_Q, exec=_Q, network=_A, agent=_Q, mcp=_Q, other=_Q),
    "readonly": dict(read=_A, write=_D, exec=_D, network=_A, agent=_D, mcp=_D, other=_D),
}
DEFAULT_CUSTOM = dict(TABLE["ask"])

TOOL_CAT = {
    "read_file": "read", "search_files": "read", "list_tree": "read", "vision_analyze": "read", "skill_view": "read", "skills_list": "read",
    "current_time": "read", "system_info": "read", "todo": "read", "ask_user": "read",
    "tool_search": "read", "tool_describe": "read", "tool_call": "read", "session_search": "read",
    "write_file": "write", "patch": "write", "skill_write": "write", "Write": "write", "Edit": "write",
    "terminal": "exec", "process": "exec", "Bash": "exec",
    "web_search": "network", "web_extract": "network", "image_generate": "network", "video_generate": "network",
    "memory": "write",
    "delegate_task": "agent", "schedule": "agent",
}

# 危險指令：自動模式下改成先問
DANGER = [
    (r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b", "rm -rf 遞迴刪除"),
    (r"\b(del|erase)\s+.*(/s|/q)", "del /s 大量刪除"),
    (r"\b(rd|rmdir)\s+.*?/s", "rmdir /s 刪除資料夾"),
    (r"Remove-Item\b.*-Recurse", "Remove-Item -Recurse 遞迴刪除"),
    (r"\bgit\s+push\b.*(--force|-f\b)", "git push --force 強制推送"),
    (r"\bgit\s+reset\s+--hard", "git reset --hard 丟掉改動"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "git clean -f 刪除未追蹤檔"),
    (r"\bgit\s+checkout\s+--\s+\.", "git checkout -- . 丟掉改動"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "關機或重開機"),
    (r"\bformat\s+[a-zA-Z]:", "格式化磁碟"),
    (r"\breg\s+delete\b", "刪除登錄檔"),
    (r"\btaskkill\b.*\s/im\s", "依名稱結束程序"),
    (r"\b(pkill|killall)\b", "依名稱結束程序"),
    (r"\bchmod\s+-R\s+777", "chmod -R 777"),
    (r"\b(drop|truncate)\s+(table|database)\b", "刪除資料表"),
    (r"(curl|wget|iwr|Invoke-WebRequest)\b[^|]*\|\s*(ba|z)?sh\b", "下載後直接執行"),
    (r"\b(npm|pip|uv\s+pip)\s+(un)?install\s+(-g|--global)\b", "全域安裝套件"),
    (r">\s*/dev/sd[a-z]", "直接寫入磁碟"),
]
# 絕對禁止：任何模式都擋
HARDLINE = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*(--no-preserve-root\s+)?/(\s|$|\*)", "刪除根目錄"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*~/?(\s|$)", "刪除整個家目錄"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\s+/dev/", "格式化磁碟"),
    (r"\bdd\s+.*of=/dev/(sd|nvme|hd)", "直接覆寫磁碟"),
    (r"\bformat\s+c:", "格式化系統碟"),
    (r"\b(rd|rmdir)\s+/s\s+/q\s+[a-zA-Z]:\\?\s*$", "刪除整顆磁碟"),
    (r"Remove-Item\s+.*[a-zA-Z]:\\\*?\s+.*-Recurse", "刪除整顆磁碟"),
]


def _ask_timeout(v) -> int:
    """問使用者時等幾秒才略過；0＝一直等，上限一天。"""
    try:
        return max(0, min(86400, int(v)))
    except (TypeError, ValueError):
        return 180


def settings() -> dict:
    from .loader import REG
    p = dict(REG.config.get("permissions") or {})
    custom = {**DEFAULT_CUSTOM, **{k: v for k, v in (p.get("custom") or {}).items() if k in CATS and v in DECISIONS}}
    dm = p.get("default_mode")
    return {
        "default_mode": dm if dm in MODES else "ask",
        "custom": custom,
        "danger_ask": p.get("danger_ask", True) is not False,
        "headless": p.get("headless") if p.get("headless") in ("deny", "approve") else "deny",
        "ask_timeout": _ask_timeout(p.get("ask_timeout")),
        "allow_cmds": [str(x).strip() for x in p.get("allow_cmds") or [] if str(x).strip()],
        "deny_cmds": [str(x).strip() for x in p.get("deny_cmds") or [] if str(x).strip()],
    }


def category(name: str, args: dict | None = None) -> str:
    if name == "memory":
        # 讀與寫分開算：list 只讀（讀取類），add／replace／remove 會動檔案（寫入類）。
        # 這樣「唯讀」模式或把寫入設成不允許時，模型仍然看得到記憶，只是改不動。
        return "read" if str((args or {}).get("action") or "") == "list" else "write"
    if name in TOOL_CAT:
        return TOOL_CAT[name]
    from .loader import REG
    t = REG.tools.get(name)
    plug = getattr(t, "plugin", "") if t else ""
    if plug == "mcp_client" or name.startswith("mcp_") or "__" in name:
        return "mcp"
    return "other"


def _cmd_of(name: str, args: dict) -> str:
    if name in ("terminal", "Bash"):
        return str(args.get("command") or args.get("cmd") or "")
    return ""


def _match(patterns: list[tuple[str, str]], cmd: str) -> str:
    for pat, why in patterns:
        if re.search(pat, cmd, re.I | re.M):
            return why
    return ""


def _prefix_hit(prefixes: list[str], cmd: str) -> str:
    c = " ".join(cmd.strip().split())
    # 有串接（&&、;、|）就不套白名單，免得「git status && rm -rf x」被放行
    if any(sep in c for sep in ("&&", "||", ";", "|", "`", "$(")):
        return ""
    for p in prefixes:
        q = " ".join(p.split())
        if q and (c == q or c.startswith(q + " ")):
            return p
    return ""


def _deny_hit(prefixes: list[str], cmd: str) -> str:
    c = " ".join(cmd.strip().split()).lower()
    for p in prefixes:
        q = " ".join(p.split()).lower()
        if q and q in c:
            return p
    return ""


def decide(mode: str, name: str, args: dict | None, headless: bool = False) -> dict:
    """回傳 {decision: auto|ask|deny, category, reason}。"""
    args = args or {}
    cfg = settings()
    mode = mode if mode in MODES else "auto"
    cat = category(name, args)
    table = cfg["custom"] if mode == "custom" else TABLE[mode]
    d = table.get(cat, "ask")
    reason = f"{MODES[mode][0]}：{CATS[cat][0]}{'直接執行' if d == 'auto' else '先問' if d == 'ask' else '不允許'}"
    cmd = _cmd_of(name, args)
    if cmd:
        hard = _match(HARDLINE, cmd)
        if hard:
            return {"decision": "deny", "category": cat, "reason": f"絕對禁止的指令（{hard}）", "hard": True}
        bad = _deny_hit(cfg["deny_cmds"], cmd)
        if bad:
            return {"decision": "deny", "category": cat, "reason": f"指令黑名單：{bad}"}
        if d == "ask":
            ok = _prefix_hit(cfg["allow_cmds"], cmd)
            if ok:
                d, reason = "auto", f"指令白名單：{ok}"
        if d == "auto" and cfg["danger_ask"]:
            why = _match(DANGER, cmd)
            if why:
                d, reason = "ask", f"危險指令：{why}"
    if d == "ask" and headless:
        d = "auto" if cfg["headless"] == "approve" else "deny"
        reason += "（排程等無人值守的對話，" + ("直接放行）" if d == "auto" else "沒人能確認，直接擋下）")
    return {"decision": d, "category": cat, "reason": reason}


def describe(mode: str) -> str:
    """給模型看的一句話：這個權限模式下哪些類別直接做、要等同意、會被擋。"""
    tb = settings()["custom"] if mode == "custom" else TABLE.get(mode, TABLE["auto"])
    g = {d: [CATS[c][0] for c in CATS if tb.get(c) == d] for d in DECISIONS}
    parts = [f"工具權限是「{MODES.get(mode, MODES['auto'])[0]}」"]
    if g["auto"]:
        parts.append("直接執行：" + "、".join(g["auto"]))
    if g["ask"]:
        parts.append("要等使用者按允許：" + "、".join(g["ask"]))
    if g["deny"]:
        parts.append("一律擋下，不要呼叫：" + "、".join(g["deny"]))
    return "；".join(parts) + "。"


def loosened(old: str, new: str) -> list[str]:
    """從 old 換到 new 之後，原本被擋或要確認、現在直接執行的類別名稱。"""
    def tb(m):
        return settings()["custom"] if m == "custom" else TABLE.get(m, TABLE["auto"])
    a, b = tb(old), tb(new)
    return [CATS[c][0] for c in CATS if a.get(c) in ("deny", "ask") and b.get(c) == "auto"]


def public() -> dict:
    return {"cats": CATS, "modes": MODES, "table": TABLE, "settings": settings(),
            "danger": [w for _, w in DANGER], "hardline": sorted({w for _, w in HARDLINE})}
