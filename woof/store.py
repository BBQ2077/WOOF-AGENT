"""SQLite 儲存：對話、訊息、插件設定。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid

from .config import DATA

_DB = DATA / "woof.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


_db = _conn()
_db.executescript(
    """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  model TEXT,
  effort TEXT,
  engine_state TEXT DEFAULT '{}',
  created REAL,
  updated REAL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta TEXT DEFAULT '{}',
  created REAL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS plugin_state(
  name TEXT PRIMARY KEY,
  enabled INTEGER,
  settings TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS usage_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  session_id TEXT,
  model TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0,
  cache_write INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
CREATE TABLE IF NOT EXISTS prompt_snapshots(
  session_id TEXT PRIMARY KEY,
  fp TEXT,
  gen INTEGER DEFAULT 0,
  text TEXT,
  at REAL
);
"""
)
_db.commit()
_cols = {r["name"] for r in _db.execute("PRAGMA table_info(sessions)").fetchall()}
if "pinned" not in _cols:
    _db.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
if "perm" not in _cols:
    _db.execute("ALTER TABLE sessions ADD COLUMN perm TEXT DEFAULT 'auto'")
if "parent" not in _cols:
    _db.execute("ALTER TABLE sessions ADD COLUMN parent TEXT")
if "archived" not in _cols:
    _db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER DEFAULT 0")
if "grp" not in _cols:
    _db.execute("ALTER TABLE sessions ADD COLUMN grp TEXT")
_db.execute("CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, name TEXT NOT NULL, pos INTEGER DEFAULT 0, collapsed INTEGER DEFAULT 0, created REAL)")
_db.commit()

# ---- 跨對話全文檢索：trigram 分詞（中文可用；不足 3 字的關鍵字改用 LIKE）
# 只收使用者、助理的文字和工具結果（每段前 2000 字）；WOOF 自動發的內部訊息不收。觸發器讓所有寫入自動同步。
_FTS_TEXT = ("(SELECT group_concat(substr(coalesce(json_extract(value,'$.text'),'') || coalesce(json_extract(value,'$.output'),''),1,2000), char(10)) "
             "FROM json_each(new.content) WHERE json_extract(value,'$.type') IN ('text','tool_result'))")
_FTS_SKIP = "json_extract(coalesce(new.meta,'{}'),'$.internal') IS NULL"
_fts_new = _db.execute("SELECT 1 FROM sqlite_master WHERE name='msg_fts'").fetchone() is None
_db.executescript(f"""
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(text, sid UNINDEXED, role UNINDEXED, tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages WHEN {_FTS_SKIP} BEGIN
  INSERT INTO msg_fts(rowid,text,sid,role) VALUES(new.id, {_FTS_TEXT}, new.session_id, new.role);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN
  DELETE FROM msg_fts WHERE rowid=old.id;
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF content ON messages BEGIN
  DELETE FROM msg_fts WHERE rowid=old.id;
  INSERT INTO msg_fts(rowid,text,sid,role) SELECT new.id, {_FTS_TEXT}, new.session_id, new.role WHERE {_FTS_SKIP};
END;
""")
if _fts_new:  # 第一次建索引：把既有訊息補進去
    _db.execute(f"INSERT INTO msg_fts(rowid,text,sid,role) SELECT new.id, {_FTS_TEXT}, new.session_id, new.role "
                f"FROM messages AS new WHERE {_FTS_SKIP}")
_db.commit()


def auto_title(sid: str, text: str) -> dict | None:
    """第一則訊息自動取一句話標題（規則式：取第一句，最多 24 字）。已有標題就不動。"""
    s = get_session(sid)
    if s is None or s.get("title") not in (None, "", "新對話"):
        return s
    t = " ".join((text or "").split())
    if not t:
        return update_session(sid, title="圖片對話")
    cut = next((i for i, ch in enumerate(t[:30]) if i >= 4 and ch in "。！？.!?；;，,：:"), None)
    head = t[:cut] if cut is not None else (t[:24] + ("…" if len(t) > 24 else ""))
    return update_session(sid, title=head.strip(" ，,：:；;。！？.!?") or t[:24])


def log_usage(sid: str, model: str | None, usage: dict) -> None:
    with _lock:
        _db.execute(
            "INSERT INTO usage_log(ts,session_id,model,input_tokens,output_tokens,cache_read,cache_write) VALUES(?,?,?,?,?,?,?)",
            (time.time(), sid, model or "", int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
             int(usage.get("cache_read") or 0), int(usage.get("cache_write") or 0)),
        )
        _db.commit()


def usage_summary(rng: str, sid: str | None) -> dict:
    """rng: today｜7d｜30d｜all；sid 有值時只算該對話（不限時間）。"""
    from datetime import datetime
    mid = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since = {"today": mid, "7d": mid - 6 * 86400, "30d": mid - 29 * 86400}.get(rng, 0.0)
    where, args = "ts>=?", [since]
    if sid:
        where, args = "session_id=?", [sid]
    with _lock:
        tot = _db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),"
            f"COALESCE(SUM(cache_read),0),COALESCE(SUM(cache_write),0) FROM usage_log WHERE {where}", args).fetchone()
        by = _db.execute(
            f"SELECT model,COUNT(*),SUM(input_tokens),SUM(output_tokens),SUM(cache_read) FROM usage_log WHERE {where} "
            f"GROUP BY model ORDER BY SUM(input_tokens)+SUM(output_tokens) DESC", args).fetchall()
        daily = _db.execute(
            f"SELECT date(ts,'unixepoch','localtime') d, COUNT(*), SUM(input_tokens), SUM(output_tokens) "
            f"FROM usage_log WHERE {where} GROUP BY d ORDER BY d", args).fetchall()
    return {
        "range": "session" if sid else rng,
        "calls": tot[0], "input_tokens": tot[1], "output_tokens": tot[2],
        "cache_read": tot[3], "cache_write": tot[4],
        "by_model": [{"model": m or "?", "calls": c, "input_tokens": i or 0, "output_tokens": o or 0, "cache_read": cr or 0}
                     for m, c, i, o, cr in by],
        "daily": [{"day": d, "calls": c, "input_tokens": i or 0, "output_tokens": o or 0} for d, c, i, o in daily],
    }


def usage_rows(since: float, sid: str | None = None) -> list[tuple]:
    """(日期, 模型, 輸入, 輸出, 快取讀取) 依日期與模型加總，算金額用。"""
    where, args = ("session_id=?", [sid]) if sid else ("ts>=?", [since])
    with _lock:
        return _db.execute(
            f"SELECT date(ts,'unixepoch','localtime') d, model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read) "
            f"FROM usage_log WHERE {where} GROUP BY d, model", args).fetchall()


def _session_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["engine_state"] = json.loads(d.get("engine_state") or "{}")
    d["pinned"] = bool(d.get("pinned"))
    d["archived"] = bool(d.get("archived"))
    d["perm"] = d.get("perm") or "auto"
    d["grp"] = d.get("grp") or ""
    return d


def list_sessions() -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM sessions ORDER BY pinned DESC, updated DESC").fetchall()
    return [_session_row(r) for r in rows]


def get_session(sid: str) -> dict | None:
    with _lock:
        r = _db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _session_row(r) if r else None


def create_session(model: str | None, effort: str | None, title: str = "新對話") -> dict:
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    with _lock:
        _db.execute(
            "INSERT INTO sessions(id,title,model,effort,engine_state,created,updated) VALUES(?,?,?,?,?,?,?)",
            (sid, title, model, effort, "{}", now, now),
        )
        _db.commit()
    return get_session(sid)  # type: ignore[return-value]


def update_session(sid: str, **fields) -> dict | None:
    if "engine_state" in fields:
        fields["engine_state"] = json.dumps(fields["engine_state"], ensure_ascii=False)
    fields["updated"] = time.time()
    cols = ",".join(f"{k}=?" for k in fields)
    with _lock:
        _db.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))
        _db.commit()
    return get_session(sid)


def list_groups() -> list[dict]:
    """對話群組：介面側欄的分類。pos 小的排前面。"""
    with _lock:
        rows = _db.execute("SELECT * FROM groups ORDER BY pos, created").fetchall()
        cnt = {gid: n for gid, n in _db.execute(
            "SELECT grp, COUNT(*) FROM sessions WHERE grp IS NOT NULL AND archived=0 GROUP BY grp").fetchall()}
    return [{"id": r["id"], "name": r["name"], "pos": r["pos"],
             "collapsed": bool(r["collapsed"]), "n": cnt.get(r["id"], 0)} for r in rows]


def create_group(name: str) -> dict:
    gid = uuid.uuid4().hex[:8]
    with _lock:
        pos = (_db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM groups").fetchone()[0]) or 0
        _db.execute("INSERT INTO groups(id, name, pos, collapsed, created) VALUES(?,?,?,0,?)",
                    (gid, name, pos, time.time()))
        _db.commit()
    return {"id": gid, "name": name, "pos": pos, "collapsed": False, "n": 0}


def update_group(gid: str, **fields) -> dict | None:
    if "collapsed" in fields:
        fields["collapsed"] = 1 if fields["collapsed"] else 0
    if not fields:
        return None
    cols = ",".join(f"{k}=?" for k in fields)
    with _lock:
        _db.execute(f"UPDATE groups SET {cols} WHERE id=?", (*fields.values(), gid))
        _db.commit()
    return next((g for g in list_groups() if g["id"] == gid), None)


def delete_group(gid: str) -> None:
    with _lock:
        _db.execute("UPDATE sessions SET grp=NULL WHERE grp=?", (gid,))
        _db.execute("DELETE FROM groups WHERE id=?", (gid,))
        _db.commit()


def group_session_ids(gid: str) -> list[str]:
    with _lock:
        return [r[0] for r in _db.execute("SELECT id FROM sessions WHERE grp=?", (gid,)).fetchall()]


def child_ids(sid: str) -> list[str]:
    """子代理對話（含更深一層）的 id，不含自己。"""
    out, todo = [], [sid]
    with _lock:
        while todo:
            cur = todo.pop()
            rows = _db.execute("SELECT id FROM sessions WHERE parent=?", (cur,)).fetchall()
            for (cid,) in rows:
                if cid not in out:
                    out.append(cid)
                    todo.append(cid)
    return out


def delete_session(sid: str) -> None:
    with _lock:
        _db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        _db.execute("DELETE FROM op_log WHERE session_id=?", (sid,))
        _db.execute("DELETE FROM prompt_snapshots WHERE session_id=?", (sid,))
        _db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        _db.commit()


def child_sessions(session_id: str) -> list[dict]:
    """這一條派出去的子代理對話（sessions.parent）。"""
    con = _conn()
    rows = con.execute(
        "SELECT id, title, created FROM sessions WHERE parent = ? ORDER BY created DESC LIMIT 10",
        (session_id,)).fetchall()
    return [dict(r) if isinstance(r, dict) else {"id": r[0], "title": r[1], "created": r[2]} for r in rows]


def upload_names(session_id: str | None = None) -> set[str]:
    """訊息裡提到的 uploads 檔名（session_id=None 表示全部對話）。"""
    from . import uploads

    if session_id is None:
        rows = _db.execute("SELECT content FROM messages WHERE content LIKE '%uploads%' OR content LIKE '%pastes%' OR content LIKE '%files%'").fetchall()
    else:
        rows = _db.execute("SELECT content FROM messages WHERE session_id=? AND (content LIKE '%uploads%' OR content LIKE '%pastes%' OR content LIKE '%files%')",
                           (session_id,)).fetchall()
    names: set[str] = set()
    for r in rows:
        names |= uploads.names_in(r["content"] or "")
    return names


def all_upload_names() -> set[str]:
    return upload_names(None)


def add_message(sid: str, role: str, content: list, meta: dict | None = None) -> dict:
    now = time.time()
    with _lock:
        # 對話剛被刪掉時（回合還在收尾），不寫入孤兒訊息
        cur = _db.execute(
            "INSERT INTO messages(session_id,role,content,meta,created) SELECT ?,?,?,?,? "
            "WHERE EXISTS(SELECT 1 FROM sessions WHERE id=?)",
            (sid, role, json.dumps(content, ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False), now, sid),
        )
        _db.execute("UPDATE sessions SET updated=? WHERE id=?", (now, sid))
        _db.commit()
        mid = cur.lastrowid if cur.rowcount else None
    return {"id": mid, "session_id": sid, "role": role, "content": content, "meta": meta or {}, "created": now}


def get_messages(sid: str, after: int = 0) -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id", (sid, after)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["content"] = json.loads(d["content"])
        d["meta"] = json.loads(d["meta"] or "{}")
        out.append(d)
    return out


def delete_messages_after(sid: str, mid: int) -> int:
    """回溯用：刪掉 session 內 id > mid 的訊息（不含 mid 本身）與其後的計時紀錄。回傳刪掉的訊息數。"""
    with _lock:
        row = _db.execute("SELECT created FROM messages WHERE session_id=? AND id=?", (sid, mid)).fetchone()
        if row is None:
            return -1
        cut = row["created"] if hasattr(row, "keys") else row[0]
        n = _db.execute("DELETE FROM messages WHERE session_id=? AND id>?", (sid, mid)).rowcount
        _db.execute("DELETE FROM op_log WHERE session_id=? AND ts>?", (sid, cut))
        _db.commit()
    return n


def mark_message_stopped(mid: int) -> bool:
    """把已寫入的 assistant 訊息標成已停止：meta.stopped=True，最後一段文字加「（已停止）」（與串流中途停止的寫法一致）。"""
    with _lock:
        row = _db.execute("SELECT content, meta FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None:
            return False
        content = json.loads(row[0]); meta = json.loads(row[1] or "{}")
        if meta.get("stopped"):
            return True
        texts = [b for b in content if b.get("type") == "text"]
        if texts and not texts[-1].get("text", "").endswith("（已停止）"):
            texts[-1]["text"] = texts[-1].get("text", "") + "\n\n（已停止）"
        meta["stopped"] = True
        _db.execute("UPDATE messages SET content=?, meta=? WHERE id=?",
                    (json.dumps(content, ensure_ascii=False), json.dumps(meta, ensure_ascii=False), mid))
        _db.commit()
    return True


def get_message(mid: int) -> dict | None:
    with _lock:
        r = _db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content"] = json.loads(d["content"])
    d["meta"] = json.loads(d["meta"] or "{}")
    return d


def delete_from(sid: str, mid: int) -> int:
    """刪掉這則（含）之後的訊息，用於編輯重送與重新生成。"""
    with _lock:
        cur = _db.execute("DELETE FROM messages WHERE session_id=? AND id>=?", (sid, mid))
        _db.commit()
        return cur.rowcount


def fork_session(sid: str, upto: int, title: str) -> dict | None:
    """複製到這則（含）為止的訊息，成為新對話（upto=0＝整段）。

    一起帶過去：壓縮摘要（訊息 id 換成新的）、系統提示詞快照（前綴一樣，模型那邊的快取可以接著用）、權限模式。
    """
    src = get_session(sid)
    if src is None:
        return None
    new = create_session(src.get("model"), src.get("effort"), title)
    idmap: dict[int, int] = {}
    with _lock:
        rows = _db.execute("SELECT id,role,content,meta,created FROM messages WHERE session_id=?"
                           + (" AND id<=?" if upto else "") + " ORDER BY id",
                           (sid, upto) if upto else (sid,)).fetchall()
        for r in rows:
            cur = _db.execute("INSERT INTO messages(session_id,role,content,meta,created) VALUES(?,?,?,?,?)",
                              (new["id"], r["role"], r["content"], r["meta"], r["created"]))
            idmap[r["id"]] = cur.lastrowid
        snap = _db.execute("SELECT fp,gen,text,at FROM prompt_snapshots WHERE session_id=?", (sid,)).fetchone()
        if snap:
            _db.execute("INSERT OR REPLACE INTO prompt_snapshots(session_id,fp,gen,text,at) VALUES(?,?,?,?,?)",
                        (new["id"], snap["fp"], snap["gen"], snap["text"], snap["at"]))
        _db.commit()
    es: dict = {"forked_from": {"id": sid, "title": src.get("title"), "upto": upto or (max(idmap) if idmap else 0)}}
    summ = (src.get("engine_state") or {}).get("summary")
    if summ and summ.get("text"):
        through = int(summ.get("through") or 0)
        mapped = max((n for o, n in idmap.items() if o <= through), default=0)
        if mapped:
            s2 = {k: v for k, v in summ.items() if k != "inflight"}
            s2["through"] = mapped
            if isinstance(s2.get("kept"), list):
                s2["kept"] = [idmap.get(x, x) if isinstance(x, int) else x for x in s2["kept"]]
            es["summary"] = s2
    pr = (src.get("engine_state") or {}).get("prune")
    if pr and pr.get("through"):
        mapped = max((n for o, n in idmap.items() if o <= int(pr["through"])), default=0)
        if mapped:
            es["prune"] = {**pr, "through": mapped}
    return update_session(new["id"], engine_state=es, perm=src.get("perm") or "auto", created=src.get("created") or time.time())


def _snip(text: str, terms: list[str], width: int = 70) -> str:
    low = text.lower()
    pos = min((p for p in (low.find(t.lower()) for t in terms) if p >= 0), default=0)
    text = re.sub(r"[*`#>]+|\[(.*?)\]\(.*?\)", lambda m: m.group(1) or " ", text)  # 拿掉 Markdown 記號
    low = text.lower()
    pos = min((p for p in (low.find(t.lower()) for t in terms) if p >= 0), default=0)
    a = max(0, pos - width // 3)
    s = " ".join(text[a:a + width].split())
    return ("…" if a else "") + s + ("…" if a + width < len(text) else "")


def search_messages(q: str, limit: int = 30, sid: str | None = None, exclude: str | None = None,
                    after: float | None = None) -> list[dict]:
    """全文檢索：關鍵字都 ≥3 字用 trigram MATCH（依相關度），否則用 LIKE（依時間）。回傳每則符合的訊息與片段。"""
    terms = [t.strip('"') for t in q.split() if t.strip('"')]
    if not terms:
        return []
    where, args = [], []
    if all(len(t) >= 3 for t in terms):
        where.append("msg_fts MATCH ?")
        args.append(" ".join('"' + t.replace('"', '""') + '"' for t in terms))
        order = "rank"
    else:
        for t in terms:
            where.append("f.text LIKE ? ESCAPE '\\'")
            args.append("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        order = "m.id DESC"
    if sid:
        where.append("f.sid=?"); args.append(sid)
    if exclude:
        where.append("f.sid<>?"); args.append(exclude)
    if after:
        where.append("m.created>=?"); args.append(after)
    sql = (f"SELECT f.rowid AS mid, f.sid, f.role, f.text, m.created, s.title, s.model, s.parent, s.archived "
           f"FROM msg_fts f JOIN messages m ON m.id=f.rowid JOIN sessions s ON s.id=f.sid "
           f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?")
    with _lock:
        rows = _db.execute(sql, (*args, limit)).fetchall()
    return [{"mid": r["mid"], "session_id": r["sid"], "role": r["role"], "created": r["created"], "title": r["title"],
             "model": r["model"], "parent": r["parent"], "archived": bool(r["archived"]), "snippet": _snip(r["text"] or "", terms)}
            for r in rows]


# ---- 系統提示詞快照：每個對話第一次送出時組好就固定，模型那邊的前綴快取才會一直有效
def get_prompt_snapshot(sid: str) -> dict | None:
    with _lock:
        r = _db.execute("SELECT fp,gen,text,at FROM prompt_snapshots WHERE session_id=?", (sid,)).fetchone()
    return dict(r) if r else None


def set_prompt_snapshot(sid: str, fp: str, gen: int, text: str) -> None:
    with _lock:
        _db.execute("INSERT INTO prompt_snapshots(session_id,fp,gen,text,at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET fp=excluded.fp,gen=excluded.gen,text=excluded.text,at=excluded.at",
                    (sid, fp, gen, text, time.time()))
        _db.commit()


def clear_prompt_snapshot(sid: str) -> None:
    with _lock:
        _db.execute("DELETE FROM prompt_snapshots WHERE session_id=?", (sid,))
        _db.commit()


def get_plugin_state(name: str) -> dict | None:
    with _lock:
        r = _db.execute("SELECT * FROM plugin_state WHERE name=?", (name,)).fetchone()
    if not r:
        return None
    return {"enabled": None if r["enabled"] is None else bool(r["enabled"]),
            "settings": json.loads(r["settings"] or "{}")}


def set_plugin_state(name: str, enabled: bool | None = None, settings: dict | None = None) -> None:
    cur = get_plugin_state(name) or {"enabled": None, "settings": {}}
    if enabled is not None:
        cur["enabled"] = enabled
    if settings is not None:
        cur["settings"] = settings
    with _lock:
        _db.execute(
            "INSERT INTO plugin_state(name,enabled,settings) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, settings=excluded.settings",
            (name, None if cur["enabled"] is None else int(cur["enabled"]), json.dumps(cur["settings"], ensure_ascii=False)),
        )
        _db.commit()


def session_stats(sid: str) -> dict:
    """單一對話的統計：回合、模型呼叫、工具呼叫、用量。由 messages 與 usage_log 即時算，不另存。"""
    msgs = get_messages(sid)
    turns = model_calls = tool_calls = tool_errors = 0
    for m in msgs:
        meta = m.get("meta") or {}
        if m["role"] == "user" and not meta.get("internal"):
            turns += 1
        elif m["role"] == "assistant":
            model_calls += 1
            tool_calls += sum(1 for b in m["content"] if b.get("type") == "tool_call")
        elif m["role"] == "tool":
            tool_errors += sum(1 for b in m["content"] if b.get("type") == "tool_result" and b.get("is_error"))
    with _lock:
        r = _db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o,"
            " COALESCE(SUM(cache_read),0) AS cr, COALESCE(SUM(cache_write),0) AS cw FROM usage_log WHERE session_id=?",
            (sid,),
        ).fetchone()
    return {
        "turns": turns, "messages": len(msgs), "model_calls": model_calls,
        "tool_calls": tool_calls, "tool_errors": tool_errors,
        "usage": {"calls": r["n"], "input_tokens": r["i"], "output_tokens": r["o"],
                  "cache_read": r["cr"], "cache_write": r["cw"]},
        "timing": timing_stats(sid),
    }


_db.execute(
    "CREATE TABLE IF NOT EXISTS op_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session_id TEXT, "
    "kind TEXT, name TEXT, ms REAL, ttft_ms REAL, is_error INTEGER DEFAULT 0)"
)
_db.execute("CREATE INDEX IF NOT EXISTS idx_op_sid ON op_log(session_id)")
_db.commit()


def log_op(sid: str, kind: str, name: str | None, ms: float, ttft: float | None = None, err: bool = False) -> None:
    """記錄一次模型呼叫（kind=model）或工具呼叫（kind=tool）的耗時，毫秒。"""
    with _lock:
        _db.execute(
            "INSERT INTO op_log(ts,session_id,kind,name,ms,ttft_ms,is_error) VALUES(?,?,?,?,?,?,?)",
            (time.time(), sid, kind, name or "", float(ms), None if ttft is None else float(ttft), 1 if err else 0),
        )
        _db.commit()


def timing_stats(sid: str) -> dict:
    with _lock:
        rows = _db.execute("SELECT kind,name,ms,ttft_ms,is_error FROM op_log WHERE session_id=?", (sid,)).fetchall()
    model = [r for r in rows if r["kind"] == "model"]
    tools = [r for r in rows if r["kind"] == "tool"]
    ttfts = [r["ttft_ms"] for r in model if r["ttft_ms"] is not None]
    by_tool: dict[str, dict] = {}
    for r in tools:
        d = by_tool.setdefault(r["name"], {"name": r["name"], "calls": 0, "ms": 0.0, "errors": 0})
        d["calls"] += 1
        d["ms"] += r["ms"]
        d["errors"] += r["is_error"]
    for d in by_tool.values():
        d["avg_ms"] = round(d["ms"] / d["calls"]) if d["calls"] else 0
        d["ms"] = round(d["ms"])
    return {
        "model_calls": len(model),
        "avg_ttft_ms": round(sum(ttfts) / len(ttfts)) if ttfts else None,
        "model_ms_total": round(sum(r["ms"] for r in model)),
        "avg_model_ms": round(sum(r["ms"] for r in model) / len(model)) if model else None,
        "tool_calls": len(tools),
        "tool_ms_total": round(sum(r["ms"] for r in tools)),
        "tools": sorted(by_tool.values(), key=lambda d: -d["ms"]),
    }
