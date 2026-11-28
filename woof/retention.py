"""資料保留：暫存 7 天、用量紀錄 30 天，沒有永久留著的暫存。

- 工具長輸出全文（data/spill，含 read_file 把 docx／pptx 轉成的文字和圖）、背景指令紀錄（data/logs）：放 7 天就刪
- watch_video 下載的影片（data/spill/video）：放 1 天就刪
- 用量紀錄（usage_log：模型、token 數、時間）：放 30 天就刪，用量統計最多也只看 30 天
- 已刪對話留下的工具耗時紀錄、系統提示快照：直接刪
開程式時整理一次，之後每 6 小時一次。「設定 → 一般 → 暫存與紀錄」可以一鍵全部清掉，不用等。
"""
from __future__ import annotations

import time
from pathlib import Path

TEMP_DAYS = 7
VIDEO_DAYS = 1
USAGE_DAYS = 30


def _dirs() -> tuple[Path, Path]:
    from woof.config import DATA

    return DATA / "spill", DATA / "logs"


def _running_logs() -> set[str]:
    """還在跑的背景指令紀錄：不刪（刪了模型就看不到輸出）。"""
    from woof import procs

    return {str(t.log_path) for t in list(procs.TASKS.values()) if t.ended is None and t.exit_code is None}


def _files(cut: float | None) -> list[Path]:
    spill, logs = _dirs()
    keep = _running_logs()
    out = []
    vcut = None if cut is None else max(cut, time.time() - VIDEO_DAYS * 86400)
    # spill/docs/<編號>/：read_file 把 docx、pptx 轉成的文字和解出的圖；spill/video/<網址雜湊>/：watch_video 下載的影片
    for d, pat, c in ((spill, "*.txt", cut), (logs, "task_*.log", cut), (spill / "docs", "*/*", cut), (spill / "video", "*/*", vcut)):
        if not d.is_dir():
            continue
        for p in d.glob(pat):
            try:
                if str(p) in keep or (c is not None and p.stat().st_mtime >= c):
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _drop_files(paths: list[Path]) -> tuple[int, int]:
    n = size = 0
    for p in paths:
        try:
            s = p.stat().st_size
            p.unlink()
            n += 1
            size += s
        except OSError:
            pass
        if p.parent.parent.name in ("docs", "video"):
            try:
                p.parent.rmdir()   # 轉檔資料夾清空了就一起刪
            except OSError:
                pass
    return n, size


def _orphans(db, live: set[str] | None = None) -> int:
    """已刪對話留下的工具耗時紀錄、系統提示快照、工具圖留存（刪對話當下它還在跑，之後才寫進來的）。
    直接對資料庫裡現有的對話比對，不會誤刪剛建立的對話。"""
    n = sum(db.execute(f"DELETE FROM {t} WHERE session_id NOT IN (SELECT id FROM sessions)").rowcount
            for t in ("op_log", "prompt_snapshots"))
    import shutil

    from woof.config import DATA

    snap = DATA / "imgsnap"
    if snap.is_dir():
        alive = {r[0] for r in db.execute("SELECT id FROM sessions")}
        for d in snap.iterdir():
            if d.is_dir() and d.name not in alive:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n


def prune(live: set[str]) -> dict:
    """定時整理：超過 7 天的暫存、超過 30 天的用量紀錄、已刪對話的殘留。"""
    from woof import store

    now = time.time()
    files, size = _drop_files(_files(now - TEMP_DAYS * 86400))
    with store._lock:
        usage = store._db.execute("DELETE FROM usage_log WHERE ts<?", (now - USAGE_DAYS * 86400,)).rowcount
        orphans = _orphans(store._db, live)
        store._db.commit()
    return {"files": files, "bytes": size, "usage_rows": usage, "orphan_rows": orphans}


def stats() -> dict:
    """設定頁顯示：現在有多少可以清。"""
    from woof import store

    paths = _files(None)
    size = 0
    for p in paths:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    with store._lock:
        usage = store._db.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
        oldest = store._db.execute("SELECT MIN(ts) FROM usage_log").fetchone()[0]
    from woof.config import DATA

    db_size = sum((DATA / n).stat().st_size for n in ("woof.db", "woof.db-wal") if (DATA / n).exists())
    return {"files": len(paths), "bytes": size, "usage_rows": usage, "usage_oldest": oldest,
            "db_bytes": db_size, "temp_days": TEMP_DAYS, "usage_days": USAGE_DAYS}


def purge_all(live: set[str]) -> dict:
    """一鍵清除：暫存全刪（還在跑的指令除外）、用量紀錄全刪、已刪對話的殘留全刪，
    再重整資料庫（VACUUM），把刪掉的對話留在檔案空白處的舊內容一起清掉。對話本身不動。"""
    from woof import store
    from woof.config import DATA

    files, size = _drop_files(_files(None))
    with store._lock:
        usage = store._db.execute("DELETE FROM usage_log").rowcount
        orphans = _orphans(store._db, live)
        store._db.commit()
        before = sum((DATA / n).stat().st_size for n in ("woof.db", "woof.db-wal") if (DATA / n).exists())
        vac = ""
        try:
            store._db.execute("VACUUM")
            store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:   # 有別的連線正在讀寫：資料已刪，只是舊內容要等下次重整
            vac = str(e)
        after = sum((DATA / n).stat().st_size for n in ("woof.db", "woof.db-wal") if (DATA / n).exists())
    return {"files": files, "bytes": size, "usage_rows": usage, "orphan_rows": orphans,
            "db_before": before, "db_after": after, "vacuum_error": vac}
