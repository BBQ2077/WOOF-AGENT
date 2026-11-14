"""五子棋：跟寵物下棋。

棋由寵物的模型下、台詞也是他講（同一個模型、同一份角色檔）；程式只管規則：
落點合不合法、誰連成五。不給模型任何程式算出來的候選點或提示。

前端（web/gomoku.js）呼叫兩個動作：
  move   看棋面下一手。前端在你還在想的時候，會先用「你可能下的那一手之後」的完整棋面來問（預先思考），
         你真的下在那裡就直接用；問的就是那個確切的棋面，所以不會不即時
  react  悔棋、認輸、你贏了、開新局這些時候讓他講一句
"""
from __future__ import annotations

import asyncio
import json
import re
import time

SIZE = 15
LETS = "ABCDEFGHIJKLMNO"
# 難度＝思考強度＋下法。這類端點每秒吐的字有限，思考越多一手越久（實測關掉思考約 6 秒、低約 14 秒、高超過 50 秒）
DIFF = {
    "easy": ("off", "隨性下，像陪玩，偶爾放水；但對方下一手就要連五時一定要擋。"),
    "normal": ("off", "認真下：自己能連五就下，對方要連五一定擋，再顧攻守。"),
    "hard": ("low", "全力下：先看清楚雙方的四、活三和雙重威脅，再決定落點。"),
}
TIMEOUT = 60


def _pet():
    from woof import pet
    c = pet.cfg()
    model = pet.pick_model(c)
    who = pet.name_of(c)
    base = pet.persona(c.get("char")).strip()
    first = base.split("\n", 1)
    if first and first[0].startswith("# "):
        base = f"# {who}" + ("\n" + first[1] if len(first) > 1 else "")
    return pet, c, model, who, base


def _faces() -> list[str]:
    from woof import pet
    return list(getattr(pet, "FACES", []))


def _system(who: str, base: str) -> str:
    # 整局都一樣的開頭放前面，服務商的提示詞快取才吃得到
    return (
        base + "\n\n"
        f"## 現在在做什麼\n你（{who}）正在跟使用者下五子棋，邊下邊聊。\n"
        "規則：15×15，黑先，橫、直、斜連成五子（含以上）就贏，沒有禁手。\n"
        "座標：橫軸 A–O（左到右）、縱軸 1–15（上到下），H8 是正中央。\n"
        "說話照你平常的個性和口氣，一句、十五字內，一律繁體中文（台灣用語），不要提到你是模型或 AI。\n"
        "表情（face）從這些挑一個：" + ", ".join(_faces()) + "。\n"
        "只輸出一個 JSON 物件，不要 markdown、不要解釋。"
    )


def coord(x: int, y: int) -> str:
    return f"{LETS[x]}{y + 1}"


_AB = re.compile(r"^([A-O])\s*([1-9]|1[0-5])$")
_BA = re.compile(r"^([1-9]|1[0-5])\s*([A-O])$")


def xy(v) -> list[int] | None:
    """模型回的座標換成 0 起算的 [x, y]；認不出來就 None。"""
    if isinstance(v, str):
        s = v.strip().upper().replace(" ", "")
        m = _AB.match(s)
        if m:
            return [ord(m.group(1)) - 65, int(m.group(2)) - 1]
        m = _BA.match(s)
        if m:
            return [ord(m.group(2)) - 65, int(m.group(1)) - 1]
        return None
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        try:
            x, y = int(v[0]), int(v[1])
        except Exception:
            return None
        return [x, y] if 0 <= x < SIZE and 0 <= y < SIZE else None
    return None


def _board(body: dict) -> list[list[int]]:
    rows = body.get("board")
    if not isinstance(rows, list) or len(rows) != SIZE:
        raise ValueError("棋面格式不對")
    rows = [[int(v) for v in r] for r in rows]
    if any(len(r) != SIZE for r in rows):
        raise ValueError("棋面格式不對")
    return rows


def _grid(rows, me: int) -> str:
    out = "    " + " ".join(LETS) + "\n"
    for y, r in enumerate(rows):
        out += f"{y + 1:>2}  " + " ".join("·" if v == 0 else "X" if v == me else "O" for v in r) + "\n"
    return out


def _json(txt: str) -> dict:
    txt = re.sub(r"```+(?:json)?", "", txt or "").strip()
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _say(v) -> str:
    from woof.pet import to_tw
    s = str(v or "").strip().replace("\n", " ")[:80]
    return to_tw(s) if s else ""


def _face(v) -> str:
    v = str(v or "").strip()
    return v if v in _faces() else ""


