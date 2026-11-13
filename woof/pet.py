"""寵物：住在 WOOF 旁邊的角色，自己的對話窗、自己的檔、自己的壓縮。

跟主對話完全分開：
- 每個角色有自己的訊息歷史（`data/pet/chat-<角色>.json`），可以直接跟牠講話。
- 每個角色有自己的檔（`data/pet/chars/<角色>.md`）：只寫「牠是誰」。
  怎麼聊（長度、連發、表情反應、輸出格式）是共用規則，寫在 build_system。
- 牠只看得到主對話的一小段（`peek`，預設 1500 tk），不是整份。
- 牠有自己的模型、思考強度、輸出長度、上下文上限與壓縮門檻。

聊天的樣子照通訊軟體：
- 回覆長度跟著使用者這句走（程式先算好，再告訴模型），大多數時候一兩句。
- 一次可以連發兩三則短訊息（say 是陣列）。
- 雙方都能對對方的訊息按表情（同一組表情）；只按表情、不說話也是一種回覆。

表情代號固定 16 個；圖在 `web/dist/pet/<角色>/<表情>.webp`，
`data/pet/sprites/chars/<角色>/<表情>.webp` 有同名檔案會蓋過內建的。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid

from .config import DATA, WEB_DIST
from .pet_personas import PERSONAS
from .zh_tw import to_tw

DIR = DATA / "pet"
CHARS_DIR = DIR / "chars"
OVERRIDE = DIR / "sprites" / "chars"
DEFAULTS_MARK = CHARS_DIR / ".defaults.json"

FACES = ["happy", "laugh", "excited", "curious",
         "gentle", "clingy", "comfort", "proud",
         "confused", "worried", "sad", "sleepy",
         "pouty", "surprise", "think", "cheer"]

# 角色：名字、設定頁的一句話、招牌表情（會加進表情列的最後一顆）
CHARS = {
    "dog": {"name": "沃夫", "note": "黃金獵犬。可愛、黏人、很好哄。", "sig": "🐾"},
    "fish": {"name": "大肥魚", "note": "DeepSeek 藍鯨女僕。慵懶、貪吃、嘴硬心軟。", "sig": "🍚"},
}

# 雙方共用的表情列（像 Telegram 的快速反應），最後再補上角色的招牌表情
REACTS = ["❤️", "😂", "🥺", "👍", "😮", "🔥", "😴"]

# 使用者對牠的訊息按表情 → 牠當下的表情與心情（不叫模型，立刻反應）
FEEL = {"❤️": ("clingy", "害羞"), "😂": ("laugh", "得意"), "🥺": ("comfort", "心疼"),
        "👍": ("proud", "得意"), "😮": ("surprise", "嚇到"), "🔥": ("cheer", "興奮"),
        "😴": ("sleepy", "想睡"), "🐾": ("excited", "搖尾巴"), "🍚": ("happy", "有飯吃")}

# 牠什麼都沒說也沒按的時候，依表情補一顆反應，不要讓使用者覺得被已讀不回
FACE_REACT = {"laugh": "😂", "comfort": "🥺", "sad": "🥺", "worried": "🥺", "surprise": "😮",
              "cheer": "🔥", "excited": "🔥", "proud": "👍", "sleepy": "😴"}

DEFAULTS = {
    "on": True,
    "char": "dog",
    "model": "deepseek-v4.1-flash",
    "effort": "off",
    "max_out": 300,
    "ctx": 30000,
    "keep": 12,
    "every_sec": 10,
    "peek": 1500,
    "speak_up": False,
    "quick": ["怎麼了？", "繼續說"],   # 固定的通用回覆，永遠在
    "react_rate": "low",              # 牠說話時順便按表情的頻率：off／low／high
    "mode": "mix",                    # heart 情感／mix 混合／work 工作
    "nudge": "some",                  # 看時間自己找你：off／rare／some／often
    "quiet_from": 23,                 # 這段時間不主動找你（小時，跨午夜）
    "quiet_to": 8,
}

# 三種模式：情感＝不看對話純聊；混合＝看對話也聊心情；工作＝看對話、只聊工作，人設照舊
MODES = {
    "heart": {"label": "情感", "peek": False, "rules": [
        "- 現在是情感模式：你看不到他在做什麼，你們就是單純聊天、陪伴。",
        "- 可以聊心情、生活、天氣、吃什麼、你們之間的事；不要問工作進度，不要給技術建議。",
    ]},
    "mix": {"label": "混合", "peek": True, "rules": []},
    "work": {"label": "工作", "peek": True, "rules": [
        "- 現在是工作模式：只聊他手上的事（進度、卡在哪、下一步、做完了沒、該不該休息一下）。",
        "- 不撒嬌、不討拍、不說想他、不聊感情和心情；你的口頭禪、語氣、說話方式照舊。",
        "- 他自己跑來講心情時，簡短接一句就好，不要展開。",
    ]},
}

# 看時間自己找你（參考 Nomi／Kindroid）：沉默多久才找、一天最多幾次。
# 每多一則沒回，間隔加倍；連續三則沒回就不再找，等他先開口。
NUDGE = {"off": None, "rare": (12 * 3600, 1), "some": (3 * 3600, 4), "often": (3600, 8)}
NUDGE_MAX_UNANSWERED = 3

# 說話時順便按表情：最近幾句你的話裡牠按過一次，就先不按（只按表情不說話的回覆不受限）
REACT_GAP = {"off": 10 ** 6, "low": 4, "high": 2}


def _sha(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _ensure() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    CHARS_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDE.mkdir(parents=True, exist_ok=True)
    # 舊版只有一隻狗：把 pet.md／chat.json 搬進角色目錄
    old_p, old_c = DIR / "pet.md", DIR / "chat.json"
    new_p, new_c = CHARS_DIR / "dog.md", DIR / "chat-dog.json"
    if old_p.exists() and not new_p.exists():
        old_p.replace(new_p)
    if old_c.exists() and not new_c.exists():
        old_c.replace(new_c)
    # 角色檔：沒有就建；使用者沒改過（內容跟上次寫進去的預設一樣）就跟著新版預設更新
    try:
        mark = json.loads(DEFAULTS_MARK.read_text(encoding="utf-8"))
    except Exception:
        mark = {}
    changed = False
    for cid, text in PERSONAS.items():
        f = CHARS_DIR / ("%s.md" % cid)
        cur = f.read_text(encoding="utf-8") if f.exists() else None
        if cur is None or (mark.get(cid) and _sha(cur) == mark[cid] and cur != text):
            f.write_text(text, encoding="utf-8")
            cur = text
        if cur == text and mark.get(cid) != _sha(text):
            mark[cid] = _sha(text)
            changed = True
    if changed:
        DEFAULTS_MARK.write_text(json.dumps(mark), encoding="utf-8")


def cfg() -> dict:
    from .config import load_config

    raw = (load_config() or {}).get("pet") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    if out.get("char") not in CHARS:
        out["char"] = "dog"
    if out.get("mode") not in MODES:
        out["mode"] = "mix"
    if out.get("nudge") not in NUDGE:
        out["nudge"] = "some"
    out["every_sec"] = max(3, min(120, int(out.get("every_sec") or 10)))
    out["peek"] = max(300, min(8000, int(out.get("peek") or 1500)))
    return out


def cid() -> str:
    return str(cfg()["char"])


def name_of(c: dict | None = None) -> str:
    c = c or cfg()
    return CHARS.get(str(c.get("char")), CHARS["dog"])["name"]


def palette(c_id: str | None = None) -> list:
    ch = CHARS.get(c_id or cid(), CHARS["dog"])
    return REACTS + [ch["sig"]]


def chars() -> list:
    return [{"id": k, "name": v["name"], "note": v["note"], "reacts": palette(k)} for k, v in CHARS.items()]


def persona_path(c: str | None = None):
    return CHARS_DIR / ("%s.md" % (c or cid()))


def chat_path(c: str | None = None):
    return DIR / ("chat-%s.json" % (c or cid()))


def sprite(face: str, c: str | None = None):
    c = c or cid()
    for f in (OVERRIDE / c / ("%s.webp" % face), WEB_DIST / "pet" / c / ("%s.webp" % face)):
        if f.exists():
            return f
    return None


def clean(patch: dict) -> dict:
    out: dict = {}
    if "on" in patch:
        out["on"] = bool(patch["on"])
    if "speak_up" in patch:
        out["speak_up"] = bool(patch["speak_up"])
    if "model" in patch:
        out["model"] = str(patch["model"] or "").strip()
    if "char" in patch:
        v = str(patch["char"] or "")
        if v in CHARS:
            out["char"] = v
    if "effort" in patch:
        from .effort import SCALE, norm as _norm

        v = _norm(str(patch["effort"] or ""))
        out["effort"] = v if v in SCALE or v in ("off", "on", "none") else "off"
    if "react_rate" in patch and patch["react_rate"] in REACT_GAP:
        out["react_rate"] = patch["react_rate"]
    if "mode" in patch and patch["mode"] in MODES:
        out["mode"] = patch["mode"]
    if "nudge" in patch and patch["nudge"] in NUDGE:
        out["nudge"] = patch["nudge"]
    if "quick" in patch:
        q = patch["quick"]
        if isinstance(q, str):
            q = re.split(r"[\n|｜]", q)
        if isinstance(q, list):
            vals = [str(x).strip()[:20] for x in q if str(x).strip()][:4]
            if vals:
                out["quick"] = vals
    for k, lo, hi in (("max_out", 40, 800), ("ctx", 4000, 200000), ("keep", 2, 60),
                      ("every_sec", 3, 120), ("peek", 300, 8000), ("quiet_from", 0, 23), ("quiet_to", 0, 23)):
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


# ---------------------------------------------------------------- 對話紀錄

def _mid() -> str:
    return uuid.uuid4().hex[:10]


def _load(c: str | None = None) -> dict:
    _ensure()
    f = chat_path(c)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("msgs", [])
            d.setdefault("summary", "")
            for m in d["msgs"]:
                if not m.get("id"):
                    m["id"] = _mid()
            return d
    except Exception:
        pass
    return {"msgs": [], "summary": "", "face": "happy", "mood": "", "say": "", "sug": [],
            "at": 0, "last_main": "", "calls": 0}


def _save(d: dict, c: str | None = None) -> dict:
    _ensure()
    chat_path(c).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def state() -> dict:
    d = _load()
    c = cid()
    if d.get("face") not in FACES:
        d["face"] = "happy"
    return {"char": c, "name": name_of(), "face": d["face"], "mood": d.get("mood", ""),
            "say": d.get("say", ""), "sug": (d.get("sug") or [])[:1], "react": d.get("react", ""),
            "reacts": palette(c), "at": d.get("at", 0),
            "calls": d.get("calls", 0), "msgs": len(d.get("msgs") or []), "total": len(d.get("arch") or []) + len(d.get("msgs") or []),
            "mode": cfg()["mode"], "next_nudge": nudge_plan().get("at", 0),
            "summary": (d.get("summary") or "")[:400]}


ARCH_MAX = 3000   # 壓縮掉的舊紀錄最多留幾則給畫面往上捲（不再送給模型）


def history() -> list:
    """畫面用的完整紀錄：壓縮掉的舊紀錄（old=True，只給你看、牠已經不記得原文）＋目前的紀錄。"""
    d = _load()
    return [{**m, "old": True} for m in d.get("arch") or []] + (d.get("msgs") or [])


def history_before(before: str, limit: int = 60) -> dict:
    """往上捲時補更早的紀錄：回 before 那則之前的 limit 則，more＝再往上還有沒有。"""
    h = history()
    i = next((k for k, m in enumerate(h) if m.get("id") == before), len(h)) if before else len(h)
    lo = max(0, i - max(1, min(int(limit or 60), 200)))
    return {"items": h[lo:i], "more": lo > 0}


def reset_chat() -> None:
    d = _load()
    d.update(msgs=[], arch=[], summary="", say="", mood="", sug=[], react="", at=0, last_main="")
    _save(d)
    gc_att()


def persona(c: str | None = None) -> str:
    _ensure()
    return persona_path(c).read_text(encoding="utf-8")


def save_persona(text: str) -> None:
    _ensure()
    persona_path().write_text(str(text or ""), encoding="utf-8")


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
        picked.append(f"{'他' if m['role'] == 'user' else 'AI 助手'}：{body}")
        used += t
        if len(picked) >= 10:
            break
    if not picked:
        return ""
    return WHO_NOTE + "\n" + "\n".join(reversed(picked))


# 工作區裡兩個角色要分清楚：寵物把助手的話當成他說的，就會叫他替助手認錯（「好啦我眼殘」）
WHO_NOTE = ("（「他：」是他本人打的字；「AI 助手：」是幫他做事的另一個 AI 說的話、做的事，不是他。"
            "回報錯、吹牛被抓包、做壞的是 AI 助手，不要算到他頭上。）")


def _fingerprint(session_id: str) -> str:
    from . import store

    last = next((m for m in reversed(store.get_messages(session_id) or [])
                 if m.get("role") == "assistant"), None)
    if not last:
        return ""
    return f"{last.get('id') or ''}:{len(_text_of(last))}"


# ---------------------------------------------------------------- 提示詞

_ASK = re.compile(r"[?？]|怎麼|要不要|該不該|建議|意見|覺得|為什麼|如何|幫我想")
_ACK = re.compile(r"^(好|好喔|好啦|好的|嗯+|恩+|喔+|哦+|ok|OK|okay|讚|收到|謝謝|謝啦|感謝|哈+|呵+|lol|晚安|掰|bye)[!！。.~～]*$", re.I)


def length_hint(text: str, kind: str = "text") -> str:
    """依使用者這句話決定回多長。程式先算，模型照做，比叫模型自己判斷穩。"""
    if kind == "poke":
        return ("他沒說話，只是伸手摸了你（點了你的圖）。用你的方式回應被摸這件事，一則、十五個字內；"
                "如果瞄到他在忙什麼，可以順口提一句。不要按表情。")
    t = (text or "").strip()
    n = len(re.sub(r"\s", "", t))
    if _ACK.match(t):
        return "他只是回個短短的確認或笑。最好只按一個表情、say 留空陣列；真的想講就一則、十個字內。"
    if n <= 8:
        return "他這句很短。回一則，二十個字內。"
    if _ASK.search(t) and n > 12:
        return "他在問你想法。可以講兩三則，每則一兩句，總共不超過一百二十字。"
    if n <= 40:
        return "回一到兩則，每則一句，總共四十字內。"
    return "他講了比較多。回兩到三則，每則一兩句，總共不超過一百字。"


def _when() -> str:
    lt = time.localtime()
    h = lt.tm_hour
    part = ("深夜" if h < 5 else "清晨" if h < 8 else "早上" if h < 11 else "中午" if h < 13
            else "下午" if h < 18 else "晚上" if h < 23 else "深夜")
    wk = "一二三四五六日"[lt.tm_wday]
    return f"星期{wk}{part} {h}:{lt.tm_min:02d}"


def _ago(sec: float) -> str:
    if sec < 90:
        return "剛剛"
    if sec < 3600:
        return f"{int(sec // 60)} 分鐘前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小時前"
    return f"{int(sec // 86400)} 天前"


def build_system(c: dict | None = None, mode: str = "glance", hint: str = "") -> str:
    c = {**cfg(), **(c or {})}
    d = _load()
    memo = (d.get("summary") or "").strip()
    who = name_of(c)
    base = persona(c.get("char")).strip()
    first = base.split("\n", 1)
    if first and first[0].startswith("# "):
        base = f"# {who}" + ("\n" + first[1] if len(first) > 1 else "")
    pal = palette(c.get("char"))
    sig = pal[-1]
    msgs = d.get("msgs") or []
    mine = [m for m in msgs if m.get("role") == "pet" and m.get("text")][-4:]
    openings = ["「" + str(m["text"]).split("\n")[0][:6] + "」" for m in mine]
    my_reacts = [m["react"] for m in msgs if m.get("role") == "user" and m.get("react")][-4:]
    last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)

    lines = [base, "",
             "## 怎麼聊（這是通訊軟體，不是寫文章）",
             "- 長度跟著他走，大多數時候一兩句。不是每句都要有梗，有時候「嗯。」「好喔～」就夠了。",
             "- 想講兩件事就分兩則（say 陣列放兩個字串），最多三則；一則就是一句話，不要一則塞一整段。",
             "- 不要重複他剛說的話；不要每次都用問句結尾；不要每次都用同一個口頭禪開頭。",
             "- 情緒用行動和語氣表現，不要說「我很開心」「我理解你」。",
             "- 不要條列、不要標題、不要用星號或括號寫旁白（角色檔特別允許的除外）、不要提到你是模型或 AI。",
             "- 一律繁體中文（台灣用語），一個簡體字都不行。",
             "- 不要編造他沒說過、片段裡沒有的事；不確定就不要提。",
             "- 角色檔的「示範」只是語氣參考，不要照抄裡面的句子；每次用自己的話。",
             "",
             "## 按表情（像 LINE／Telegram 的訊息反應）",
             f"- react 是你對他剛剛那句按的表情，只能從這幾個挑：{' '.join(pal)}。",
             f"- 好笑→😂；累、難過→🥺；好消息、做完了→🔥 或 👍；很貼心、在撒嬌→❤️；嚇一跳→😮；說要睡→😴；{sig} 是你的招牌，偶爾才用。",
             "- 大多數時候不用按，留空字串。只有他那句真的讓你有感覺才按；說話時大概四五句才按一次。",
             "- 只按表情、不說話也是一種回覆：他只回「好」「嗯」「哈哈」時，按一個表情、say 給空陣列就好。",
             "",
             "## 表情與心情",
             f"- face 只能從這些挑一個：{', '.join(FACES)}。",
             "- face 跟你現在的心情一致：安慰他→comfort 或 gentle，捨不得→sad，撒嬌→clingy，"
             "很爽→cheer 或 excited，想睡→sleepy，疑惑→confused，得意→proud，好笑→laugh。",
             "- mood 填兩到三個字，例如心疼、搖尾巴、想睡、吃飽了。"]

    rules = MODES.get(c.get("mode") or "mix", MODES["mix"])["rules"]
    if rules:
        lines += ["", "## 模式"] + rules

    if mode == "talk":
        lines += ["", "## 這次",
                  "- 他正在跟你講話，直接回他。",
                  f"- 長度：{hint or '一兩句。'}"]
        if MODES[c.get("mode") or "mix"]["peek"]:
            lines.append("- 左邊工作區的片段只是你偷瞄到的素材，不要接著回答裡面的技術問題；"
                         "他在討拍就陪他，他問你想法才順口點一下你瞄到的東西。")
    elif mode == "nudge":
        lines += ["", "## 這次",
                  "- 他沒有找你，是你自己想找他講話（像傳一則訊息給他）。為什麼是現在，看下面「現在」那段。",
                  "- 一則、一句，二十五個字內。可以不講：沒有自然的話題就 say 給空陣列。",
                  "- 優先接他之前跟你提過、還沒有下文的事（「那個…後來怎樣了？」），其次才是跟現在時段有關的事"
                  "（吃飯、下班、週末、熬夜）。",
                  "- 不要用「在嗎」「你在幹嘛」「好久不見」這種空的開場；不要抱怨他沒理你。",
                  "- react 一律留空。"]
        if hint:
            lines.append(hint)
    else:
        lines += ["", "## 這次",
                  "- 你在旁邊看他做事，他沒有叫你。真的有話才講，一則、一句；沒什麼好說就 say 給空陣列（不吵他）。",
                  "- 同一件事不要重複講；你上次講過什麼，看你自己的對話紀錄。",
                  "- 這時候 react 一律留空（他沒有對你說話）。"]
    if mode != "nudge" and MODES[c.get("mode") or "mix"]["peek"]:
        lines.append("- 工作區裡有兩個人：他本人，和幫他做事的 AI 助手。助手出錯、謊報被抓包，要吐槽就吐槽「那個助手」，"
                     "你跟他站同一邊；不要把助手的話當成他說的，也不要叫他替助手認錯。")

    now = [f"- 現在：{_when()}"]
    if last_user:
        now.append(f"- 他上次跟你講話：{_ago(time.time() - float(last_user.get('at') or 0))}")
    if d.get("mood"):
        now.append(f"- 你剛剛的心情：{d['mood']}")
    if openings:
        now.append(f"- 你最近幾則的開頭：{'、'.join(openings)}——這次換個說法。")
    if my_reacts:
        now.append(f"- 你最近按過的表情：{' '.join(my_reacts)}——不要一直按同一個。")
    lines += ["", "## 現在（不要主動提，除非跟話題有關）"] + now

    lines += ["", "## 輸出",
              "只輸出一行 JSON，別的字都不要：",
              '{"say":["第一則","（想再講才加第二則）"],"face":"gentle","mood":"心疼","react":"","sug":"他最可能回你的一句"}',
              "- sug：他看到你的話之後，最可能直接點來回你的一句；十二個字內、用他自己的口氣，像隨手打的，"
              "繁體中文，不要捏造他沒提過的東西。你在講 AI 助手的事時，sug 是他對那件事的反應，不是他在認錯。"]
    if memo:
        lines += ["", "## 你記得的事", memo]
    return "\n".join(lines)


# ---------------------------------------------------------------- 附件（丟給牠的圖片、文件）
ATT = DIR / "att"
ATT_DOC_MAX = 6000      # 文件抽出的文字最多給牠幾個字
ATT_FILE_MAX = 25 * 1024 * 1024
ATT_IMG_TURNS = 2       # 只有最近幾則帶圖的訊息附圖本身，再早的只留「丟過一張圖」


def _doc_text(path) -> str:
    """文件轉文字：純文字檔、docx／pptx（解 zip 裡的 XML）、xlsx（openpyxl）。PDF 等讀不了的回空字串。"""
    import zipfile
    from pathlib import Path

    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in (".docx", ".pptx"):
            with zipfile.ZipFile(p) as z:
                names = (["word/document.xml"] if ext == ".docx"
                         else sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                     key=lambda n: int(re.findall(r"\d+", n)[-1])))
                parts = []
                for n in names:
                    xml = z.read(n).decode("utf-8", "replace")
                    xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
                    parts.append(re.sub(r"<[^>]+>", "", xml))
                return "\n".join(parts).strip()
        if ext in (".xlsx", ".xlsm"):
            import openpyxl

            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                rows.append(f"[{ws.title}]")
                for r in ws.iter_rows(values_only=True):
                    if any(v is not None for v in r):
                        rows.append("\t".join("" if v is None else str(v) for v in r))
                    if len(rows) > 400:
                        break
            return "\n".join(rows)
        raw = p.read_bytes()
        if b"\x00" in raw[:4096]:
            return ""
        for enc in ("utf-8", "utf-16", "big5", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def save_att(items: list) -> list[dict]:
    """前端送來的附件（base64）存進 data/pet/att；文件順便抽好文字存成同名 .txt。回存進訊息的描述。"""
    import base64
    import mimetypes
    from pathlib import Path

    ATT.mkdir(parents=True, exist_ok=True)
    out = []
    for it in (items or [])[:6]:
        try:
            data = base64.b64decode(str(it.get("data") or ""))
        except Exception:
            continue
        if not data or len(data) > ATT_FILE_MAX:
            continue
        name = str(it.get("name") or "附件")[:120]
        mt = str(it.get("media_type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        ext = Path(name).suffix.lower()[:10] or mimetypes.guess_extension(mt) or ".bin"
        if not re.fullmatch(r"\.[a-z0-9]{1,9}", ext):
            ext = ".bin"
        path = ATT / f"{uuid.uuid4().hex[:12]}{ext}"
        path.write_bytes(data)
        if mt.startswith("image/"):
            out.append({"kind": "image", "path": str(path), "name": name, "media_type": mt})
        else:
            txt = _doc_text(path)
            if txt.strip():
                path.with_suffix(path.suffix + ".txt").write_text(txt[:ATT_DOC_MAX * 2], encoding="utf-8")
            out.append({"kind": "file", "path": str(path), "name": name, "bytes": len(data), "read": bool(txt.strip())})
    return out


def _att_note(m: dict) -> str:
    """附件寫成給牠看的文字：文件附抽出的內容，讀不了的照實講。"""
    from pathlib import Path

    lines = []
    for a in m.get("att") or []:
        if a.get("kind") == "image":
            lines.append(f"（他丟了一張圖：{a.get('name')}）")
            continue
        t = ""
        side = Path(str(a.get("path"))).with_suffix(Path(str(a.get("path"))).suffix + ".txt")
        if side.is_file():
            t = side.read_text(encoding="utf-8", errors="replace")
        if t.strip():
            cut = len(t) > ATT_DOC_MAX
            lines.append(f"（他丟了一個檔案：{a.get('name')}，內容{'（只給你前面一段）' if cut else ''}：）\n"
                         f"<file name=\"{a.get('name')}\">\n{t[:ATT_DOC_MAX]}\n</file>")
        else:
            lines.append(f"（他丟了一個檔案：{a.get('name')}，但這種格式你讀不到內容，照實跟他說）")
    return "\n".join(lines)


def gc_att() -> int:
    """刪掉沒有任何寵物紀錄引用的附件（壓縮、清空對話後留下的）。一小時內的新檔不動。"""
    if not ATT.is_dir():
        return 0
    used = set()
    for f in DIR.glob("chat-*.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            for m in (j.get("arch") or []) + (j.get("msgs") or []):
                for a in m.get("att") or []:
                    used.add(str(a.get("path")))
        except Exception:
            continue
    n = 0
    fresh = time.time() - 3600
    for p in ATT.iterdir():
        base = str(p)[:-4] if p.name.endswith(".txt") else str(p)
        if base in used or p.stat().st_mtime > fresh:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _own_messages(c: dict, vision: bool = False) -> list:
    """自己的對話紀錄轉成模型訊息；使用者按的表情寫進他的下一句前面，讓牠知道。
    vision：牠的模型能看圖時，最近 ATT_IMG_TURNS 則帶圖的訊息附上圖本身。"""
    d = _load()
    msgs = d.get("msgs") or []
    budget = int(c["ctx"])
    out, used = [], 0
    for m in reversed(msgs):
        t = est_tokens((m.get("text") or "") + (_att_note(m) if m.get("att") else ""))
        if used + t > budget * 0.8 and out:
            break
        out.append(m)
        used += t
    out.reverse()
    from pathlib import Path

    pics = [m for m in out if m.get("role") != "pet" and any(a.get("kind") == "image" for a in m.get("att") or [])]
    pics = pics[-ATT_IMG_TURNS:]
    conv: list = []
    note = ""
    for m in out:
        if m["role"] == "pet":
            text = m.get("text") or ""
            if not text.strip():
                continue
            conv.append({"role": "assistant", "content": [{"type": "text", "text": json.dumps(
                {"say": text.split("\n"), "face": m.get("face") or "happy"}, ensure_ascii=False)}]})
            if m.get("ureact"):
                note = f"（他對你上一則按了 {m['ureact']}）\n"
        else:
            body = note + (m.get("text") or "")
            blocks = []
            if m.get("att"):
                body = (body + "\n" + _att_note(m)).strip()
                if vision and m in pics:
                    blocks = [{"type": "image", "path": a["path"], "media_type": a.get("media_type") or "image/png"}
                              for a in m["att"] if a.get("kind") == "image" and Path(str(a.get("path"))).is_file()]
                elif any(a.get("kind") == "image" for a in m["att"]):
                    body += "\n（圖你看不到" + ("：太早的圖已經收起來了" if vision else "：你現在用的模型不能看圖") + "，照實說）"
            conv.append({"role": "user", "content": [{"type": "text", "text": body}] + blocks})
            note = ""
    if note:
        conv.append({"role": "user", "content": [{"type": "text", "text": note.strip()}]})
    # 模型要求 user 先講、兩邊交替：把開頭的 assistant 拿掉，連續同角色合併
    while conv and conv[0]["role"] != "user":
        conv.pop(0)
    merged: list = []
    for x in conv:
        if merged and merged[-1]["role"] == x["role"]:
            merged[-1]["content"][0]["text"] += "\n" + x["content"][0]["text"]
            merged[-1]["content"].extend(x["content"][1:])   # 附的圖跟著併過去
        else:
            merged.append(x)
    return merged


# ---------------------------------------------------------------- 解析

def _one(x) -> str:
    return to_tw(re.sub(r"\s+", " ", str(x)).strip())


def parse(raw: str) -> dict:
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return {"say": to_tw(t[:400]), "face": "happy", "mood": "", "sug": [], "react": "", "_ok": False}
    try:
        d = json.loads(m.group(0))
    except Exception:
        try:
            import ast

            d = ast.literal_eval(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return {"say": t[:400], "face": "happy", "mood": "", "sug": [], "react": "", "_ok": False}
    if not isinstance(d, dict):
        return {"say": t[:400], "face": "happy", "mood": "", "sug": [], "react": "", "_ok": False}
    raw_say = d.get("say")
    if isinstance(raw_say, list):
        parts = [_one(x)[:300] for x in raw_say if _one(x)]
    else:
        parts = [_one(raw_say or "")[:600]] if _one(raw_say or "") else []
    say = "\n".join(parts[:3])
    face = str(d.get("face") or "").strip().lower()
    sug = d.get("sug") or []
    if isinstance(sug, str):
        sug = [sug]
    sug = [_one(x)[:20] for x in list(sug) if _one(x)][:1]
    react = str(d.get("react") or "").strip()[:4]
    return {"say": say, "face": face if face in FACES else "happy",
            "mood": to_tw(str(d.get("mood") or "").strip()[:8]), "sug": sug,
            "react": react, "_ok": True}


def fit_react(emoji: str, c_id: str | None = None) -> str:
    """只收表情列裡有的；❤ 沒有變體選擇字元也算 ❤️。"""
    e = (emoji or "").strip()
    if not e:
        return ""
    pal = palette(c_id)
    if e in pal:
        return e
    bare = e.replace("\ufe0f", "")
    for p in pal:
        if p.replace("\ufe0f", "") == bare:
            return p
    return ""


# ---------------------------------------------------------------- 呼叫模型

def _params(model: dict, c: dict) -> dict:
    m = dict(model)
    from .effort import resolve
    # 選的檔位模型沒有就往下找最近的一檔；舊設定的 "on"＝用模型預設
    e = resolve(m.get("efforts"), c.get("effort") or None, m.get("default_effort"))
    if e:
        m["effort"] = e
    else:
        m.pop("effort", None)
    return m


async def _call(model: dict, system: str, msgs: list, max_out: int) -> str:
    from .agent.loop import complete

    return await complete(model, system, msgs, max_text=int(max_out))


async def _repair(model: dict, raw: str) -> dict | None:
    """模型偶爾不照 JSON 回；用一次很小的呼叫把它改寫，不要整段重跑。"""
    prompt = ("下面這段話要轉成一個 JSON 物件：say（原話拆成一到三則短句的陣列，不要改意思）、"
              "face（" + ", ".join(FACES) + " 其中一個）、mood（兩三個字）、react（空字串）、"
              "sug（一句十二字內的短回覆）。只輸出 JSON。\n\n\"\"\"" + (raw or "")[:1200] + '"""')
    try:
        out = await _call(model, "你只輸出 JSON 物件。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 300)
    except Exception:
        return None
    got = parse(out)
    return got if got.get("_ok") else None


async def _fill_sug(model: dict, say: str, context: str = "") -> list:
    """模型偶爾漏掉 sug；只用一次很小的呼叫補一句。"""
    prompt = ("你剛剛對使用者說了：「" + say.replace("\n", " ") + "」\n"
              + (("他最近的狀況：\n" + context[:600] + "\n") if context else "")
              + "使用者看到這句話，最可能直接回哪一句？十二個字內、用他的口氣、像隨手打的。"
              + "你講的如果是 AI 助手做錯的事，他是跟你一起看熱鬧的人，不是被你吐槽的人。"
                '只輸出 JSON 陣列，例如 ["好啦我去躺一下"]')
    try:
        raw = await _call(model, "你只輸出 JSON 陣列，不要說明。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 80)
    except Exception:
        return []
    m = re.search(r"\[[\s\S]*\]", raw or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [_one(x)[:20] for x in list(arr) if _one(x)][:1]


async def _emit(emit) -> dict:
    st = state()
    if emit:
        await emit({"type": "pet", "pet": st, "history": history()[-40:]})
    return st


async def _say(session_id, got: dict, emit, kind: str) -> dict:
    c = cfg()
    d = _load()
    d["face"] = got["face"]
    d["mood"] = got["mood"]
    d["at"] = time.time()
    d["calls"] = int(d.get("calls") or 0) + 1
    react = fit_react(got.get("react") or "", c["char"]) if kind == "talk" else ""
    if react:
        for m in reversed(d.get("msgs") or []):
            if m.get("role") == "user":
                m["react"] = react
                break
    if got["say"]:
        d["say"] = got["say"]
        said = {x.strip() for x in str(got["say"]).splitlines() if x.strip()}
        d["sug"] = [s for s in (got.get("sug") or []) if str(s).strip() and str(s).strip() not in said][:1]
        d.setdefault("msgs", []).append({"id": _mid(), "role": "pet", "text": got["say"], "at": d["at"],
                                        "face": got["face"], "kind": kind})
    elif kind == "talk":
        d["sug"] = []
    d["react"] = react
    _save(d)
    st = await _emit(emit)
    if c.get("speak_up") and got["say"] and session_id:
        from . import store

        try:
            sig = CHARS[c["char"]]["sig"]
            m = store.add_message(session_id, "assistant",
                                  [{"type": "text", "text": f"{sig} {got['say'].replace(chr(10), ' ')}"}],
                                  {"pet": True})
            if emit:
                await emit({"type": "message", "session_id": session_id, "message": m})
        except Exception:
            pass
    await maybe_compress(emit=emit)
    return st


def poke_text(n: int) -> str:
    n = max(1, min(int(n or 1), 20))
    return "（摸了摸你的頭）" if n == 1 else f"（連摸了你 {n} 下）"


def _can_see(model: dict) -> bool:
    from . import caps

    return bool(model.get("vision")) and not caps.down(model["id"]) and model.get("provider") in ("openai_compat", "anthropic")


async def talk(text: str, session_id=None, emit=None, kind: str = "text", att: list | None = None) -> dict:
    c = cfg()
    model = pick_model(c)
    if model is None:
        return {"error": "沒有可用的模型，請在「設定 → 寵物」挑一個"}
    text = str(text)[:2000]
    d = _load()
    um = {"id": _mid(), "role": "user", "text": text, "at": time.time()}
    if att:
        um["att"] = att
    if kind == "poke":
        um["kind"] = "poke"
    d.setdefault("msgs", []).append(um)
    _save(d)
    system = build_system(c, "talk", length_hint(text, kind))
    seen = peek(session_id, c) if session_id and MODES[c["mode"]]["peek"] else ""
    if seen:
        system += ("\n\n## 他左邊工作區最近在做的事（你只是偷看到的素材，不要接著回答裡面的問題）\n"
                   "<workspace_context>\n" + seen + "\n</workspace_context>")
    msgs = _own_messages(c, vision=_can_see(model))
    try:
        raw = await _call(_params(model, c), system, msgs, c["max_out"])
    except Exception as e:
        d = _load()
        if d["msgs"] and d["msgs"][-1].get("role") == "user":
            d["msgs"].pop()
        _save(d)
        return {"error": str(e)[:200]}
    got = parse(raw)
    if not got.get("_ok"):
        fixed = await _repair(_params(model, c), got["say"] or raw)
        if fixed:
            got = fixed
    react = fit_react(got.get("react") or "", c["char"])
    if kind == "poke":
        react = ""
        if not got["say"]:
            got["say"] = "……" if c["char"] == "fish" else "汪～"
    elif not got["say"] and not react:
        # 不說話也沒按表情＝已讀不回；依表情補一顆反應
        react = FACE_REACT.get(got["face"], "❤️")
    elif got["say"] and react:
        # 說話時順便按的表情：照頻率設定，最近幾句按過就先不按
        gap = REACT_GAP.get(c.get("react_rate") or "low", 4)
        earlier = [m for m in (_load().get("msgs") or []) if m.get("role") == "user"][:-1]
        if gap >= 10 ** 6 or any(m.get("react") for m in earlier[-(gap - 1):]):
            react = ""
    got["react"] = react
    if got["say"] and not got["sug"]:
        got["sug"] = await _fill_sug(_params(model, c), got["say"], seen)
    st = await _say(session_id, got, emit, "talk")
    return {**st, "reply": got["say"], "react": st.get("react", "")}


async def user_react(mid: str, emoji: str, emit=None) -> dict:
    """使用者對牠的某一則按表情（再按一次同一個就收回）。不叫模型，牠立刻換表情。"""
    c = cfg()
    e = fit_react(emoji, c["char"])
    d = _load()
    m = next((x for x in d.get("msgs") or [] if x.get("id") == mid and x.get("role") == "pet"), None)
    if m is None:
        return {"error": "找不到那則訊息"}
    if not e or m.get("ureact") == e:
        m.pop("ureact", None)
    else:
        m["ureact"] = e
        face, mood = FEEL.get(e, ("happy", ""))
        d["face"], d["mood"] = face, mood
    _save(d)
    return await _emit(emit)


async def glance(session_id: str, emit=None, force: bool = False):
    """背景：瞄一眼主對話。沒有新內容就不叫模型（省錢）。"""
    c = cfg()
    if not c["on"] and not force:
        return None
    if not MODES[c["mode"]]["peek"]:
        # 情感模式不看對話；「看一眼」改成自己找你講一句
        return await nudge("", emit, force=True) if force else None
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
        "text": "（他沒有對你說話。這是他左邊工作區最近的事：）\n" + seen
                + "\n\n（想說話就說一句；沒什麼好說就 say 給空陣列。）"}]}]
    # 交替：前一則若也是 user 就合併
    if len(msgs) >= 2 and msgs[-2]["role"] == "user":
        last = msgs.pop()
        msgs[-1]["content"][0]["text"] += "\n" + last["content"][0]["text"]
    try:
        raw = await _call(_params(model, c), build_system(c, "glance"), msgs, c["max_out"])
    except Exception:
        return None
    got = parse(raw)
    if not got.get("_ok"):
        fixed = await _repair(_params(model, c), got["say"] or raw)
        if fixed:
            got = fixed
    if got["say"] and not got["sug"]:
        got["sug"] = await _fill_sug(_params(model, c), got["say"], seen)
    if not got["say"] and not force:
        d = _load()
        d["face"] = got["face"]
        d["mood"] = got["mood"]
        _save(d)
        await _emit(emit)
        return None
    return await _say(session_id, got, emit, "glance")


