"""工作區快照：模型用 write_file／patch 改檔前，先把原檔存一份；之後可以把檔案還原到某一則訊息之前。

- 每個使用者訊息（一個回合）一個檢查點：data/snapshots/<對話>/<訊息 id>/manifest.json＋原檔副本。
- 同一回合同一個檔只記第一次（那才是「這回合之前」的樣子）。
- 原本不存在的檔，還原時會刪掉。
- 還原前會把現況再存一份（_undo），可以撤銷這次還原。
- 只管 write_file／patch（含子代理）改的檔；終端機指令改的檔追不到。
- 跟「回溯對話」分開：回溯只動對話，還原只動檔案。

容量（config.toml [snapshots]）：
- 原檔內容依雜湊存在 _blobs/，同一份內容不管被存幾次都只佔一份空間。
- 只存改動的部分：同一個檔第二次以後的快照，存成「跟上一份比，哪幾段照抄、哪幾段是新的」（行為單位），
  只有第一份是完整內容；全部再用 zlib 壓縮。還原時從完整那份一路套回去。
  差異比完整內容還大、或鏈太長（DELTA_DEPTH）、或檔案太大（DELTA_MAX）時，改存完整一份。
- 每個對話最多保留 max_per_session 個檢查點（舊的先刪）。
- 超過 retention_days 天沒有新快照的對話，整個清掉。
- 全部加起來超過 max_total_mb，從最舊的檢查點開始刪。
- 單檔超過 max_file_mb 不存（還原時會說明略過）。
- 寫檔時最多每 10 分鐘自動整理一次；設定頁也可以手動整理。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import shutil
import threading
import time
import zlib
from pathlib import Path

from .config import DATA, load_config

log = logging.getLogger("woof.snapshot")
ROOT = DATA / "snapshots"
BLOBS = ROOT / "_blobs"
UNDO = "_undo"
DEFAULTS = {"enabled": True, "max_per_session": 20, "max_total_mb": 500, "max_file_mb": 10, "retention_days": 7}
LIMITS = {"max_per_session": (1, 500), "max_total_mb": (10, 100000), "max_file_mb": (1, 1000), "retention_days": (1, 3650)}
PRUNE_EVERY = 600
DELTA_DEPTH = 50           # 差異鏈最多幾層，超過就存完整一份（還原時不用套太多層）
DELTA_MAX = 2 * 1048576    # 超過這個大小不算差異，直接壓縮存完整
MAGIC_FULL = b"WOOF-F1\n"
MAGIC_DELTA = b"WOOF-D1\n"
_prune_lock = threading.Lock()
_last_prune = 0.0


def settings() -> dict:
    raw = load_config().get("snapshots") or {}
    out = dict(DEFAULTS)
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    for k, (lo, hi) in LIMITS.items():
        try:
            out[k] = max(lo, min(hi, int(raw.get(k, out[k]))))
        except (TypeError, ValueError):
            pass
    return out


def clean_settings(body: dict) -> dict:
    cur = settings()
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    for k, (lo, hi) in LIMITS.items():
        if k in body:
            try:
                cur[k] = max(lo, min(hi, int(body[k])))
            except (TypeError, ValueError):
                raise ValueError(f"{k} 要是數字")
    return cur


def _obj(sha: str) -> Path:
    return BLOBS / sha[:2] / sha


def _parse(raw: bytes) -> tuple[str, bytes, str | None]:
    """回傳 (種類, 內容, 基底 sha)。種類：full／delta／raw（沒有標頭的舊格式）。"""
    if raw.startswith(MAGIC_FULL):
        return "full", zlib.decompress(raw[len(MAGIC_FULL):]), None
    if raw.startswith(MAGIC_DELTA):
        rest = raw[len(MAGIC_DELTA):]
        base, body = rest[:64].decode(), rest[65:]
        return "delta", zlib.decompress(body), base
    return "raw", raw, None


def _lines(b: bytes) -> list[bytes]:
    return b.splitlines(keepends=True)


def _make_delta(base: bytes, new: bytes) -> bytes:
    """new 相對 base 的差異：[[起, 迄]（照抄 base 的第幾行到第幾行）| "新內容"]。"""
    a, b = _lines(base), _lines(new)
    ops: list = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.append([i1, i2])
        elif j2 > j1:
            ops.append(b"".join(b[j1:j2]).decode("latin-1"))
    return json.dumps(ops, ensure_ascii=True, separators=(",", ":")).encode()


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    a = _lines(base)
    out = []
    for op in json.loads(delta):
        out.append(b"".join(a[op[0]:op[1]]) if isinstance(op, list) else op.encode("latin-1"))
    return b"".join(out)


def _depth(sha: str) -> int:
    n = 0
    while n <= DELTA_DEPTH:
        p = _obj(sha)
        if not p.exists():
            return DELTA_DEPTH + 1
        with p.open("rb") as f:
            head = f.read(len(MAGIC_DELTA) + 64)
        if not head.startswith(MAGIC_DELTA):
            return n
        sha = head[len(MAGIC_DELTA):].decode()
        n += 1
    return n


def load(sha: str) -> bytes:
    """取出某份快照的完整內容（差異就沿著基底一路套回來）。"""
    chain = []
    while True:
        kind, body, base = _parse(_obj(sha).read_bytes())
        if kind != "delta":
            data = body
            break
        chain.append(body)
        sha = base
    for d in reversed(chain):
        data = _apply_delta(data, d)
    return data


def _put(data: bytes, prev: str | None) -> str:
    """存一份內容，回傳 sha（完整內容的雜湊）。prev＝同一個檔上一份快照，能用就只存差異。"""
    sha = hashlib.sha256(data).hexdigest()
    dst = _obj(sha)
    if dst.exists():
        return sha
    full = MAGIC_FULL + zlib.compress(data, 6)
    out = full
    if prev and prev != sha and len(data) <= DELTA_MAX and _obj(prev).exists() and _depth(prev) < DELTA_DEPTH:
        try:
            base = load(prev)
            if len(base) <= DELTA_MAX:
                delta = MAGIC_DELTA + prev.encode() + b"\n" + zlib.compress(_make_delta(base, data), 6)
                if len(delta) < len(full):
                    out = delta
        except Exception:
            log.exception("算差異失敗，改存完整內容")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(out)
    tmp.replace(dst)
    return sha


def _content(d: Path, ent: dict) -> bytes:
    if ent.get("sha"):
        return load(ent["sha"])
    return (d / ent["blob"]).read_bytes()  # 最早的格式：原檔副本放在檢查點資料夾裡


def _prev_sha(sid: str | None, key: str, before: Path) -> str | None:
    """同一個對話裡，這個檔最近一次的快照 sha。"""
    if not sid:
        return None
    for d in reversed(_cps(ROOT / sid)):
        if d == before:
            continue
        e = _read(d).get("files", {}).get(key) or {}
        if e.get("sha"):
            return e["sha"]
    return None


def _dir(sid: str, mid: int | str) -> Path:
    return ROOT / sid / str(mid)


def _read(d: Path) -> dict:
    try:
        return json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}


def _write(d: Path, man: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "manifest.tmp"
    tmp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "manifest.json")


def _key(path: Path) -> str:
    return str(path.resolve())


def _capture(d: Path, man: dict, path: Path, sid: str | None = None) -> None:
    k = _key(path)
    if k in man["files"]:
        return
    ent: dict = {"existed": path.exists(), "at": time.time()}
    if ent["existed"]:
        size = path.stat().st_size
        ent["size"] = size
        cap = settings()["max_file_mb"]
        if path.is_dir() or size > cap * 1048576:
            ent["skipped"] = "資料夾" if path.is_dir() else f"檔案太大（{size / 1048576:.1f} MB，上限 {cap} MB）"
        else:
            ent["sha"] = _put(path.read_bytes(), _prev_sha(sid, k, d))
    man["files"][k] = ent


def before_write(ctx, path: Path) -> None:
    """files 插件寫檔前呼叫。失敗只記錄，不擋寫入。"""
    try:
        if not settings()["enabled"]:
            return
        turn = getattr(ctx, "turn", ctx)  # 工具拿到的是 ToolContext，回合資訊在 .turn
        sid = getattr(turn, "root_session_id", None) or turn.session_id
        mid = int(getattr(turn, "turn_mid", 0) or 0)
        if not mid:
            return
        d = _dir(sid, mid)
        man = _read(d)
        if _key(path) in man.get("files", {}):
            return
        man.setdefault("created", time.time())
        man["mid"] = mid
        _capture(d, man, Path(path), sid)
        _write(d, man)
        if time.time() - _last_prune > PRUNE_EVERY:
            threading.Thread(target=prune, daemon=True).start()
    except Exception:
        log.exception("快照失敗：%s", path)


def _state(path: str, ent: dict) -> str:
    """現在這個檔跟快照比：same（沒變）／changed／deleted／created（原本沒有，現在有）。"""
    p = Path(path)
    if not ent["existed"]:
        return "created" if p.exists() else "same"
    if not p.exists():
        return "deleted"
    if ent.get("skipped"):
        return "unknown"
    if p.stat().st_size != ent.get("size"):
        return "changed"
    return "changed" if p.read_bytes() != _content(ent["_dir"], ent) else "same"


def checkpoints(sid: str) -> list[dict]:
    base = ROOT / sid
    if not base.exists():
        return []
    out = []
    for d in _cps(base):
        man = _read(d)
        files = []
        for path, ent in man.get("files", {}).items():
            ent = {**ent, "_dir": d}
            try:
                st = _state(path, ent)
            except Exception:
                st = "unknown"
            files.append({"path": path, "existed": ent["existed"], "state": st, "skipped": ent.get("skipped")})
        out.append({"mid": int(d.name), "created": man.get("created"), "files": files})
    undo = base / UNDO
    if undo.exists():
        out_undo = _read(undo)
        return out + [{"mid": UNDO, "created": out_undo.get("created"), "target": out_undo.get("target"),
                       "files": [{"path": p, "existed": e["existed"]} for p, e in out_undo.get("files", {}).items()]}]
    return out


def _apply(d: Path, path: str, ent: dict) -> str:
    p = Path(path)
    if ent.get("skipped"):
        return f"略過 {p.name}：{ent['skipped']}"
    if ent["existed"]:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".woof-restore")
        tmp.write_bytes(_content(d, ent))
        tmp.replace(p)
        return ""
    if p.is_file():
        p.unlink()
    return ""


def restore(sid: str, mid: int, only: str | None = None) -> dict:
    """把檔案還原到第 mid 則訊息送出之前。only＝只還原這個檔。
    取 mid 之後（含）每個檔最早的那份快照；還原前先把現況存到 _undo。"""
    base = ROOT / sid
    cps = sorted((x for x in base.iterdir() if x.is_dir() and x.name.isdigit() and int(x.name) >= mid),
                 key=lambda x: int(x.name)) if base.exists() else []
    plan: dict[str, tuple[Path, dict]] = {}
    for d in cps:
        for path, ent in _read(d).get("files", {}).items():
            if only and path != only:
                continue
            plan.setdefault(path, (d, ent))
    if not plan:
        return {"restored": [], "notes": ["沒有可還原的檔案"]}
    undo = base / UNDO
    shutil.rmtree(undo, ignore_errors=True)
    uman = {"files": {}, "created": time.time(), "target": mid, "only": only}
    for path in plan:
        _capture(undo, uman, Path(path), sid)
    _write(undo, uman)
    done, notes = [], []
    for path, (d, ent) in plan.items():
        try:
            n = _apply(d, path, ent)
            (notes.append(n) if n else done.append(path))
        except Exception as e:
            notes.append(f"{Path(path).name}：{type(e).__name__}: {e}")
    if not only:
        # 用掉的檢查點先收進 _undo，撤銷時放回去
        keep = undo / "cps"
        keep.mkdir(parents=True, exist_ok=True)
        for d in cps:
            shutil.move(str(d), str(keep / d.name))
    return {"restored": done, "notes": notes}


def undo(sid: str) -> dict:
    d = ROOT / sid / UNDO
    man = _read(d)
    if not man.get("files"):
        return {"restored": [], "notes": ["沒有可撤銷的還原"]}
    done, notes = [], []
    for path, ent in man["files"].items():
        try:
            n = _apply(d, path, ent)
            (notes.append(n) if n else done.append(path))
        except Exception as e:
            notes.append(f"{Path(path).name}：{type(e).__name__}: {e}")
    keep = d / "cps"
    if keep.exists():
        for x in keep.iterdir():
            if not (ROOT / sid / x.name).exists():
                shutil.move(str(x), str(ROOT / sid / x.name))
    shutil.rmtree(d, ignore_errors=True)
    return {"restored": done, "notes": notes}


def drop(sid: str) -> None:
    """刪對話時一起清快照。"""
    shutil.rmtree(ROOT / sid, ignore_errors=True)


# ---------------------------------------------------------------- 容量管理
def _cps(base: Path) -> list[Path]:
    if not base.exists():
        return []
    return sorted((x for x in base.iterdir() if x.is_dir() and x.name.isdigit()), key=lambda x: int(x.name))


def _sessions() -> list[Path]:
    if not ROOT.exists():
        return []
    return [x for x in ROOT.iterdir() if x.is_dir() and not x.name.startswith("_")]


def _created(d: Path) -> float:
    return float(_read(d).get("created") or d.stat().st_mtime)


def _dir_bytes(p: Path) -> int:
    n = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                n += f.stat().st_size
        except OSError:
            pass
    return n


def usage() -> dict:
    sess = _sessions()
    cps = sum(len(_cps(s)) for s in sess)
    full = delta = 0
    for b in (BLOBS.rglob("*") if BLOBS.exists() else []):
        if b.is_file():
            with b.open("rb") as f:
                delta += f.read(len(MAGIC_DELTA)) == MAGIC_DELTA
            full += 1
    full -= delta
    logical = 0
    for s in sess:
        for m in s.rglob("manifest.json"):
            try:
                logical += sum(int(e.get("size") or 0) for e in json.loads(m.read_text(encoding="utf-8")).get("files", {}).values() if e.get("sha") or e.get("blob"))
            except Exception:
                pass
    return {"bytes": _dir_bytes(ROOT) if ROOT.exists() else 0, "logical": logical, "sessions": len(sess), "checkpoints": cps,
            "blobs": full + delta, "full": full, "delta": delta, "last_prune": _last_prune, "settings": settings()}


def _referenced() -> set[str]:
    refs: set[str] = set()
    for s in _sessions():
        for man_path in s.rglob("manifest.json"):
            try:
                man = json.loads(man_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            refs |= {e["sha"] for e in (man.get("files") or {}).values() if e.get("sha")}
    # 差異的基底也要留著（沿鏈往回找）
    todo = list(refs)
    while todo:
        p = _obj(todo.pop())
        try:
            with p.open("rb") as f:
                head = f.read(len(MAGIC_DELTA) + 64)
        except OSError:
            continue
        if head.startswith(MAGIC_DELTA):
            base = head[len(MAGIC_DELTA):].decode()
            if base not in refs:
                refs.add(base)
                todo.append(base)
    return refs


def prune(live_sessions: set[str] | None = None) -> dict:
    """照設定整理：刪過期對話、每對話超量的舊檢查點、總量超標的最舊檢查點，最後清掉沒人用的原檔內容。
    live_sessions＝還存在的對話 id；有給就順便刪掉對話已經不在的快照。"""
    global _last_prune
    if not _prune_lock.acquire(blocking=False):
        return {"skipped": "正在整理"}
    try:
        _last_prune = time.time()
        cfg = settings()
        before = _dir_bytes(ROOT) if ROOT.exists() else 0
        removed_cps = removed_sessions = 0
        now = time.time()
        for s in _sessions():
            cps = _cps(s)
            gone = live_sessions is not None and s.name not in live_sessions
            newest = max((_created(d) for d in cps), default=s.stat().st_mtime)
            if gone or now - newest > cfg["retention_days"] * 86400:
                shutil.rmtree(s, ignore_errors=True)
                removed_sessions += 1
                continue
            for d in cps[:-cfg["max_per_session"]] if len(cps) > cfg["max_per_session"] else []:
                shutil.rmtree(d, ignore_errors=True)
                removed_cps += 1
        _gc()
        cap = cfg["max_total_mb"] * 1048576
        if _dir_bytes(ROOT) > cap:
            allcps = sorted(((_created(d), d) for s in _sessions() for d in _cps(s)), key=lambda x: x[0])
            for _, d in allcps:
                shutil.rmtree(d, ignore_errors=True)
                removed_cps += 1
                _gc()
                if _dir_bytes(ROOT) <= cap:
                    break
        for s in _sessions():
            if not any(s.iterdir()):
                s.rmdir()
        after = _dir_bytes(ROOT) if ROOT.exists() else 0
        return {"freed": max(0, before - after), "removed_checkpoints": removed_cps,
                "removed_sessions": removed_sessions, "bytes": after}
    finally:
        _prune_lock.release()


def _gc() -> None:
    if not BLOBS.exists():
        return
    refs = _referenced()
    for f in BLOBS.rglob("*"):
        # 剛寫進來、清單還沒存好的不刪
        if f.is_file() and f.name not in refs and time.time() - f.stat().st_mtime > 120:
            try:
                f.unlink()
            except OSError:
                pass
