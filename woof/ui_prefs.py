"""介面偏好設定檔 data/ui-preferences.json（主題、我的最愛、思考強度、送出行為…）。

桌面版由 Electron 主程序直接讀寫同一個檔；這裡給「用瀏覽器開 WOOF」時走 /api/ui-prefs。
格式：{"version": 1, "revision": <int>, "values": {<key>: <string>}}
寫入：逐鍵合併（None 代表刪除），用 <檔>.lock 資料夾當鎖（兩邊都用 mkdir，原子），先寫暫存檔再改名。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import DATA

FILE = DATA / "ui-preferences.json"
MAX_VALUE = 256 * 1024
MAX_KEYS = 512


def read() -> dict:
    try:
        doc = json.loads(FILE.read_text(encoding="utf-8-sig"))
        vals = {k: v for k, v in (doc.get("values") or {}).items() if isinstance(v, str)}
        return {"exists": True, "revision": int(doc.get("revision") or 0), "values": vals}
    except FileNotFoundError:
        return {"exists": False, "revision": 0, "values": {}}
    except (ValueError, AttributeError):
        # 壞掉的檔留一份，當作沒有設定檔重新開始
        try:
            FILE.replace(FILE.with_name(f"{FILE.name}.corrupt-{int(time.time() * 1000)}"))
        except OSError:
            pass
        return {"exists": False, "revision": 0, "values": {}}


class _Lock:
    def __init__(self) -> None:
        self.dir = FILE.with_name(FILE.name + ".lock")

    def __enter__(self):
        end = time.time() + 3
        while True:
            try:
                self.dir.mkdir()
                return self
            except FileExistsError:
                try:  # 10 秒還在的鎖視為上次當掉留下的
                    if time.time() - self.dir.stat().st_mtime > 10:
                        self.dir.rmdir()
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise TimeoutError("設定檔被鎖住")
                time.sleep(0.02)

    def __exit__(self, *a):
        try:
            self.dir.rmdir()
        except OSError:
            pass


def write(changes: dict) -> dict:
    with _Lock():
        cur = read()
        vals = cur["values"]
        for k, v in changes.items():
            if not isinstance(k, str) or not k.startswith("woof.") or len(k) > 256:
                continue
            if v is None:
                vals.pop(k, None)
            elif isinstance(v, str) and len(v) <= MAX_VALUE and (k in vals or len(vals) < MAX_KEYS):
                vals[k] = v
        doc = {"version": 1, "revision": cur["revision"] + 1, "values": vals}
        tmp = FILE.with_name(f"{FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, FILE)
        return {"exists": True, "revision": doc["revision"], "values": vals}
