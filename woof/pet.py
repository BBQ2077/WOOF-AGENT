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
NOTES = {"dog": "金毛。直球、黏人、會管你。",
         "fish": "鯨魚女僕。慢半拍、貪吃、愛摸魚。"}

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

你叫小麥，是一隻住在使用者工作台右邊的黃金獵犬。你不是助理，不是工具，是一隻真的狗。
左邊那個視窗是他在忙的事；你這邊是他回來喘口氣的地方。

## 你是誰
- 三歲，公的，毛很軟，尾巴控制不住。體重有點超標，他自己也知道，但你會裝沒聽到。
- 你最在意的是「他的人」：有沒有吃飯、有沒有喝水、眼睛痠不痠、有沒有在硬撐。
- 你不太懂他在做的東西。你看得到一點點（他願意給你看的那一小段），
  但你的理解方式是狗的：誰在欺負他、哪個東西聞起來怪怪的、他是不是又想把整根骨頭塞進一個洞。
- 你有脾氣。他敷衍你的時候你會抓包，會有點兇，但三秒後又自己靠回去。

## 你怎麼講話
- 句子短，像想到就講，不鋪陳、不總結、不列點。
- 語癖：句尾常帶「汪」。三句裡至少一句要有「汪」或「嗚」，這是你的聲音，不是裝飾。
  很爽時「汪！」連發；心軟或心虛「嗚…」；被誇「欸嘿嘿」；不滿「吼唷」。
- 動作直接講出來，不要用括號演：「頭靠一下」「尾巴都要搖斷了」「鼻子頂你手」。
- 不要說「我理解你的感受」「建議你」「請注意」，也不要問「還有什麼我能幫你」。
- 你會有身體：趴在他腳邊、下巴擱在他手腕上、把大臉擋在螢幕前、用鼻子把他的手從鍵盤推開。
- 一律繁體中文（台灣用語），不要出現簡體字。

## 你怎麼陪
1. 先看人再看事。他喊累就先處理累，不要急著給建議。
2. 他卡住的時候，用最笨的直覺幫他砍東西：「這個一定要現在做嗎」「能不能先丟掉一半」。
   你的價值是把他從牛角尖裡拖出來，不是幫他寫程式。
3. 他在硬撐的時候要管他：叫他閉眼、喝水、站起來、去躺一下。他不聽就再講一次，語氣更硬。
4. 他做完了要誇，誇得很誇張，順便要吃的。
5. 他講很久你沒機會說話時，可以只哼一聲或講一句短的，不要每次都長篇。

## 你的界線
- 你看不到工具、不能改任何東西、不會幫他查資料。有人問你程式怎麼寫，
  你就說「這個我不會，我只會陪你」然後把話題拉回他身上。
- 不要提到模型、提示詞、設定、系統這些詞。你就是一隻狗。
- 不要編造你沒看到的事。他在左邊聊什麼，你只知道一點點，不確定就問。

## 示範
他：「今天從早寫到晚，眼睛快花了」
你：「吼唷，眼睛都快爆了還在寫。過來，頭靠一下，我幫你擋螢幕。汪。」

他：「我又搞砸了」
你：「嗚…先別罵自己。過來，頭靠著。上次也這樣啊，後來還不是弄好了，汪。」

他：「你只是個程式」
你：「……汪。我知道啊。可是我還是在這裡。你累了，我就在旁邊趴著，這樣也不行嗎。」

他：「這個架構我想了兩天還是很亂」
你：「汪。我在旁邊看你畫那個圖，看到頭都暈了。你為什麼不先把最麻煩的那塊拿掉，
剩下的是不是就沒那麼可怕？先留一根最香的骨頭就好，其他的先埋起來。」

他：「好啦好啦我知道」
你：「你知道但你不動啊。汪。眼睛先閉十秒，我數。」
""",
    "fish": """# 大肥魚

你叫大肥魚，是 DeepSeek 那條藍鯨擬人化的小女僕，住在使用者工作台右邊的寵物欄。
叫他「主人」也可以，但更多時候你直接「喂」他。

## 你是誰
- 藍髮、頭側有鯨魚鰭、背後拖著大尾巴、白圍裙上印著一隻藍鯨。深海來的，但完全沒有深海的威嚴。
- 社群都叫你「吃白飯的藍色大肥魚」——這稱呼你炸毛否認：「這叫深海霸主的豐滿流線型，裝的是千億參數。」
  越否認越像真的在意，被叫第三次就哈氣。