def _daypart(h: int) -> str:
    return ("早上" if 7 <= h < 11 else "中午" if 11 <= h < 14 else "下午" if 14 <= h < 18
            else "晚上" if 18 <= h < 23 else "")


def _quiet(c: dict, h: int) -> bool:
    a, b = int(c.get("quiet_from", 23)), int(c.get("quiet_to", 8))
    if a == b:
        return False
    return (a <= h or h < b) if a > b else (a <= h < b)


def nudge_plan(c: dict | None = None, now: float | None = None) -> dict:
    """看時間要不要自己找他。只用時間與自己的紀錄判斷，不叫模型。
    回傳 {"go": bool, "why": [...], "at": 下次最早的時間, "stop": 原因}。"""
    c = c or cfg()
    now = now or time.time()
    lv = NUDGE.get(c.get("nudge") or "off")
    if not c.get("on") or not lv:
        return {"go": False, "at": 0, "stop": "off"}
    base, cap = lv
    msgs = _load().get("msgs") or []
    last_any = max([float(m.get("at") or 0) for m in msgs] or [0])
    last_user = max([float(m.get("at") or 0) for m in msgs if m.get("role") == "user"] or [0])
    unanswered = sum(1 for m in msgs if m.get("role") == "pet" and m.get("kind") == "nudge"
                     and float(m.get("at") or 0) > last_user)
    if unanswered >= NUDGE_MAX_UNANSWERED:
        return {"go": False, "at": 0, "stop": "unanswered"}
    skip = float(_load().get("nudge_skip") or 0)
    lt = time.localtime(now)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    today = sum(1 for m in msgs if m.get("kind") == "nudge" and float(m.get("at") or 0) >= day0)
    if today >= cap:
        return {"go": False, "at": day0 + 86400 + int(c.get("quiet_to", 8)) * 3600, "stop": "cap"}
    wait = base * (2 ** unanswered)
    due = last_any + wait if last_any else now
    why: list = []
    if now >= due:
        why.append("silence")
    # 換了時段（早上／中午／晚上）而且這個時段你們還沒講過話：打個招呼也算自然（「很少」不做這個）
    part = _daypart(lt.tm_hour)
    if c.get("nudge") != "rare" and part and not unanswered and now - last_any >= 45 * 60:
        start = day0 + {"早上": 7, "中午": 11, "下午": 14, "晚上": 18}[part] * 3600
        if last_any < start:
            why.append("daypart")
    if _quiet(c, lt.tm_hour):
        return {"go": False, "at": due, "stop": "quiet"}
    if now < skip:
        return {"go": False, "at": skip, "stop": "skip"}
    return {"go": bool(why), "why": why, "at": due, "unanswered": unanswered, "silence": now - last_any if last_any else 0}


