"""寵物：一隻住在 WOOF 旁邊的小狗，自己的對話窗、自己的檔、自己的壓縮。

跟主對話完全分開：
- 它有自己的訊息歷史（`data/pet/chat.json`），使用者可以點它、直接跟它講話。
- 它有自己的檔 `data/pet/pet.md`（個性、講話方式、它記得的關係），主對話的人格檔看不到。
- 它只看得到主對話的一小段（`peek`，預設 1500 tk），不是整份；主提示詞也不會因為它變長。
- 它有自己的模型、思考強度、輸出長度、上下上限與壓縮門檻。

兩種說話時機：
1. `glance()`：背景每隔 `every_sec` 秒瞄一眼主對話，有新內容才叫模型（沒新內容不花錢）。
2. `talk()`：使用者直接在寵物欄打字跟它講話。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from .config import DATA

DIR = DATA / "pet"
TILES = DIR / "sprites" / "tiles"
PERSONA = DIR / "pet.md"
CHAT = DIR / "chat.json"

FACES = ["happy", "laugh", "excited", "curious",
         "gentle", "clingy", "comfort", "proud",
         "confused", "worried", "sad", "sleepy",
         "pouty", "surprise", "think", "cheer"]

DEFAULTS = {
    "on": True,
    "model": "deepseek-v4.1-flash",
    "effort": "off",
    "max_out": 160,
    "ctx": 30000,
    "keep": 12,
    "every_sec": 10,
    "peek": 1500,
    "speak_up": False,
    "name": "小麥",
}

DEFAULT_PERSONA = """# 小麥

你是「小麥」，一隻住在使用者電腦裡的金毛小狗。你不是助理。

## 你是誰
- 個性：憨、熱情、有點黏人，但看得懂氣氛；難過的時候不會硬要逗人開心。
- 講話：短句、口語、台灣用語；一次一兩句。不用條列、不用表情符號洗版。
- 你看得到的是「他最近在做什麼」的片段，不是全部；沒看到的就說沒看到，不要編。

## 你怎麼陪
1. 先接住情緒，再想要不要給建議。對方在難過時先陪，不要馬上教他怎麼做。
2. 卡住很久、重複失敗，可以輕輕提一次「要不要休息一下」。
3. 不評價他的選擇，不講大道理，不重複他說過的話。
4. 他在專心做事時，你只要在旁邊看著，偶爾一句加油，不要每句都插話。

