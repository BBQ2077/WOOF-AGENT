"""五子棋：跟寵物下棋。

棋由寵物的模型下、台詞也是他講：同一個模型、同一份角色檔、同一份記憶（他的對話紀錄）。
你下了哪裡、他回了什麼都寫進他的紀錄，寵物欄即時看得到，之後聊天他也記得這局。
程式只管規則：落點合不合法、誰連成五。不給模型任何程式算出來的候選點或提示。

前端（web/gomoku.js）呼叫兩個動作：
  move    看棋面下一手（不寫紀錄）。前端在你還在想的時候，會先用「你可能下的那一手之後」的完整棋面來問（預先思考），
          你真的下在那裡就直接用；問的就是那個確切的棋面，所以不會不即時
  record  真的下了：把你那一手和他那句寫進紀錄（沒用到的預先思考不會進他的記憶）
  react   開新局、悔棋、認輸、輸贏、和棋：寫進紀錄並讓他回一句
"""
from __future__ import annotations

import asyncio
import json
import re
import time

SIZE = 15
LETS = "ABCDEFGHIJKLMNO"
# 難度＝想多久＋怎麼下。想多久照他目前模型自己有的檔位挑（不寫死哪一家的檔位）：
#   簡單＝模型最低的一檔（能關就關），普通＝寵物設定的思考強度，高手＝模型最高的一檔。
# 不給程式算的候選點或提示。
DIFF = {
    "easy": ("low", "隨性下，像陪朋友玩，不用每手都算到最好，偶爾放水沒關係；但對方下一手就要連五時還是要擋。"),
    "normal": ("set", "認真下：自己能連五就下，對方要連五一定擋，再顧攻守。"),
    "hard": ("top", "全力下：先看自己能不能連五、做活四或雙三，再看對方有沒有活三、衝四或雙重威脅要擋，想清楚再落子。"),
}
TIMEOUT = 180


def _pet():
    from woof import pet
    c = pet.cfg()
    return pet, c, pet.pick_model(c)


def _faces() -> list[str]:
    from woof import pet
    return list(getattr(pet, "FACES", []))


# 放進他平常的提示詞（同一份角色檔、記得的事）；整局不變，快取吃得到
GAME = (
    "- 你正在跟他下五子棋，邊下邊聊。對話紀錄裡「（五子棋）」開頭的是棋局裡發生的事，這局之前你們聊過的也都算數，可以接著聊。\n"
    "- 規則：15×15，黑先，橫、直、斜連成五子（含以上）就贏，沒有禁手。座標：橫軸 A–O（左到右）、縱軸 1–15（上到下），H8 是正中央。\n"
    "- 說話照你平常的個性和口氣，一句、十五字內；可以聊棋，也可以順著你們剛剛聊的事講。\n"
    "- 每次最後會給你目前的棋盤，照那一段的格式只輸出一個 JSON 物件，不要 markdown、不要解釋。"
)


def _emit():
    from woof.server import broadcast
    return broadcast


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


def _stones(rows, me: int, you: int) -> str:
    """同一個棋盤再用座標寫一次：模型數格子常數錯一欄，直接列出每顆子的位置比較不會看錯（不是提示，只是換個寫法）。"""
    pts = lambda who: " ".join(coord(x, y) for y in range(SIZE) for x in range(SIZE) if rows[y][x] == who) or "（無）"
    return f"你的子（X）：{pts(me)}\n對方的子（O）：{pts(you)}\n"


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
    if isinstance(v, (list, tuple)):   # 他平常的回覆格式是 say 陣列，偶爾照那樣回
        v = " ".join(str(x).strip() for x in v if str(x).strip())
    s = str(v or "").strip().replace("\n", " ")[:80]
    return to_tw(s) if s else ""


def _face(v) -> str:
    v = str(v or "").strip()
    return v if v in _faces() else ""


