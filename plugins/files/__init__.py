"""檔案工具：read_file、write_file、patch、search_files、list_tree。"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from woof import snapshot


def _resolve(p: str, cwd: str) -> Path:
    p = p.strip()
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)  # /c/Users → C:/Users
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else Path(cwd) / path


def _in_scope(path: Path, ctx) -> bool:
    """寫入子代理只能改 write_scope 之內的路徑；None 表示不限制。"""
    # 工具拿到的是 ToolContext，真正的子代理設定在 .turn 上
    turn = getattr(ctx, "turn", ctx)
    scope = getattr(turn, "write_scope", None)
    if scope is None:
        return True
    p = path.resolve()
    return any(p == s or s in p.parents for s in scope)


def _col_of(ref: str) -> int:
    """B12 → 2（1 起算）。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n


def _xlsx_digest(path: Path, rows: int = 20, start: int = 1, raw: bool = False) -> str:
    """用標準庫拆 xlsx（zip + xml）：工作表、範圍、欄位、前幾列。
    模型一次就看懂這張表長什麼樣，不必開十幾支 terminal 反覆試 pandas／openpyxl。"""
    import zipfile
    import xml.etree.ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        return f"這個 xlsx 打不開（{e}）"

    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(f"{NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")):
            rels[r.get("Id")] = r.get("Target", "")
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = [(s.get("name"), rels.get(s.get(rid_attr), "")) for s in wb.iter(f"{NS}sheet")]

    if raw:
        lines: list[str] = []
        for idx, (name, target) in enumerate(sheets, 1):
            if idx < max(1, start):
                continue
            member = "xl/" + str(target).lstrip("/")
            if not member.startswith("xl/"):
                member = "xl/" + member
            if member not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(member))
            lines.append(f"# 工作表 {idx} {name}（TSV，{path.name}）")
            for r in root.iter(f"{NS}row"):
                cells: dict[int, str] = {}
                for c in r.findall(f"{NS}c"):
                    v = c.find(f"{NS}v")
                    if v is None:
                        continue
                    val = v.text or ""
                    if c.get("t") == "s":
                        try:
                            val = shared[int(val)]
                        except Exception:
                            pass
                    cells[_col_of(c.get("r", "A1"))] = str(val).replace("\t", " ").replace("\n", " ")
                if cells:
                    lines.append("\t".join(cells.get(i, "") for i in range(1, max(cells) + 1)))
                if len(lines) > 400000:
                    break
            lines.append("")
        zf.close()
        return "\n".join(lines)

    out = [f"{path.name}（{path.stat().st_size / 1024:.0f} KB，{len(sheets)} 個工作表）",
           "工作表：" + " / ".join(f"{i} {n}" for i, (n, _t) in enumerate(sheets, 1))]
    budget = 7000   # 一次讀完的預算：模型不用為了一張表來回十幾次
    for idx, (name, target) in enumerate(sheets, 1):
        if idx < max(1, start):
            continue
        member = "xl/" + str(target).lstrip("/")
        if not member.startswith("xl/"):
            member = "xl/" + member
        if member not in zf.namelist():
            out.append(f"\n[{idx}] {name}（讀不到內容）")
            continue
        root = ET.fromstring(zf.read(member))
        dim = root.find(f"{NS}dimension")
        span = dim.get("ref") if dim is not None else ""
        nrows = 0
        body: list[str] = []
        for r in root.iter(f"{NS}row"):
            nrows += 1
            if len(body) >= rows or budget <= 0:
                continue
            cells: dict[int, str] = {}
            for c in r.findall(f"{NS}c"):
                v = c.find(f"{NS}v")
                if v is None:
                    t = c.find(f"{NS}is/{NS}t")
                    val = t.text if t is not None else ""
                else:
                    val = v.text or ""
                    if c.get("t") == "s":
                        try:
                            val = shared[int(val)]
                        except Exception:
                            pass
                col = _col_of(c.get("r", "A1"))
                if val != "":
                    cells[col] = str(val).replace("\n", " ")[:40]
            if cells:
                width = max(cells)
                line = "|".join(cells.get(k, "") for k in range(1, width + 1))[:240]
                body.append(line)
                budget -= len(line) + 5
        head = f"{span}，" if span else ""
        out.append(f"\n[{idx}] {name}　（{head}{nrows} 列）")
        out.extend("    " + line for line in body)
        if nrows > len(body):
            where = "篇幅到上限了" if budget <= 0 else "把 limit 加大"
            out.append(f"    …還有 {nrows - len(body)} 列（{where}，或 terminal 指定儲存格）")
        if budget <= 0:
            out.append(f"\n（後面還有 {len(sheets) - idx} 個工作表沒列出；同一支 read_file 加 offset={idx + 1} 從下一個看起）")
            break
    zf.close()
    out.append("\n（要看整張表的原始數值：同一個檔再 read_file 一次，limit 設 200000、offset=工作表編號，"
               "會直接吐 TSV；不需要 python／openpyxl）")
    return "\n".join(out)


