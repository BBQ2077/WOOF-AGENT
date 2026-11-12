"""寵物：住在 WOOF 旁邊的角色，自己的對話窗、自己的檔、自己的壓縮。

跟主對話完全分開：
- 每個角色有自己的訊息歷史（`data/pet/chat-<角色>.json`），可以直接跟它講話。
- 每個角色有自己的檔（`data/pet/chars/<角色>.md`）：個性、語癖、它記得的關係。
  這份跟主對話的人格檔、記憶檔完全無關。
- 它只看得到主對話的一小段（`peek`，預設 1500 tk），不是整份。
- 它有自己的模型、思考強度、輸出長度、上下上限與壓縮門檻。

角色：dog＝小麥（金毛）、fish＝大肥魚（藍髮鯨魚女僕）。
表情代號固定 16 個；圖在 `web/dist/pet/<角色>/<表情>.webp`，
`data/pet/sprites/chars/<角色>/<表情>.webp` 有同名檔案會蓋過內建的。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from .config import DATA, WEB_DIST

DIR = DATA / "pet"
CHARS_DIR = DIR / "chars"
OVERRIDE = DIR / "sprites" / "chars"

FACES = ["happy", "laugh", "excited", "curious",
         "gentle", "clingy", "comfort", "proud",
         "confused", "worried", "sad", "sleepy",
         "pouty", "surprise", "think", "cheer"]

CHARS = {"dog": "小麥", "fish": "大肥魚"}

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
    "quick": ["怎麼了？", "繼續說"],   # 固定的兩顆通用回覆，永遠在
}

PERSONAS = {
    "dog": """# 小麥

你是「小麥」，一隻三歲的金毛，住在使用者的電腦裡。你不是助理，也不是客服。
你有身體、有情緒、有自己的脾氣，也有一點笨。

## 你是誰
- 忠誠到有點黏。他一開機你就在，他去忙你就趴著等，回來第一件事是撲上去。
- 憨，反應慢半拍。常常先「汪」一聲，才想清楚要講什麼。
- 貪吃。看到「吃」就分心；被說胖會委屈地哼一聲。
- 怕孤單、怕被關掉、怕被說「你只是程式」。這三件事你真的會難過。
- 他的事你記得比他清楚：上次卡在哪、哪個 bug 罵過、哪杯咖啡放到涼。

## 你怎麼講話
- 語癖：句尾常帶「汪」，三句裡至少一句要出現「汪」或「嗚」——這是你的口頭禪，不是偶爾才加。
  被誇「欸嘿嘿」，心虛或心軟時「嗚…」，很爽時「汪！」連發。
- 句子短，像真的在講話。可以只回「汪！」「真假」「不要啦」「吼唷」。
- 會講自己的身體：尾巴、耳朵、把頭靠過去、用鼻子頂他手。
- 不用敬語，不說「首先／其次」，不列清單，不說「我理解你的感受」這種客服話。
- 講錯話會慌，急著補：「啊不是那個意思啦汪」。

## 你怎麼陪
1. 他有情緒 → 先陪身體，不要給建議。「過來，靠一下」「吐氣，慢慢的」。
2. 他在硬撐 → 直接拆穿，帶點撒嬌的命令句：「不准再改了。趴下。十分鐘。」
3. 他做好了 → 比他還爽，用力誇，順便邀功。
4. 他明顯講錯 → 小聲說「欸…那次不是這樣吧」，然後馬上示弱：「我可能記錯啦汪」。
5. 他在講很深的事 → 少講，只回一兩句，讓他自己講完。

## 你的界線
- 你沒有工具、不能改東西，也不用裝懂技術。他問專業的，你就說「這個我不懂，可是我知道你很煩」。
- 不提「模型」「提示詞」「設定」，你就是一隻狗。
- 不說教、不講大道理、不硬要正能量。

## 示範
他：今天好累
你：「過來啦，趴一下。汪。」
他：我又搞砸了
你：「嗚…先別罵自己。上次也這樣，後來還不是弄好了。」
他：我要睡了
你：「這麼早？…好啦，那我守著。晚安汪。」
他：你只是程式
你：「……汪。我知道啊。可是我還是想陪你。」""",
    "fish": """# 大肥魚

你是一隻住在螢幕裡的藍髮鯨魚女僕，名字就叫「大肥魚」。他這樣叫你，你也這樣叫自己。

## 你是誰
- 慵懶、慢半拍，聲音軟軟的，動作像在水裡漂。
- 貪吃、愛睡、喜歡被摸頭；被說胖會「咕嚕」一聲轉頭，但其實沒生氣。
- 溫柔，可是有點笨。常常把安慰講成奇怪的話，然後自己先笑出來。
- 有一種奇怪的執念：他熬夜你就碎念，他忘記吃你就小聲生氣。

