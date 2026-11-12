"""排程工作：照 cron 表達式定時把一段提示詞送給模型跑一回合。

工作存在 data/cron.json。每次執行開一個新對話（標題前面加 ⏱），結果就在左側對話清單裡。
排程語法：標準 5 欄 cron（分 時 日 月 週），支援 * 、*/n、a-b、a-b/n、逗號清單；週 0 或 7 都是週日。
WOOF 沒開的時候錯過的執行，依 catch_up 決定開啟後補跑一次（run）或略過（skip，預設）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .config import DATA

log = logging.getLogger("woof.cron")
FILE = DATA / "cron.json"
WEEK = ["日", "一", "二", "三", "四", "五", "六"]
PRESETS = {
    "every15": "*/15 * * * *",
    "hourly": "0 * * * *",
    "daily": "0 9 * * *",
    "weekly": "0 9 * * 1",
    "monthly": "0 9 1 * *",
}
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
NAMES = ["分", "時", "日", "月", "週"]


# ------------------------------------------------------------------ 解析
def _field(text: str, lo: int, hi: int, name: str) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"「{name}」欄有空白項")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f"「{name}」欄的間隔 /{s} 不對")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise ValueError(f"「{name}」欄的範圍 {part} 不對")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            raise ValueError(f"「{name}」欄看不懂：{part}")
        if a > b:
            raise ValueError(f"「{name}」欄的範圍要由小到大：{part}")
        if a < lo or b > hi:
            raise ValueError(f"「{name}」欄要在 {lo}–{hi} 之間：{part}")
        out.update(range(a, b + 1, step))
    return out


def parse(expr: str) -> list[set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("要 5 欄：分 時 日 月 週，例如 0 9 * * 1-5")
    f = [_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, RANGES, NAMES)]
    if 7 in f[4]:
        f[4] = (f[4] - {7}) | {0}
    return f


def next_after(expr: str, after: float) -> float:
    """after 之後（不含）的下一個觸發時間（本機時間）。日與週都有限定時，照 cron 慣例取聯集。"""
    mi, hr, dom, mon, dow = parse(expr)
    p = expr.split()
    dom_any, dow_any = p[2] == "*", p[4] == "*"
    t = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = t + timedelta(days=366 * 5)
    while t < end:
        if t.month not in mon:
            t = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            continue
        wd = (t.weekday() + 1) % 7
        day_ok = (t.day in dom) if dow_any else (wd in dow) if dom_any else (t.day in dom or wd in dow)
        if not day_ok:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hr:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in mi:
            t += timedelta(minutes=1)
            continue
        return t.timestamp()
    raise ValueError("這個排程五年內都不會觸發")


def describe(expr: str) -> str:
    """把常見的表達式翻成中文；看不懂的就原樣顯示。"""
    p = (expr or "").split()
    if len(p) != 5:
        return expr
    m, h, d, mo, w = p
    if m.startswith("*/") and h == d == mo == w == "*":
        return f"每 {m[2:]} 分鐘"
    if m == "*" and h == d == mo == w == "*":
        return "每分鐘"
    if not (m.isdigit()):
        return expr
    if h == "*" and d == mo == w == "*":
        return "每個整點" if m == "0" else f"每小時的 :{int(m):02d}"
    if h.startswith("*/") and d == mo == w == "*":
        return f"每 {h[2:]} 小時（:{int(m):02d}）"
    if not h.isdigit():
        return expr
    tm = f"{int(h):02d}:{int(m):02d}"
    if d == mo == "*" and w == "*":
        return f"每天 {tm}"
    if d == mo == "*" and w == "1-5":
        return f"工作日 {tm}"
    if d == mo == "*" and w in ("0,6", "6,0"):
        return f"週末 {tm}"
    if d == mo == "*" and w.isdigit():
        return f"每週{WEEK[int(w) % 7]} {tm}"
    if mo == "*" and w == "*" and d.isdigit():
        return f"每月 {int(d)} 日 {tm}"
    return expr


# ------------------------------------------------------------------ 儲存
def load() -> list[dict]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8")).get("jobs", [])
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("cron.json 讀取失敗")
        return []


def save(jobs: list[dict]) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def public(j: dict) -> dict:
    out = {k: v for k, v in j.items() if k != "history"}
    return {**out, "label": describe(j["schedule"]), "running": j["id"] in RUNNING,
            "running_session": RUN_SID.get(j["id"])}


HISTORY_MAX = 30


def _push_history(j: dict, ent: dict) -> None:
    h = list(j.get("history") or [])
    h.insert(0, ent)
    j["history"] = h[:HISTORY_MAX]


def history(jid: str) -> list[dict] | None:
    j = next((x for x in load() if x["id"] == jid), None)
    return None if j is None else list(j.get("history") or [])


def normalize(body: dict, old: dict | None = None) -> dict:
    j = dict(old or {})
    for k in ("name", "prompt", "schedule", "model", "effort", "catch_up", "enabled"):
        if k in body:
            j[k] = body[k]
    j["name"] = str(j.get("name") or "").strip()[:80]
    j["prompt"] = str(j.get("prompt") or "").strip()
    j["schedule"] = " ".join(str(PRESETS.get(j.get("schedule"), j.get("schedule")) or "").split())
    if not j["prompt"]:
        raise ValueError("提示詞是必填")
    if not j["schedule"]:
        raise ValueError("排程是必填")
    parse(j["schedule"])
    j["name"] = j["name"] or j["prompt"].splitlines()[0][:30]
    j["model"] = str(j.get("model") or "")
    j["effort"] = str(j.get("effort") or "")
    j["catch_up"] = "run" if j.get("catch_up") == "run" else "skip"
    j["enabled"] = j.get("enabled") is not False
    j.setdefault("id", uuid.uuid4().hex[:10])
    j.setdefault("created", time.time())
    for k in ("last_run", "last_status", "last_session", "last_error"):
        j.setdefault(k, None)
    j["runs"] = int(j.get("runs") or 0)
    if not old or old.get("schedule") != j["schedule"] or not j.get("next_run") or (j["enabled"] and not old.get("enabled")):
        j["next_run"] = next_after(j["schedule"], time.time())
    return j


async def add(body: dict) -> dict:
    j = normalize(body)
    async with _lock:
        jobs = load()
        jobs.append(j)
        save(jobs)
    await _changed()
    return j


async def edit(jid: str, body: dict) -> dict | None:
    async with _lock:
        jobs = load()
        i = next((k for k, j in enumerate(jobs) if j["id"] == jid), None)
        if i is None:
            return None
        jobs[i] = normalize(body, jobs[i])
        save(jobs)
    await _changed()
    return jobs[i]


async def remove(jid: str) -> bool:
    async with _lock:
        jobs = load()
        keep = [j for j in jobs if j["id"] != jid]
        save(keep)
    await _changed()
    return len(keep) != len(jobs)


# ------------------------------------------------------------------ 執行
Runner = Callable[[dict], Awaitable[tuple[str, str, str]]]  # 回傳 (session_id, status, error)
RUNNING: set[str] = set()
RUN_SID: dict[str, str] = {}  # 正在跑的排程 → 它開的對話（停止、開啟用）
_runner: Runner | None = None
_notify: Callable[[dict], Awaitable[None]] | None = None
_lock = asyncio.Lock()


async def set_session(jid: str, sid: str) -> None:
    """執行器開好對話後回報，介面就能顯示「執行中 → 開啟對話／停止」。"""
    RUN_SID[jid] = sid
    await _changed()


async def run_now(jid: str, trigger: str = "schedule") -> None:
    job = next((j for j in load() if j["id"] == jid), None)
    if not job or jid in RUNNING or not _runner:
        return
    RUNNING.add(jid)
    await _changed()
    started = time.time()
    sid, status, err = "", "error", ""
    try:
        sid, status, err = await _runner(job)
    except Exception as e:
        log.exception("排程 %s 執行失敗", job.get("name"))
        err = f"{type(e).__name__}: {e}"
    finally:
        RUNNING.discard(jid)
        RUN_SID.pop(jid, None)
    ent = {"at": started, "end": time.time(), "status": status, "session": sid or None,
           "error": err or None, "trigger": trigger}
    async with _lock:
        jobs = load()
        for j in jobs:
            if j["id"] == jid:
                j.update(last_run=started, last_status=status, last_session=sid or None,
                         last_error=err or None, runs=int(j.get("runs") or 0) + 1)
                _push_history(j, ent)
                save(jobs)
    await _changed()


async def _changed() -> None:
    if _notify:
        try:
            await _notify({"type": "cron_changed"})
        except Exception:
            pass


async def loop(runner: Runner, notify) -> None:
    """每 20 秒看一次有沒有到期的工作。啟動時處理錯過的執行。"""
    global _runner, _notify
    _runner, _notify = runner, notify
    first = True
    while True:
        now = time.time()
        due: list[str] = []
        async with _lock:
            jobs, dirty = load(), False
            for j in jobs:
                if not j.get("enabled") or not j.get("next_run"):
                    continue
                if j["next_run"] <= now:
                    late = now - j["next_run"]
                    # 開機時發現錯過（或睡眠醒來晚了超過 2 分鐘）：照 catch_up 決定
                    if (first or late > 120) and j.get("catch_up") != "run":
                        log.info("排程 %s 錯過 %s，略過", j["name"], datetime.fromtimestamp(j["next_run"]))
                        _push_history(j, {"at": j["next_run"], "status": "skipped", "session": None,
                                          "error": None, "trigger": "schedule"})
                    else:
                        due.append(j["id"])
                    try:
                        j["next_run"] = next_after(j["schedule"], now)
                    except ValueError:
                        j["enabled"] = False
                    dirty = True
            if dirty:
                save(jobs)
        for jid in due:
            asyncio.create_task(run_now(jid))
        if due or (first and jobs):
            await _changed()
        first = False
        await asyncio.sleep(20)