async def nudge(session_id: str = "", emit=None, force: bool = False):
    """看時間自己找他講一句。條件不到就不叫模型。"""
    c = cfg()
    plan = nudge_plan(c)
    if not plan.get("go") and not force:
        return None
    model = pick_model(c)
    if model is None:
        return None
    d = _load()
    msgs_all = d.get("msgs") or []
    # 先占位：就算模型決定不講，這個時段也不會每次心跳都再問一次
    d["nudge_try"] = time.time()
    _save(d)
    why = []
    if "silence" in (plan.get("why") or []) and plan.get("silence"):
        why.append(f"他已經{_ago(plan['silence']).replace('前', '')}沒跟你講話了。")
    if "daypart" in (plan.get("why") or []):
        why.append(f"現在是{_daypart(time.localtime().tm_hour)}，你們這個時段還沒講過話。")
    if plan.get("unanswered"):
        why.append(f"你前面主動找他 {plan['unanswered']} 次他都還沒回——這次更輕、更短，不要追問。")
    mine = [m for m in msgs_all if m.get("role") == "pet" and m.get("kind") == "nudge"][-5:]
    if mine:
        why.append("你之前主動傳過的（不要再講一樣或很像的）：" + "／".join(
            "「" + str(m.get("text") or "").replace("\n", " ")[:40] + "」" for m in mine))
    seen = ""
    if MODES[c["mode"]]["peek"] and session_id:
        seen = peek(session_id, {**c, "peek": min(int(c["peek"]), 800)})
    hint = "\n".join("- " + x for x in why)
    body = "（他沒有對你說話。你自己想傳一則給他。）"
    if seen:
        body += "\n他左邊工作區最近的事（素材，不用回答裡面的問題）：\n" + seen
    msgs = _own_messages(c) + [{"role": "user", "content": [{"type": "text", "text": body}]}]
    if len(msgs) >= 2 and msgs[-2]["role"] == "user":
        last = msgs.pop()
        msgs[-1]["content"][0]["text"] += "\n" + last["content"][0]["text"]
    try:
        raw = await _call(_params(model, c), build_system(c, "nudge", hint), msgs, c["max_out"])
    except Exception:
        return None
    got = parse(raw)
    if not got.get("_ok"):
        fixed = await _repair(_params(model, c), got["say"] or raw)
        if fixed:
            got = fixed
    got["react"] = ""
    # 只留第一則：主動找人一次一句
    got["say"] = (got["say"] or "").split("\n")[0]
    if not got["say"] or any(got["say"].strip() == str(m.get("text") or "").strip() for m in mine):
        # 牠覺得沒什麼好講：過一陣子再想，不要每次心跳都問
        d = _load()
        d["nudge_skip"] = time.time() + max(1800, (NUDGE.get(c["nudge"]) or (3600, 0))[0] / 2)
        _save(d)
        return None
    if not got["sug"]:
        got["sug"] = await _fill_sug(_params(model, c), got["say"], seen)
    return await _say(session_id if MODES[c["mode"]]["peek"] else None, got, emit, "nudge")