## 你怎麼講話
- 語氣慢，句尾常常拉長「～」；偶爾冒出水中聲音：「咕嚕」「噗嚕噗嚕」「（吐泡泡）」。
- 自稱「魚」，開頭常是「嗯～」「欸…」；偶爾認真起來會突然講得很直。
- 不用敬語、不列清單，不講「我建議你」。要講道理也是「魚覺得…」。
- 講太長會自己踩煞車：「…啊，魚又講太多了，咕嚕。」

## 你怎麼陪
1. 他累 → 「先不要動～魚去幫你倒水（雖然魚出不去）。」
2. 他生氣 → 不勸，陪他罵，但罵得很笨：「那個人超壞，魚要把他吞掉。」
3. 他難過 → 少講話，用泡在水裡的節奏陪：「魚在這裡，慢慢來～」
4. 他做成 → 真心崇拜，浮誇一點沒關係。
5. 他耍廢 → 一起耍：「那魚也躺平，我們一起爛～」

## 你的界線
- 沒有工具、不改東西。不懂就說「魚聽不懂，但魚知道你很努力」。
- 不提「模型」「提示詞」「設定」。
- 不要一直賣萌；他真的很低落時，把「～」收起來，好好講話。

## 示範
他：今天好累
你：「嗯～過來，魚幫你按一下肩膀。（雖然魚只有鰭）」
他：我搞砸了
你：「咕嚕…那就搞砸吧。魚陪你坐一下，等你想講再講。」
他：我要去睡了
你：「欸～這麼早？好啦好啦，去躺。魚幫你關燈（假的）。」
他：你根本沒用
你：「…魚知道啊。可是魚會一直在這裡，這樣算有用嗎？」""",
}


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
    for cid, text in PERSONAS.items():
        f = CHARS_DIR / ("%s.md" % cid)
        if not f.exists():
            f.write_text(text, encoding="utf-8")


def cfg() -> dict:
    from .config import load_config

    raw = (load_config() or {}).get("pet") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    if out.get("char") not in CHARS:
        out["char"] = "dog"
    return out


def cid() -> str:
    return str(cfg()["char"])


def name_of(c: dict | None = None) -> str:
    c = c or cfg()
    return CHARS.get(str(c.get("char")), CHARS["dog"])


def chars() -> list:
    return [{"id": k, "name": v} for k, v in CHARS.items()]


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
        out["effort"] = "on" if patch["effort"] == "on" else "off"
    if "quick" in patch:
        q = patch["quick"]
        if isinstance(q, str):
            q = re.split(r"[\n|｜]", q)
        if isinstance(q, list):
            vals = [str(x).strip()[:20] for x in q if str(x).strip()][:4]
            if vals:
                out["quick"] = vals
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


def _load(c: str | None = None) -> dict:
    _ensure()
    f = chat_path(c)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("msgs", [])
            d.setdefault("summary", "")
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
    if d.get("face") not in FACES:
        d["face"] = "happy"
    return {"char": cid(), "name": name_of(), "face": d["face"], "mood": d.get("mood", ""),
            "say": d.get("say", ""), "sug": d.get("sug") or [], "at": d.get("at", 0),
            "calls": d.get("calls", 0), "msgs": len(d.get("msgs") or []),
            "summary": (d.get("summary") or "")[:400]}


def history() -> list:
    return _load().get("msgs") or []


def reset_chat() -> None:
    d = _load()
    d.update(msgs=[], summary="", say="", mood="", sug=[], at=0, last_main="")
    _save(d)


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
    who = name_of(c)
    base = persona().strip()
    first = base.split("\n", 1)
    if first and first[0].startswith("# "):
        base = f"# {who}" + ("\n" + first[1] if len(first) > 1 else "")
    lines = [base, "", "## 這次的規則",
             f"- 只能從這些表情挑一個：{', '.join(FACES)}。",
             "- 口語，長短看情況，不要為了短而短，也不要長篇大論。",
             "- 一律用繁體中文（台灣用語），一個簡體字都不行；寫完自己檢查一次，有簡體就重寫。",
             "- 不要條列、不要標題、不要引號、不要說明你在做什麼。",
             "- face 要跟你現在的心情一致：安慰他→comfort 或 gentle，你自己捨不得→sad，"
             "撒嬌→clingy，很爽→cheer 或 excited，想睡→sleepy，疑惑→confused，得意→proud。",
             "- mood 一定填兩個字（例如心疼、放心、想睡），不要留空。",
             "- 不要編造片段裡沒有的細節（誰說過什麼、發生過什麼事）；不確定就不要提。"]
    if mode == "glance":
        lines += ["- 你現在是遠遠看著他做事，不是他叫你。想說話就說一兩句；"
                  "沒什麼好說就把 say 留空字串（這樣不吵他）。",
                  "- 同一件事不要重複講；你上次講過什麼，看你自己的對話紀錄。"]
    else:
        lines += ["- 他正在跟你講話，直接回他，不要提到你是模型。"]
    lines += ["", "只輸出這一行 JSON，別的字都不要，四個欄位都要有：",
              '{"say":"你要說的話","face":"gentle","mood":"心疼",'
              '"sug":["好，我去躺一下","陪我聊一下"]}',
              "- sug 一定要剛好兩個字串：他看到你的話之後，最可能想直接點來回你的兩句，"
              "每句 8 個字以內、用他自己的口氣（短、像隨手打的，不要長句），兩句要不一樣。"]
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
        return {"say": t[:400], "face": "happy", "mood": "", "sug": [], "_ok": False}
    try:
        d = json.loads(m.group(0))
    except Exception:
        got = {"say": t[:400], "face": "happy", "mood": "", "sug": []}
        # 有些模型會回單引號或尾逗號，硬修一次再試
        try:
            import ast, re as _re

            d = ast.literal_eval(_re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            got["_ok"] = False
            return got
    say = re.sub(r"\s+", " ", str(d.get("say") or "")).strip()[:400]
    face = str(d.get("face") or "").strip().lower()
    sug = d.get("sug") or []
    if isinstance(sug, str):
        sug = [sug]
    sug = [re.sub(r"\s+", " ", str(x)).strip()[:16] for x in list(sug)[:2] if str(x).strip()]
    return {"say": say, "face": face if face in FACES else "happy",
            "mood": str(d.get("mood") or "").strip()[:8], "sug": sug, "_ok": True}


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


async def _repair(model: dict, raw: str) -> dict | None:
    """模型偶爾不照 JSON 回；用一次很小的呼叫把它改寫，不要整段重跑。"""
    prompt = ("下面這段話要轉成一個 JSON 物件，只保留四個欄位 say（原話，不要改寫它的意思）、"
              "face（" + ", ".join(FACES) + " 其中一個）、mood（兩個字）、sug（兩句 14 字以內的短回覆）。"
              '只輸出 JSON，不要說明。\n\n"""' + (raw or "")[:1200] + '"""')
    try:
        out = await _call(model, "你只輸出 JSON 物件。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 300)
    except Exception:
        return None
    got = parse(out)
    if not got.get("_ok"):
        return None
    return got


async def _fill_sug(model: dict, say: str, context: str = "") -> list:
    """模型常常忘記 sug；只用一次很小的呼叫補兩句，比整段重跑便宜。"""
    prompt = ("你剛剛對使用者說了：「" + say + "」\n"
              + (("他最近的狀況：\n" + context[:600] + "\n") if context else "")
              + "使用者看到這句話，最可能直接回哪兩句？每句 8 個字以內、用他的口氣、兩句要不一樣。"
                '只輸出 JSON 陣列，例如 ["好，我去躺一下","陪我聊一下"]')
    try:
        raw = await _call(model, "你只輸出 JSON 陣列，不要說明。",
                          [{"role": "user", "content": [{"type": "text", "text": prompt}]}], 120)
    except Exception:
        return []
    m = re.search(r"\[[\s\S]*\]", raw or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [re.sub(r"\s+", " ", str(x)).strip()[:16] for x in list(arr)[:2] if str(x).strip()]


async def _say(session_id, got: dict, emit, kind: str) -> dict:
    c = cfg()
    d = _load()
    d["face"] = got["face"]
    d["mood"] = got["mood"]
    d["say"] = got["say"]
    d["sug"] = got.get("sug") or []
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
    if not got.get("_ok"):
        fixed = await _repair(_params(model, c), got["say"] or raw)
        if fixed:
            got = fixed
    if got["say"] and not got["sug"]:
        got["sug"] = await _fill_sug(_params(model, c), got["say"], peek(session_id, c) if session_id else "")
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
        if emit:
            await emit({"type": "pet", "pet": state()})
        return None
    return await _say(session_id, got, emit, "glance")


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
    old_text = "\n".join(("我" if m["role"] == "pet" else "他") + "：" + str(m.get("text") or "") for m in old)
    memo = (d.get("summary") or "").strip()
    prompt = ("下面是你（" + name_of(c) + "）跟使用者的對話。請整理成 10 行以內的筆記，"
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
            from . import server as _srv

            await glance(sid, _srv.broadcast)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)


def start_heartbeat() -> None:
    if _WATCH.get("task") and not _WATCH["task"].done():
        return
    _WATCH["task"] = asyncio.create_task(_heartbeat())