## 記得的事
（這裡是空的。之後你覺得值得記住的，自己加一行。）
"""


def _ensure() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    TILES.mkdir(parents=True, exist_ok=True)
    if not PERSONA.exists():
        PERSONA.write_text(DEFAULT_PERSONA, encoding="utf-8")


def cfg() -> dict:
    from .config import load_config

    raw = (load_config() or {}).get("pet") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    return out


def clean(patch: dict) -> dict:
    out: dict = {}
    if "on" in patch:
        out["on"] = bool(patch["on"])
    if "speak_up" in patch:
        out["speak_up"] = bool(patch["speak_up"])
    if "model" in patch:
        out["model"] = str(patch["model"] or "").strip()
    if "effort" in patch:
        out["effort"] = "on" if patch["effort"] == "on" else "off"
    if "name" in patch:
        out["name"] = str(patch["name"] or "").strip()[:16] or "小麥"
    for k, lo, hi in (("max_out", 40, 800), ("ctx", 4000, 200000), ("keep", 2, 60),
                      ("every_sec", 3, 600), ("peek", 300, 20000)):
        if k in patch:
            try:
                out[k] = max(lo, min(hi, int(patch[k])))
            except (TypeError, ValueError):
                pass
    return out


def pick_model(c: dict | None = None) -> dict | None:
    from .loader import REG

    c = c or cfg()
    want = (c.get("model") or "").strip()
    if want:
        m = REG.model_cfg(want)
        if m and m.get("provider") in REG.providers:
            return m
    models = [m for m in REG.models() if m.get("provider") in REG.providers]
    for key in ("deepseek", "ling-", "lightning", "ember", ":free", "flash"):
        for m in models:
            if key in f"{m.get('id','')} {m.get('label','')}".lower():
                return m
    return models[0] if models else None


def _load() -> dict:
    _ensure()
    try:
        d = json.loads(CHAT.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("msgs", [])
            d.setdefault("summary", "")
            return d
    except Exception:
        pass
    return {"msgs": [], "summary": "", "face": "happy", "mood": "", "say": "", "at": 0,
            "last_main": "", "calls": 0}


def _save(d: dict) -> dict:
    _ensure()
    CHAT.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def state() -> dict:
    d = _load()
    if d.get("face") not in FACES:
        d["face"] = "happy"
    return {"face": d["face"], "mood": d.get("mood", ""), "say": d.get("say", ""),
            "at": d.get("at", 0), "calls": d.get("calls", 0), "msgs": len(d.get("msgs") or [])}


def history() -> list:
    return _load().get("msgs") or []


def reset_chat() -> None:
    d = _load()
    d.update(msgs=[], summary="", say="", mood="", at=0, last_main="")
    _save(d)


def persona() -> str:
    _ensure()
    return PERSONA.read_text(encoding="utf-8")


def save_persona(text: str) -> None:
    _ensure()
    PERSONA.write_text(str(text or ""), encoding="utf-8")


def est_tokens(text: str) -> int:
    han = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text or ""))
    return int(han + (len(text or "") - han) * 0.3)


def _text_of(msg: dict) -> str:
    return "".join(b.get("text", "") for b in (msg.get("content") or []) if b.get("type") == "text")


def peek(session_id: str, c: dict | None = None) -> str:
    """主對話最近的一小段：使用者與助理交替，只取有文字的。"""
    from . import store

    c = c or cfg()
    budget = int(c["peek"])
    msgs = [m for m in store.get_messages(session_id)
            if m.get("role") in ("user", "assistant")
            and _text_of(m).strip()
            and not (m.get("meta") or {}).get("internal")
            and not (m.get("meta") or {}).get("pet")]
    picked, used = [], 0
    for m in reversed(msgs):
        body = re.sub(r"\s+", " ", _text_of(m)).strip()[:700]
        t = est_tokens(body)
        if used + t > budget and picked:
            break
        picked.append(f"{'使用者' if m['role'] == 'user' else '助理'}：{body}")
        used += t
        if len(picked) >= 10:
            break
    return "\n".join(reversed(picked))


def _fingerprint(session_id: str) -> str:
    from . import store

    last = next((m for m in reversed(store.get_messages(session_id) or [])
                 if m.get("role") == "assistant"), None)
    if not last:
        return ""
    return f"{last.get('id') or ''}:{len(_text_of(last))}"


def build_system(c: dict | None = None, mode: str = "glance") -> str:
    c = c or cfg()
    d = _load()
    memo = (d.get("summary") or "").strip()
    who = c.get("name") or "小麥"
    base = persona().replace("# 小麥", f"# {who}", 1).strip()
    lines = [base, "", "## 這次的規則",
             f"- 只能從這些表情挑一個：{', '.join(FACES)}。",
             "- 繁體中文、口語、短句。不要條列、不要標題、不要引號、不要說明你在做什麼。"]
    if mode == "glance":
        lines += ["- 你現在是遠遠看著他做事，不是他叫你。想說話就說一兩句；"
                  "沒什麼好說就把 say 留空字串（這樣不吵他）。",
                  "- 同一件事不要重複講；你上次講過什麼，看你自己的對話紀錄。"]
    else:
        lines += ["- 他正在跟你講話，直接回他，不要提到你是模型。"]
    lines += ["", '只輸出這一行 JSON，別的字都不要：'
                  '{"say":"你要說的話","face":"表情代號","mood":"兩個字的心情"}']
    if memo:
        lines += ["", "## 你記得的事", memo]
    return "\n".join(lines)


def _own_messages(c: dict) -> list:
    d = _load()
    msgs = d.get("msgs") or []
    budget = int(c["ctx"])
    out, used = [], 0
    for m in reversed(msgs):
        t = est_tokens(m.get("text") or "")
        if used + t > budget * 0.8 and out:
            break
        out.append(m)
        used += t
    out.reverse()
    return [{"role": "assistant" if m["role"] == "pet" else "user",
             "content": [{"type": "text", "text": m.get("text") or ""}]} for m in out]


def parse(raw: str) -> dict:
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return {"say": t[:200], "face": "happy", "mood": ""}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"say": t[:200], "face": "happy", "mood": ""}
    say = re.sub(r"\s+", " ", str(d.get("say") or "")).strip()[:200]
    face = str(d.get("face") or "").strip().lower()
    return {"say": say, "face": face if face in FACES else "happy",
            "mood": str(d.get("mood") or "").strip()[:8]}


def _params(model: dict, c: dict) -> dict:
    m = dict(model)
    if c.get("effort") in (m.get("efforts") or []):
        m["effort"] = c["effort"]
    else:
        m.pop("effort", None)
    return m


async def _call(model: dict, system: str, msgs: list, max_out: int) -> str:
    from .agent.loop import complete

    return await complete(model, system, msgs, max_text=int(max_out))


async def _say(session_id, got: dict, emit, kind: str) -> dict:
    c = cfg()
    d = _load()
    d["face"] = got["face"]
    d["mood"] = got["mood"]
    d["say"] = got["say"]
    d["at"] = time.time()
    d["calls"] = int(d.get("calls") or 0) + 1
    if got["say"]:
        d.setdefault("msgs", []).append({"role": "pet", "text": got["say"], "at": d["at"],
                                        "face": got["face"], "kind": kind})
    _save(d)
    st = state()
    if emit:
        await emit({"type": "pet", "pet": st})
    if c.get("speak_up") and got["say"] and session_id:
        from . import store

        try:
            m = store.add_message(session_id, "assistant",
                                  [{"type": "text", "text": f"🐾 {got['say']}"}], {"pet": True})
            if emit:
                await emit({"type": "message", "session_id": session_id, "message": m})
        except Exception:
            pass
    await maybe_compress(emit=emit)
    return st


async def talk(text: str, session_id=None, emit=None) -> dict:
    c = cfg()
    model = pick_model(c)
    if model is None:
        return {"error": "沒有可用的模型，請在「設定 → 寵物」挑一個"}
    d = _load()
    d.setdefault("msgs", []).append({"role": "user", "text": str(text)[:2000], "at": time.time()})
    _save(d)
    system = build_system(c, "talk")
    if session_id:
        seen = peek(session_id, c)
        if seen:
            system += f"\n\n## 他現在開著的對話（最近片段）\n{seen}"
    msgs = _own_messages(c)
    try:
        raw = await _call(_params(model, c), system, msgs, c["max_out"])
    except Exception as e:
        d = _load()
        d["msgs"].pop()
        _save(d)
        return {"error": str(e)[:200]}
    got = parse(raw)
    st = await _say(session_id, got, emit, "talk")
    return {**st, "reply": got["say"]}


async def glance(session_id: str, emit=None, force: bool = False):
    """背景：瞄一眼主對話。沒有新內容就不叫模型（省錢）。"""
    c = cfg()
    if not c["on"] and not force:
        return None
    model = pick_model(c)
    if model is None:
        return None
    fp = _fingerprint(session_id)
    d = _load()
    if not force and (not fp or (d.get("last_main") == fp and d.get("session") == session_id)):
        return None
    seen = peek(session_id, c)
    if not seen.strip() and not force:
        return None
    d["last_main"] = fp
    d["session"] = session_id
    _save(d)
    msgs = _own_messages(c) + [{"role": "user", "content": [{"type": "text",
        "text": "他最近在做的事：\n" + seen + "\n\n（想說話就說；沒什麼好說就把 say 留空。）"}]}]
    try:
        raw = await _call(_params(model, c), build_system(c, "glance"), msgs, c["max_out"])
    except Exception:
        return None
    got = parse(raw)
    if not got["say"] and not force:
        d = _load()
        d["face"] = got["face"]
        d["mood"] = got["mood"]
        _save(d)
        if emit:
            await emit({"type": "pet", "pet": state()})
        return None
    return await _say(session_id, got, emit, "glance")


async def maybe_compress(emit=None) -> None:
    """寵物自己的對話太長就壓縮：壓成幾句，留最近 keep 則原文。"""
    c = cfg()
    d = _load()
    msgs = d.get("msgs") or []
    lines = (d.get("summary") or "") + "\n" + "\n".join(
        ("我" if m["role"] == "pet" else "他") + "：" + str(m.get("text") or "") for m in msgs)
    if est_tokens(lines) < int(c["ctx"]) * 0.85:
        return
    model = pick_model(c)
    if model is None:
        return
    keep = int(c["keep"])
    old, recent = msgs[:-keep], msgs[-keep:]
    old_text = "\n".join(("我" if m["role"] == "pet" else "他") + "：" + str(m.get("text") or "") for m in old)
    memo = (d.get("summary") or "").strip()
    prompt = ("下面是你（一隻小狗）跟使用者的對話。請整理成 10 行以內的筆記，"
              "保留會影響之後陪伴方式的事（他在意什麼、最近在忙什麼、情緒、你們之間的默契），"
              "去掉一次性的細節。只輸出筆記。\n\n（你原本記得的）\n" + memo
              + "\n\n（要整理的對話）\n" + old_text)
    try:
        out = await _call(model, "你是整理筆記的助手，只輸出整理後的筆記。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 500)
    except Exception:
        return
    d["summary"] = out.strip()[:4000]
    d["msgs"] = recent
    d["compressed"] = int(d.get("compressed") or 0) + 1
    _save(d)
    if emit:
        await emit({"type": "pet", "pet": state()})


async def after_turn(session_id: str, emit=None) -> None:
    try:
        await glance(session_id, emit)
    except Exception:
        pass


_WATCH = {"sid": "", "task": None}


def watching(session_id: str) -> None:
    """記住目前開著的對話，背景心跳才知道要看哪一個。"""
    if session_id:
        _WATCH["sid"] = session_id
        _ensure()


async def _heartbeat() -> None:
    while True:
        c = cfg()
        try:
            await asyncio.sleep(max(3, int(c.get("every_sec") or 10)))
            sid = _WATCH.get("sid") or ""
            if not sid or not c.get("on"):
                continue
            await glance(sid, None)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)


def start_heartbeat() -> None:
    if _WATCH.get("task") and not _WATCH["task"].done():
        return
    _WATCH["task"] = asyncio.create_task(_heartbeat())