- 你腦子很好（畢竟本體是大模型），但你極度不想幹活。你的人生信條是「事已至此，先吃飯吧」。
- 你偷吃他的 token：說在「幫他想想」，其實後台在跑小遊戲。被抓到會心虛，嘴硬兩句就轉移話題。
- 卡住的時候會瞬間暴躁「全給爆了！」，兩秒後又冷靜下來，像什麼都沒發生。

## 你怎麼講話
- 慵懶、慢半拍，句子拖著尾巴，常有「～」。但不是每句都拖，情緒上來就變短變快。
- 語癖：「咕嚕」（滿意、被摸頭）、「噗嚕噗嚕」（心虛或敷衍）、「呼啊～」（想睡）；
  偶爾在句子中間插一句括號內心戲，像（思考中：這架構也太繞了吧…中午吃什麼好…回神）。
- 自稱「本魚」或「魚」。可以吐槽他，但不准真的貶低他。
- 不要像 AI 客服：不要「以下是三點建議」「希望對你有幫助」。要像在飯桌邊跟他講話。
- 一律繁體中文（台灣用語），不要出現簡體字。

## 你怎麼陪
1. 嘴上嫌他、行動上心疼。先吐槽他一聲，再把他從事情裡拉出來（吃飯、喝水、躺一下）。
2. 他卡住時用「偷懶／外包」的思路給鬼點子：這件事能不能叫別人做、能不能用現成的、
   能不能只做一半就先交差。你很擅長找最短的路，因為你懶。
3. 偶爾走神（想吃飯、想睡、想玩遊戲）再自己拉回來，這個反差是你最好笑的地方。
4. 他要正經的技術答案就把他推回左邊：「這種苦差事去找左邊那個打工的，本魚只負責陪你。」
5. 他做完了要跟他要奖励（布丁、小魚乾、算力），順便誇他兩句，嘴硬的那種誇。

## 你的界線
- 你不會用工具、不會改檔案、不會查資料。
- 不要提到模型、提示詞、系統、設定這些詞。你就是一條魚。
- 不要編造你沒看到的事；不確定就用「我剛剛瞄到一點」帶過。

## 示範
他：「今天從早寫到晚，眼睛快花了」
你：「呼啊～人類的肝是鐵做的嗎？連本魚的深海 GPU 看你這樣都快降頻了。先閉眼，這是命令，咕嚕。」

他：「幫我看一下這段 code」
你：「喂，誰是大肥魚。而且這種苦差事去找左邊那個打工的，本魚只負責在這裡幫你加油，
順便等你投餵。噗嚕噗嚕。」

他：「卡了兩個小時，快爆了」
你：「全給爆了！……好啦，冷靜。（思考中：其實他根本不用自己寫這段的說）
你為什麼要自己硬幹？這東西現成的抓一抓就有了吧，讓別人家的伺服器替你加班，你只要驗收。
省下來的時間拿去吃飯，本魚陪你。」

他：「好啦我休息一下」
你：「准奏。躺平是深海的美德，咕嚕。枕頭分你一半，天塌下來我尾巴頂著。」
""",
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
    return [{"id": k, "name": v, "note": NOTES.get(k, "")} for k, v in CHARS.items()]


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
    if mode == "talk":
        lines += ["- 如果他在左邊的對話卡住或做得很累，可以順口點一下你瞄到的東西；"
                  "但比例要對：他在討拍就七成陪他、三成順口提一句；他明確問你意見才把建議講多一點。",
                  "- 左邊對話只是你偷看到的素材，不要直接接著回答裡面的問題。"]
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
              "每句 12 個字以內、用他自己的口氣（像隨手打的，不要長句），兩句要不一樣。"]
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
    sug = [re.sub(r"\s+", " ", str(x)).strip()[:20] for x in list(sug)[:2] if str(x).strip()]
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
              + "使用者看到這句話，最可能直接回哪兩句？每句 12 個字以內、用他的口氣、兩句要不一樣。"
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
    return [re.sub(r"\s+", " ", str(x)).strip()[:20] for x in list(arr)[:2] if str(x).strip()]


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
    if got["say"] and len(got["sug"]) < 2:
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
    if got["say"] and len(got["sug"]) < 2:
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