def _effort(pet, model: dict, c: dict, want: str) -> dict:
    """關掉思考：模型清單沒有「關」這一檔，但介面本身能關（DeepSeek、NIM 這類）就直接送關；其餘照一般規則往下找。"""
    m = pet._params(model, {**c, "effort": want})
    if want == "off" and m.get("effort") != "off" and (m.get("effort_style") or "") in ("deepseek", "nim"):
        m["effort"] = "off"
    return m


async def _ask(pet, c, model, diff: str, system: str, prompt: str, max_out: int) -> str:
    m = _effort(pet, model, c, DIFF.get(diff, DIFF["normal"])[0])
    return await asyncio.wait_for(
        pet._call(m, system, [{"role": "user", "content": [{"type": "text", "text": prompt}]}], max_out),
        timeout=TIMEOUT)


def register(woof):
    async def move(body: dict) -> dict:
        pet, c, model, who, base = _pet()
        if not model:
            return {"error": "沒有可用的模型，先到「設定 → 寵物」挑一個"}
        try:
            rows = _board(body)
            me, you = int(body.get("pet") or 2), int(body.get("human") or 1)
        except Exception as e:
            return {"error": str(e)[:80]}
        diff = str(body.get("diff") or "normal")
        hist = ""
        for mv in list(body.get("moves") or [])[-16:]:
            try:
                hist += ("你 " if int(mv[2]) == me else "對方 ") + coord(int(mv[0]), int(mv[1])) + "；"
            except Exception:
                continue
        fmt = '{"move":"H8","say":"一句話","face":"表情"}'
        prompt = (
            f"輪到你，你執{'黑' if me == 1 else '白'}子（X），對方執{'黑' if you == 1 else '白'}子（O）。\n"
            "棋盤（·＝空點）：\n" + _grid(rows, me) + "\n"
            + (f"最近的落子：{hist}\n" if hist else "")
            + DIFF.get(diff, DIFF["normal"])[1] + "\n"
            + (str(body.get("retry_note") or "")[:120] + "\n" if body.get("retry_note") else "")
            + "move 一定要是空點。回覆格式：" + fmt
        )
        t0 = time.time()
        try:
            raw = await _ask(pet, c, model, diff, _system(who, base), prompt, 200)
        except asyncio.TimeoutError:
            return {"error": "他想太久了"}
        except Exception as e:
            return {"error": f"模型呼叫失敗：{str(e)[:120]}"}
        got = _json(raw)
        mv = xy(got.get("move"))
        if mv is None:
            hit = re.search(r"\b([A-O])\s*([1-9]|1[0-5])\b", (raw or "").upper())
            mv = xy(hit.group(0)) if hit else None
        if mv is None:
            return {"error": "他回的落點看不懂", "ms": int((time.time() - t0) * 1000)}
        if rows[mv[1]][mv[0]] != 0:
            return {"error": f"他想下的 {coord(*mv)} 已經有子了", "taken": coord(*mv),
                    "ms": int((time.time() - t0) * 1000)}
        return {"move": mv, "say": _say(got.get("say")), "face": _face(got.get("face")),
                "ms": int((time.time() - t0) * 1000)}

    EVENTS = {
        "start": "新的一局剛擺好，{first}。打個招呼。",
        "undo": "對方剛悔棋，把最後一步收回去了（第 {ply} 手）。回應一下。",
        "lose": "對方剛連成五子，你輸了（比分 你 {p}：對方 {u}）。",
        "resign": "對方認輸了，這局算你贏（比分 你 {p}：對方 {u}）。",
        "draw": "棋盤下滿了，和棋。",
    }

    async def react(body: dict) -> dict:
        pet, c, model, who, base = _pet()
        if not model:
            return {"error": "沒有可用的模型"}
        ev = str(body.get("event") or "")
        if ev not in EVENTS:
            return {"error": "不認得的事件"}
        sc = body.get("score") or {}
        text = EVENTS[ev].format(first="你先下" if body.get("pet_first") else "對方先下",
                                 ply=int(body.get("ply") or 0), p=int(sc.get("p") or 0), u=int(sc.get("u") or 0))
        prompt = text + '\n回覆格式：{"say":"一句話","face":"表情"}'
        try:
            raw = await _ask(pet, c, model, "easy", _system(who, base), prompt, 120)
        except asyncio.TimeoutError:
            return {"error": "他想太久了"}
        except Exception as e:
            return {"error": f"模型呼叫失敗：{str(e)[:120]}"}
        got = _json(raw)
        say = _say(got.get("say") or ("" if got else raw))
        return {"say": say, "face": _face(got.get("face"))}

    woof.add_action("move", move)
    woof.add_action("react", react)