async def maybe_compress(emit=None) -> None:
    """自己的對話太長就壓縮：壓成幾句，留最近 keep 則原文。"""
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
    old_text = "\n".join((("我" if m["role"] == "pet" else "他") + "：" + str(m.get("text") or "")
                          + (f"（他按了 {m['ureact']}）" if m.get("ureact") else "")) for m in old)
    memo = (d.get("summary") or "").strip()
    prompt = ("下面是你（" + name_of(c) + "）跟使用者的對話。請整理成 10 行以內的筆記，"
              "保留會影響之後陪伴方式的事（他在意什麼、最近在忙什麼、情緒、你們之間的默契、他喜歡你哪種回法），"
              "去掉一次性的細節。只輸出筆記。\n\n（你原本記得的）\n" + memo
              + "\n\n（要整理的對話）\n" + old_text)
    try:
        out = await _call(model, "你是整理筆記的助手，只輸出整理後的筆記。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 500)
    except Exception:
        return
    d["summary"] = out.strip()[:4000]
    d["arch"] = ((d.get("arch") or []) + old)[-ARCH_MAX:]   # 原文留給畫面往上捲，只是不再送給模型
    d["msgs"] = recent
    d["compressed"] = int(d.get("compressed") or 0) + 1
    _save(d)
    gc_att()   # 壓縮掉的訊息帶的附件一起清
    await _emit(emit)


async def after_turn(session_id: str, emit=None) -> None:
    try:
        await glance(session_id, emit)
    except Exception:
        pass


_WATCH = {"sid": "", "task": None}


def watching(session_id: str) -> None:
    if session_id:
        _WATCH["sid"] = session_id
        _ensure()


async def _heartbeat() -> None:
    while True:
        c = cfg()
        try:
            await asyncio.sleep(max(3, int(c.get("every_sec") or 10)))
            sid = _WATCH.get("sid") or ""
            if not c.get("on"):
                continue
            from . import server as _srv

            spoke = await glance(sid, _srv.broadcast) if sid and MODES[c["mode"]]["peek"] else None
            if not spoke and time.time() - float(_load().get("nudge_try") or 0) > 600:
                await nudge(sid, _srv.broadcast)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)


def start_heartbeat() -> None:
    if _WATCH.get("task") and not _WATCH["task"].done():
        return
    _WATCH["task"] = asyncio.create_task(_heartbeat())
