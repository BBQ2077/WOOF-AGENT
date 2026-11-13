"""寵物：三種模式＋看時間主動找你，用真模型跑一輪（在暫存資料夾，不碰真正的寵物紀錄）。

執行：.venv\\Scripts\\python.exe tests\\pet_nudge_live.py [模型 id]
會把 config.toml／keys.toml 複製到暫存資料夾讀，不印出任何 key。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="woof_pet_")
for f in ("config.toml", "keys.toml"):
    if (ROOT / f).exists():
        shutil.copy(ROOT / f, Path(TMP) / f)
os.environ["WOOF_DATA"] = TMP
sys.path.insert(0, str(ROOT))

from woof import pet, store  # noqa: E402
from woof.config import load_config, save_config  # noqa: E402
from woof.loader import REG  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "opencode-zen-deepseek-v4-1-flash"


def set_pet(**kw):
    cfg = load_config()
    cur = dict(cfg.get("pet") or {})
    cur.update(pet.clean(kw))
    cfg["pet"] = cur
    save_config(cfg)
    REG.config["pet"] = cur


def seed(char: str, msgs: list) -> None:
    d = pet._load(char)
    d["msgs"] = msgs
    d.pop("nudge_skip", None)
    pet._save(d, char)


async def main() -> None:
    REG.load_all()
    sid = store.create_session("寵物測試", MODEL)["id"] if hasattr(store, "create_session") else ""
    if sid:
        store.add_message(sid, "user", [{"type": "text", "text": "幫我把季報的圖表改成長條圖，明天早上要交"}])
        store.add_message(sid, "assistant", [{"type": "text", "text": "好，已經把 Q3 營收圖改成長條圖，還剩毛利率那張。"}])
    now = time.time()
    # 他三個半小時前說過週五要面試，之後就沒講話
    base = [
        {"id": "u1", "role": "user", "text": "我週五下午要去面試，有點緊張", "at": now - 4 * 3600},
        {"id": "p1", "role": "pet", "text": "會順利的啦\n先睡飽比較重要", "at": now - 4 * 3600 + 20, "face": "gentle"},
        {"id": "u2", "role": "user", "text": "好喔", "at": now - 3.5 * 3600},
    ]
    out = {}
    for char in ("dog", "fish"):
        for mode in ("heart", "mix", "work"):
            set_pet(model=MODEL, char=char, mode=mode, nudge="some", on=True, quiet_from=3, quiet_to=3, effort="off")
            seed(char, list(base))
            plan = pet.nudge_plan()
            got = await pet.nudge(sid, None)
            said = [m for m in pet.history() if m.get("kind") == "nudge"]
            out[f"{char}/{mode}"] = {"plan": {k: plan.get(k) for k in ("go", "why", "stop")},
                                     "say": said[-1]["text"] if said else "(沒講)", "spoke": bool(got)}
            print(char, mode, json.dumps(out[f"{char}/{mode}"], ensure_ascii=False), flush=True)
    # 退避：連續三則沒回就不再找
    set_pet(char="dog", mode="mix", nudge="often")
    seed("dog", base + [{"id": f"n{i}", "role": "pet", "kind": "nudge", "text": f"第{i}則", "at": now - 3 * 3600 + i * 3000}
                        for i in range(3)])
    print("三則沒回", json.dumps(pet.nudge_plan(), ensure_ascii=False))
    seed("dog", base + [{"id": "n0", "role": "pet", "kind": "nudge", "text": "x", "at": now - 1800}])
    print("一則沒回、半小時前", json.dumps(pet.nudge_plan(), ensure_ascii=False))
    set_pet(quiet_from=0, quiet_to=23)
    seed("dog", list(base))
    print("安靜時段", json.dumps(pet.nudge_plan(), ensure_ascii=False))
    shutil.rmtree(TMP, ignore_errors=True)


asyncio.run(main())
