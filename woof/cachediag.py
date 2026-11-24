"""快取前綴比對：找出「這次請求為什麼沒吃到快取」。

服務商的快取只認完全相同的前綴（模型 → 工具清單 → 系統提示詞 → 訊息，依序），改到哪裡，那裡之後全部重算。
每次送出前，供應商插件把實際要送的內容交給 record()：算每一段的指紋，跟同一個對話上一次送的比，
找出第一個不一樣的地方。收到用量後 finish() 把結果寫進 data/logs/cache_diag.jsonl：
  - 前綴有變：寫出變在哪（換模型、工具清單、系統提示詞、第幾則訊息），以及這段期間對話有沒有壓縮、收起舊圖、清舊工具輸出
  - 前綴沒變但快取沒中：服務商那邊的原因（快取過期、分流到別台機器）
只存指紋和長度，不存內容。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import time

from .config import DATA

# 這次請求屬於哪個對話、是哪一種（loop 設定，供應商插件送出前讀）
SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar("woof_cache_sid", default=None)
KIND: contextvars.ContextVar[str] = contextvars.ContextVar("woof_cache_kind", default="turn")

LOG = DATA / "logs" / "cache_diag.jsonl"
_LAST: dict[str, dict] = {}      # 對話 → 上一次主回合請求的指紋
_PENDING: dict[str, dict] = {}   # 對話 → 這次請求的比對結果，等用量回來一起寫
MISS_MIN_TOK = 8000              # 太短的請求不記（服務商有最低快取長度）


def _h(x) -> str:
    return hashlib.sha1(json.dumps(x, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _shape(m: dict) -> dict:
    """一則訊息的輪廓：角色、欄位、圖片張數、文字長度。只拿來描述差在哪，不存內容。"""
    imgs, chars, kinds = 0, 0, []

    def walk(x):
        nonlocal imgs, chars
        if isinstance(x, dict):
            t = x.get("type")
            if t in ("image", "image_url", "input_image"):
                imgs += 1
                return
            if t:
                kinds.append(t)
            for k, v in x.items():
                if k in ("text", "content", "thinking", "reasoning_content", "output", "arguments") and isinstance(v, str):
                    chars += len(v)
                elif isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(m)
    return {"role": m.get("role"), "keys": sorted(k for k in m if k not in ("role",)), "imgs": imgs, "chars": chars,
            "kinds": kinds[:40]}


def _tool_names(tools) -> list[str]:
    out = []
    for t in tools or []:
        f = t.get("function") if isinstance(t, dict) else None
        out.append(str((f or t).get("name") if isinstance(f or t, dict) else t))
    return out


def _state(sid: str) -> dict:
    """對話裡會改到前綴的狀態：壓縮次數、舊圖收起線、清舊工具輸出到哪、摘要到哪。"""
    try:
        from . import store

        es = (store.get_session(sid) or {}).get("engine_state") or {}
        return {"compress": int(es.get("compress_count") or 0), "img_before": int(es.get("img_before") or 0),
                "prune": int((es.get("prune") or {}).get("through") or 0),
                "summary": int((es.get("summary") or {}).get("through") or 0)}
    except Exception:
        return {}


def _why_state(a: dict, b: dict) -> list[str]:
    out = []
    if a.get("compress") != b.get("compress") or a.get("summary") != b.get("summary"):
        out.append("壓縮")
    if a.get("img_before") != b.get("img_before"):
        out.append("收起舊圖")
    if a.get("prune") != b.get("prune"):
        out.append("清舊工具輸出")
    return out


def _diff_msg(a: dict, b: dict) -> str:
    """同一個位置的訊息，上次和這次差在哪。"""
    bits = []
    if a["role"] != b["role"]:
        bits.append(f"角色 {a['role']}→{b['role']}")
    gone = [k for k in a["keys"] if k not in b["keys"]]
    new = [k for k in b["keys"] if k not in a["keys"]]
    if gone:
        bits.append("少了欄位 " + ",".join(gone))
    if new:
        bits.append("多了欄位 " + ",".join(new))
    if a["imgs"] != b["imgs"]:
        bits.append(f"圖片 {a['imgs']}→{b['imgs']} 張")
    if a["chars"] != b["chars"]:
        bits.append(f"文字 {a['chars']}→{b['chars']} 字")
    if a["kinds"] != b["kinds"] and not bits:
        bits.append("區塊組成不同")
    return "；".join(bits) or "內容不同（長度一樣）"


def record(model_id: str, system, tools, messages: list, params: dict | None = None) -> dict | None:
    """送出前呼叫。回傳比對結果（也留著等 finish 寫紀錄）；不在對話裡的請求（標題、看圖等）不比。"""
    sid = SESSION.get()
    if not sid:
        return None
    try:
        kind = KIND.get()
        fp = {"model": model_id, "params": _h(params or {}), "tools": _h(tools or []), "tool_names": _tool_names(tools),
              "system": _h(system), "msgs": [_h(m) for m in messages], "shapes": [_shape(m) for m in messages],
              "state": _state(sid), "at": time.time()}
        prev = _LAST.get(sid)
        res: dict = {"sid": sid, "kind": kind, "model": model_id, "n": len(messages)}
        if prev is None:
            res["where"] = "first"
        elif prev["model"] != fp["model"]:
            res["where"], res["detail"] = "model", f"{prev['model']}→{fp['model']}"
        elif prev["tools"] != fp["tools"]:
            a, b = prev["tool_names"], fp["tool_names"]
            add, rm = [x for x in b if x not in a], [x for x in a if x not in b]
            d = []
            if add:
                d.append("多了 " + ",".join(add))
            if rm:
                d.append("少了 " + ",".join(rm))
            if not d:
                d.append("順序或說明變了")
            res["where"], res["detail"] = "tools", "；".join(d)
        elif prev["system"] != fp["system"]:
            res["where"] = "system"
        elif prev["params"] != fp["params"]:
            res["where"], res["detail"] = "params", "思考強度或請求參數變了"
        else:
            pm, cm = prev["msgs"], fp["msgs"]
            i = next((k for k in range(min(len(pm), len(cm))) if pm[k] != cm[k]), None)
            if i is None and len(cm) < len(pm):
                i = len(cm)
            if i is None:
                res["where"] = "ok"
            elif i >= len(pm) - 1 and i < len(cm):
                # 只改到上一次請求的最後一則（例如便條拿掉、插話併進同一則）：快取找得到前一個斷點，影響很小
                res["where"], res["idx"] = "tail", i
            else:
                res["where"], res["idx"] = "messages", i
                res["detail"] = (_diff_msg(prev["shapes"][i], fp["shapes"][i]) if i < len(cm)
                                 else f"訊息變少 {len(pm)}→{len(cm)} 則")
                res["of"] = len(pm)
        why = _why_state(prev["state"], fp["state"]) if prev else []
        if why:
            res["because"] = why
        if prev:
            res["gap_s"] = round(fp["at"] - prev["at"], 1)
        if kind == "turn":
            _LAST[sid] = fp
        _PENDING[sid] = res
        return res
    except Exception:
        return None


def finish(sid: str, input_tokens, cache_read) -> None:
    """收到用量後呼叫：前綴有變，或前綴沒變但快取沒中，寫一筆紀錄。"""
    res = _PENDING.pop(sid, None)
    if not res:
        return
    try:
        inp, hit = int(input_tokens or 0), int(cache_read or 0)
        res["in"], res["cache"] = inp, hit
        changed = res["where"] in ("model", "tools", "system", "params", "messages")
        missed = res["where"] in ("ok", "tail") and inp >= MISS_MIN_TOK and hit < inp * 0.5
        if missed:
            res["where"] = "provider"
            res["detail"] = "前綴跟上次一樣，服務商卻沒命中（快取過期或分流到別台機器）"
        if not (changed or missed):
            return
        res["ts"] = round(time.time(), 1)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2_000_000:   # 只留最近的
            LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-2000:]) + "\n", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    except Exception:
        pass


def forget(sid: str) -> None:
    _LAST.pop(sid, None)
    _PENDING.pop(sid, None)