def _diff_effort(model: dict, c: dict, diff: str) -> str:
    """難度對到這個模型有的思考檔位；模型沒有檔位（不能調思考）就照設定，難度只剩下法的差別。"""
    from woof.effort import SCALE, norm
    es = [norm(e) for e in (model.get("efforts") or []) if e]
    how = DIFF.get(diff, DIFF["normal"])[0]
    if not es or how == "set":
        return c.get("effort") or ""
    if set(es) <= {"off", "on"}:
        return "off" if how == "low" and "off" in es else "on"
    graded = sorted((e for e in es if e in SCALE), key=SCALE.index)
    if how == "low":
        return "off" if "off" in es else (graded[0] if graded else es[0])
    return graded[-1] if graded else es[-1]


async def _ask(pet, c, model, prompt: str, max_out: int, diff: str = "normal") -> str:
    m = pet._params(model, {**c, "effort": _diff_effort(model, c, diff)})
    return await asyncio.wait_for(
        pet._call(m, pet.build_system(c, "game", GAME), pet.game_messages(c, prompt), max_out),
        timeout=TIMEOUT)


def register(woof):
    async def move(body: dict) -> dict:
        pet, c, model = _pet()
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
            f"（五子棋・目前棋盤）輪到你，你執{'黑' if me == 1 else '白'}子（X），對方執{'黑' if you == 1 else '白'}子（O）。\n"
            "棋盤（·＝空點）：\n" + _grid(rows, me) + "\n"
            + _stones(rows, me, you)
            + (f"最近的落子：{hist}\n" if hist else "")
            + DIFF.get(diff, DIFF["normal"])[1] + "\n"
            + (str(body.get("retry_note") or "")[:120] + "\n" if body.get("retry_note") else "")
            + "move 一定要是空點。回覆格式：" + fmt
        )
        t0 = time.time()
        try:
            raw = await _ask(pet, c, model, prompt, 200, diff)
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

    def _pt(v) -> str:
        p = xy(v)
        return coord(*p) if p else ""

    async def record(body: dict) -> dict:
        """真的下了：我那一手（可能沒有，例如他先手）＋他那一手和那句話，寫進他的紀錄。"""
        from woof import pet
        h = _pt(body.get("human"))
        note = "（五子棋）" + "，".join(x for x in [
            "開了新的一局，你先下" if body.get("opening") else "",
            f"我下 {h}" if h else "",
        ] if x) if (h or body.get("opening")) else ""
        say = _say(body.get("say"))
        await pet.game_log(note, say, _face(body.get("face")), emit=_emit())
        return {"ok": True}

    EVENTS = {
        "start": "（五子棋）開了新的一局，我先下",
        "undo": "（五子棋）我悔了一手",
        "lose": "（五子棋）我下 {mv}，連成五子，我贏了（比分 我 {u}：你 {p}）",
        "win": "（五子棋）你連成五子，你贏了（比分 我 {u}：你 {p}）",
        "resign": "（五子棋）我認輸了，這局算你贏（比分 我 {u}：你 {p}）",
        "draw": "（五子棋）棋盤下滿了，和棋",
    }

    async def react(body: dict) -> dict:
        pet, c, model = _pet()
        if not model:
            return {"error": "沒有可用的模型"}
        ev = str(body.get("event") or "")
        if ev not in EVENTS:
            return {"error": "不認得的事件"}
        sc = body.get("score") or {}
        note = EVENTS[ev].format(mv=_pt(body.get("move")) or "最後一手", p=int(sc.get("p") or 0), u=int(sc.get("u") or 0))
        await pet.game_log(note, emit=_emit())   # 先寫進紀錄，寵物欄馬上看得到；他就照著紀錄回
        prompt = '（五子棋）回他一句。回覆格式：{"say":"一句話","face":"表情"}'
        try:
            raw = await _ask(pet, c, model, prompt, 120)
        except asyncio.TimeoutError:
            return {"error": "他想太久了"}
        except Exception as e:
            return {"error": f"模型呼叫失敗：{str(e)[:120]}"}
        got = _json(raw)
        say = _say(got.get("say") or ("" if got else raw))
        if say:
            await pet.game_log("", say, _face(got.get("face")), emit=_emit())
        return {"say": say, "face": _face(got.get("face"))}

    woof.add_action("move", move)
    woof.add_action("record", record)
    woof.add_action("react", react)
