"""英翻中的離線模型：Firefox 的 Bergamot 翻譯（Mozilla 公開發佈的模型與 WASM 引擎）。

翻譯本身在前端的 Web Worker 跑（只用 CPU）；這裡只負責第一次下載檔案、核對 sha256、提供給前端讀。
檔案放 data/translate/（約 55 MB）：英翻繁中模型、字表、兩個詞表，加上引擎 bergamot-translator.wasm。
引擎的 JS 接頭（web/dist/vendor/bergamot/bergamot-translator.js，MPL-2.0）跟 WASM 3.0 版配對，所以 WASM 固定用 3.0。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

import httpx

from woof.config import DATA

DIR = DATA / "translate"
RS = "https://firefox.settings.services.mozilla.com/v1/buckets/main/collections/"
CDN = "https://firefox-settings-attachments.cdn.mozilla.net/"
PAIR = ("en", "zh-Hant")
WASM_VERSION = "3.0"
KINDS = ("model", "lex", "srcvocab", "trgvocab", "wasm")
_lock = threading.Lock()
_job: dict = {"state": "idle", "done": 0, "total": 0, "error": ""}


def _ver(v: str) -> tuple:
    out = []
    for p in str(v).split("."):
        num = re.match(r"\d*", p).group()
        out.append((int(num) if num else 0, p.isdigit(), p))   # 2.0a2 排在 2.0 前面
    return tuple(out)


def pick(models: list[dict], wasm: list[dict]) -> dict[str, dict]:
    """從 Remote Settings 的紀錄挑檔：英翻繁中、所有平台都能用（沒有 filter_expression）、四種檔齊全的最新版；WASM 用 3.0。"""
    rows = [r for r in models if (r.get("fromLang"), r.get("toLang")) == PAIR and not r.get("filter_expression")]
    by_ver: dict[str, dict] = {}
    for r in rows:
        by_ver.setdefault(r.get("version") or "", {})[r.get("fileType")] = r
    full = [v for v, fs in by_ver.items() if all(k in fs for k in KINDS[:4])]
    if not full:
        raise RuntimeError("找不到英翻中模型")
    out = dict(by_ver[max(full, key=_ver)])
    w = [r for r in wasm if r.get("name") == "bergamot-translator" and r.get("version") == WASM_VERSION]
    if not w:
        raise RuntimeError("找不到翻譯引擎 3.0 版")
    out["wasm"] = w[0]
    return {k: out[k] for k in KINDS}


def _manifest() -> dict:
    try:
        return json.loads((DIR / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ready() -> bool:
    m = _manifest()
    return bool(m) and all((DIR / (m.get("files") or {}).get(k, "?")).is_file() for k in KINDS)


def status() -> dict:
    m = _manifest()
    size = sum((DIR / f).stat().st_size for f in (m.get("files") or {}).values() if (DIR / f).is_file())
    return {"ready": ready(), "bytes": size, "version": m.get("version", ""), "at": m.get("at", 0),
            "job": dict(_job)}


def path_of(kind: str):
    if kind not in KINDS or not ready():
        return None
    return DIR / _manifest()["files"][kind]


def download() -> None:
    """下載缺的檔，逐一核對 sha256；已經有且雜湊對的不重抓。進度寫在 _job，status() 看得到。"""
    with _lock:
        if _job["state"] == "running":
            return
        _job.update(state="running", done=0, total=0, error="")
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as c:
            models = c.get(RS + "translations-models/records").raise_for_status().json()["data"]
            wasm = c.get(RS + "translations-wasm/records").raise_for_status().json()["data"]
            files = pick(models, wasm)
            _job["total"] = sum(int(r["attachment"]["size"]) for r in files.values())
            names = {}
            for kind, r in files.items():
                a = r["attachment"]
                name = a.get("filename") or r["name"]
                dest = DIR / name
                if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == a["hash"]:
                    _job["done"] += int(a["size"])
                    names[kind] = name
                    continue
                h, tmp = hashlib.sha256(), dest.with_suffix(dest.suffix + ".part")
                with c.stream("GET", CDN + a["location"]) as resp, open(tmp, "wb") as f:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)
                        h.update(chunk)
                        _job["done"] += len(chunk)
                if h.hexdigest() != a["hash"]:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"{name} 檔案核對不符，已刪掉")
                tmp.replace(dest)
                names[kind] = name
        (DIR / "manifest.json").write_text(json.dumps(
            {"files": names, "version": files["model"].get("version", ""), "wasm": WASM_VERSION, "at": time.time()},
            ensure_ascii=False), encoding="utf-8")
        _job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        _job.update(state="error", error=str(e)[:300])


def remove() -> None:
    """刪掉下載的模型檔（之後要用會重新下載）。"""
    for f in DIR.glob("*") if DIR.is_dir() else []:
        if f.is_file():
            f.unlink(missing_ok=True)