def register(woof):
    async def read_file(args: dict, ctx) -> dict:
        path = _resolve(args["path"], ctx.cwd)
        if not path.exists():
            return {"output": f"檔案不存在：{path}", "is_error": True}
        if path.is_dir():
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            return {"output": "\n".join(f"{i.name}/" if i.is_dir() else i.name for i in items[:500])}
        offset = max(int(args.get("offset") or 1), 1)
        limit = min(int(args.get("limit") or 2000), 2000)
        raw = path.read_bytes()
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            if int(args.get("limit") or 0) >= 5000:
                txt = _xlsx_digest(path, limit, offset, raw=True)
                if len(txt) > 11000:   # 太長：整份存檔，只回前面幾行＋位置（模型用 read_file/search_files 取區段）
                    out_path = path.with_name(f"{path.stem}.s{offset}.tsv")
                    out_path.write_text(txt, encoding="utf-8", newline="")
                    head = "\n".join(txt.splitlines()[:120])
                    return {"output": f"{head}\n\n（整張表 {len(txt):,} 字已存成檔案：{out_path}"
                                      "；要看某一段就 read_file 那個檔加 offset，要找人／值就用 search_files）"}
                return {"output": txt}
            return {"output": _xlsx_digest(path, limit, offset)}
        if b"\x00" in raw[:4096]:
            hint = ""
            if path.suffix.lower() in (".pdf", ".doc", ".pptx", ".zip", ".7z", ".docx"):
                hint = ("；這種檔要用 terminal 轉文字再看"
                        "（uv run --with pypdf python -c \"import pypdf;print(pypdf.PdfReader(r'%s')"
                        ".pages[0].extract_text())\"）" % path)
            return {"output": f"二進位檔案（{len(raw)} bytes），無法以文字讀取{hint}", "is_error": True}
        lines = raw.decode("utf-8", errors="replace").splitlines()
        part = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(f"{i}|{l}" for i, l in enumerate(part, start=offset))
        more = f"\n…（共 {len(lines)} 行，下一段 offset={offset + limit}）" if offset - 1 + limit < len(lines) else ""
        return {"output": (body or "(空檔案)") + more}

    async def write_file(args: dict, ctx) -> dict:
        path = _resolve(args["path"], ctx.cwd)
        if not _in_scope(path, ctx):
            return {"output": f"範圍外，不能寫入：{path}", "is_error": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        content = args.get("content", "")
        snapshot.before_write(ctx, path)
        path.write_text(content, encoding="utf-8", newline="")
        back = path.read_text(encoding="utf-8")
        ok = back == content
        return {"output": f"已寫入 {path}（{len(content.encode())} bytes）" + ("" if ok else "，但讀回內容不一致"),
                "is_error": not ok}

    async def patch(args: dict, ctx) -> dict:
        path = _resolve(args["path"], ctx.cwd)
        if not _in_scope(path, ctx):
            return {"output": f"範圍外，不能修改：{path}", "is_error": True}
        if not path.is_file():
            return {"output": f"檔案不存在：{path}", "is_error": True}
        # newline="" 保留原本的換行（CRLF 檔不會被改成 LF）
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        old, new = args["old_string"], args["new_string"]
        if "\r\n" in text and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        n = text.count(old)
        if n == 0:
            return {"output": "找不到 old_string，請先 read_file 確認原文", "is_error": True}
        if n > 1 and not args.get("replace_all"):
            return {"output": f"old_string 出現 {n} 次，請加長內容讓它唯一，或設定 replace_all", "is_error": True}
        text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        snapshot.before_write(ctx, path)
        path.write_text(text, encoding="utf-8", newline="")
        return {"output": f"已修改 {path}（取代 {n if args.get('replace_all') else 1} 處）"}

    async def search_files(args: dict, ctx) -> dict:
        root = _resolve(args.get("path") or ".", ctx.cwd)
        pattern = args["pattern"]
        target = args.get("target", "content")
        limit = min(int(args.get("limit") or 100), 500)
        rg = shutil.which("rg")
        if target == "files":
            hits = []
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv")]
                for f in fn:
                    if fnmatch.fnmatch(f, pattern):
                        hits.append(str(Path(dp) / f))
                        if len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
            return {"output": "\n".join(hits) or "沒有找到"}
        if rg:
            cmd = [rg, "-n", "--no-heading", "--max-columns", "300", "-m", "20", pattern, str(root)]
            if args.get("glob"):
                cmd[1:1] = ["-g", args["glob"]]
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = proc.stdout.decode("utf-8", "replace").splitlines()
            return {"output": "\n".join(lines[:limit]) + (f"\n…共 {len(lines)} 筆" if len(lines) > limit else "")
                    if lines else "沒有找到"}
        rx = re.compile(pattern)
        out = []
        for p in root.rglob(args.get("glob") or "*"):
            if p.is_file() and len(out) < limit:
                try:
                    for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            out.append(f"{p}:{i}:{line[:300]}")
                except OSError:
                    pass
        return {"output": "\n".join(out) or "沒有找到"}

    SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                 ".ruff_cache", ".idea", ".next", ".tox", ".cache"}

    def _tree(root: Path, depth: int, cap: int) -> str:
        """每個資料夾最多列 per 項（資料夾優先），整體最多 cap 項；最近修改看全部掃到的檔案。"""
        import time as _t
        per = max(15, cap // 10)
        recent: list = []
        count = 0

        def entries(d: Path):
            try:
                return sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())), None
            except OSError as e:
                return [], e.strerror or str(e)

        def walk(d: Path, level: int, prefix: str) -> list[str]:
            nonlocal count
            out: list[str] = []
            items, err = entries(d)
            if err:
                return [f"{prefix}（讀不到：{err}）"]
            shown = 0
            for it in items:
                try:
                    is_dir = it.is_dir()
                    st = it.stat()
                except OSError:
                    continue
                if not is_dir:
                    recent.append((st.st_mtime, it))
                if shown >= per or count >= cap:
                    continue
                shown += 1
                count += 1
                if is_dir and it.name in SKIP_DIRS:
                    out.append(f"{prefix}{it.name}/  （略過）")
                elif is_dir:
                    out.append(f"{prefix}{it.name}/")
                    if level < depth:
                        out.extend(walk(it, level + 1, prefix + "  "))
                else:
                    size = st.st_size
                    sz = f"{size}B" if size < 1024 else f"{size / 1024:.0f}K" if size < 1048576 else f"{size / 1048576:.1f}M"
                    out.append(f"{prefix}{it.name}  {sz}  {_t.strftime('%m-%d %H:%M', _t.localtime(st.st_mtime))}")
            if len(items) > shown:
                out.append(f"{prefix}…還有 {len(items) - shown} 項")
            return out

        lines = walk(root, 1, "")
        out = f"{root}（{depth} 層）\n" + "\n".join(lines)
        recent.sort(key=lambda x: -x[0])
        if recent:
            out += "\n\n最近修改：\n" + "\n".join(
                f"  {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(m))}  {p.relative_to(root)}" for m, p in recent[:10])
        return out

    async def list_tree(args: dict, ctx) -> dict:
        root = _resolve(args.get("path") or ".", ctx.cwd)
        if not root.is_dir():
            return {"output": f"不是資料夾：{root}", "is_error": True}
        depth = max(1, min(int(args.get("depth") or 2), 4))
        cap = max(50, min(int(args.get("limit") or 400), 2000))
        return {"output": await asyncio.to_thread(_tree, root, depth, cap)}

    woof.add_tool("list_tree", "列出資料夾的樹狀結構（預設 2 層、最多 4 層），附檔案大小、修改時間和最近修改的 10 個檔案；"
                  "略過 .git、node_modules、__pycache__、虛擬環境等。剛接觸一個專案時先用它定位。",
                  {"type": "object", "properties": {
                      "path": {"type": "string", "description": "資料夾，預設工作目錄"},
                      "depth": {"type": "integer", "description": "層數，1 到 4，預設 2"},
                      "limit": {"type": "integer", "description": "最多列幾項，預設 400"}}}, list_tree)
    woof.add_tool("read_file", "讀取文字檔（附行號），或列出資料夾內容。",
                  {"type": "object", "properties": {
                      "path": {"type": "string"}, "offset": {"type": "integer", "description": "起始行，從 1 開始"},
                      "limit": {"type": "integer", "description": "最多行數／xlsx 最多列數，上限 2000。xlsx 會直接給工作表、範圍、欄位與前幾列，不要自己開 python 拆；要看整張表的原始數值就同一個檔加 limit=200000（吐 TSV，太長會存成 .tsv 檔給你）"}},
                   "required": ["path"]}, read_file)
    woof.add_tool("write_file", "寫入整個檔案（覆蓋），會自動建立資料夾並讀回驗證。",
                  {"type": "object", "properties": {
                      "path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]}, write_file)
    woof.add_tool("patch", "在檔案中找到唯一的一段文字並取代。",
                  {"type": "object", "properties": {
                      "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                      "replace_all": {"type": "boolean"}},
                   "required": ["path", "old_string", "new_string"]}, patch)
    woof.add_tool("search_files", "搜尋檔案內容（正規表示式）或依檔名搜尋（target=files，pattern 為萬用字元）。",
                  {"type": "object", "properties": {
                      "pattern": {"type": "string"}, "path": {"type": "string"},
                      "target": {"type": "string", "enum": ["content", "files"]},
                      "glob": {"type": "string"}, "limit": {"type": "integer"}},
                   "required": ["pattern"]}, search_files)
