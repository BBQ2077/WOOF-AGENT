"""網頁內容整理：清雜訊、標題目錄、關鍵字段落、預覽（純文字比對，不叫模型）。

存檔是 clean() 的結果（保留連結）；給模型看的預覽與全文再經 plain() 拿掉站內連結網址、表格格線。
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

_IMG = re.compile(r"!\[[^\]]*\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))*)(?:\s+\"[^\"]*\")?\)")
_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HEAD6 = re.compile(r"^#{1,6}\s+\S")
_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_JUNK = re.compile(r"[\s*\-+|•·>#:：,，.。/\\()（）\[\]_`'\"!?！？¶»«\u200b]+")
_SYM = re.compile(r'[{}\[\]":,\\<>=;]')
_CITE = re.compile(r"\[\[[^\]]{1,12}\]\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+\"[^\"]*\")?\)|\[\d{1,3}\](?!\()")
_TABSEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_SENT = re.compile(r"(?<=[。！？；])|(?<=[.!?;])\s+|\s+\|\s+|<br>")
# 只是網站外框、不是內文的固定行（編輯鈕、跳到內容、錨點、登入提示）
_EDIT = re.compile(r"^\s*(\[?\s*(编辑|編輯|edit|编辑源代码)\s*\]?|permalink:.*|skip to (main )?content|跳转到内容|跳至內容|跳到主要內容|"
                   r"dismiss alert|you (signed in|signed out|switched accounts) .*refresh your session\..*|"
                   r"loading(\.\.\.|…)?|載入中(\.\.\.|…)?|加载中(\.\.\.|…)?|uh oh!|"
                   r"there was (a problem|an error) (hiding|loading) this (comment|page)\.?|"
                   r"this (question|answer) (shows|does not show) .*|(is|was) this (answer|page) (helpful|useful)\??|"
                   r"save this (question|answer)\.?|show activity on this post\.?|.{1,40} toggle|toggle .{1,20})\s*$", re.I)
# 沒套到資料的前端樣板（{{ item.subject }}）
_TPL = re.compile(r"\{\{[^{}\n]{1,80}\}\}")
# 網頁快照站（文章列表附的 Archive.org、Ghostarchive 之類），給模型看時拿掉
_MIRROR = {"web.archive.org", "ghostarchive.org", "archive.ph", "archive.today", "archive.is"}
# 留言數、回答數這類不是章節的標題
_COUNT = re.compile(r"^\s*\d[\d,.]*k?\s*(comments?|answers?|replies|repl(y|ies)|votes?|則?留言|則?回應|個?回答|条评论|個?回覆)\b.*$", re.I)

# 繁→簡單字表（OpenCC），比對關鍵字時兩邊都轉成簡體再比，字數不變、位置對得上
_TS = {}
try:
    for _ln in Path(__file__).with_name("ts_chars.txt").read_text(encoding="utf-8").splitlines():
        if _ln and not _ln.startswith("#"):
            for _i in range(0, len(_ln) - 1, 2):
                _TS[ord(_ln[_i])] = _ln[_i + 1]
except OSError:
    pass


def fold(s: str) -> str:
    return s.lower().translate(_TS)


def _is_fence(s: str) -> bool:
    return s.lstrip().startswith("```")


def _machine(s: str) -> bool:
    """壓縮過的 JSON／JS 這類機器資料（很長、符號很密）。"""
    return len(s) > 1500 and len(_SYM.findall(s)) / len(s) > 0.12


def _link_texts(s: str) -> list[str]:
    return [m.group(1) for m in _LINK.finditer(s)]


def _meaningful(t: str) -> bool:
    return bool(_JUNK.sub("", t))


def _width(s: str) -> int:
    return sum(2 if ord(c) > 0x2e80 else 1 for c in _JUNK.sub("", s))


def _nav_line(s: str) -> bool:
    """幾乎整行都是連結、而且連結字短（選單、導覽、頁尾）；整行一個長標題的是文章列表，不算。"""
    links = _link_texts(s)
    if not links or len(_JUNK.sub("", _LINK.sub("", s))) > 3:
        return False
    return len(links) >= 3 or max(_width(x) for x in links) < 30


def _line_clean(s: str) -> str:
    s = _IMG.sub("", s)
    s = _LINK.sub(lambda m: m.group(0) if _meaningful(m.group(1)) else "", s)   # 空連結、只有 ¶ 之類符號的錨點
    if not s.lstrip().startswith("|"):
        s = _ESC.sub(r"\1", s)
    return s.rstrip()


def _split_blocks(lines: list[str]) -> list[list[int]]:
    """依空行分段，程式碼區塊整段算一段；回傳每段的行索引。"""
    out, cur, fence = [], [], False
    for i, s in enumerate(lines):
        if _is_fence(s):
            fence = not fence
            cur.append(i)
            if not fence:
                out.append(cur)
                cur = []
            continue
        if fence:
            cur.append(i)
            continue
        if not s.strip() or _HEAD6.match(s):
            if cur:
                out.append(cur)
            cur = []
            if s.strip():
                out.append([i])
            continue
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def clean(md: str) -> str:
    """去掉圖片、空連結、選單連結行、機器資料、頭尾重複的側欄、沒有內文的空標題；多個空行合成一個。"""
    md = _IMG.sub("", (md or "").replace("\r\n", "\n").replace("\r", "\n"))
    # 連結文字裡夾圖片和強制換行（新聞列表常見）：拿掉圖片後把斷開的「[」接回同一行
    md = re.sub(r"\[(?:[ \t]*\\{0,2}[ \t]*\n)+[ \t]*(?=[^\n]*\]\()", "[", md)
    lines = md.split("\n")
    fence, kept = False, []
    nav = [False] * len(lines)
    for i, s in enumerate(lines):
        if _is_fence(s):
            fence = not fence
        elif not fence:
            nav[i] = _nav_line(s)
    fence = False
    for i, s in enumerate(lines):
        if not fence and not _is_fence(s):
            # 清單項目裡的標題（「1. ## 標題」、縮排的「    ## 標題」）拉回成一般標題
            m = re.match(r"^\s{0,8}(?:(?:\d+\.|[-*+])\s+)?(#{1,6}\s+\S.*)$", s)
            if m:
                s = m.group(1)
        if _is_fence(s) or fence:
            if _is_fence(s):
                fence = not fence
            kept.append(s.rstrip())
            continue
        if _machine(s):
            continue
        if _HEAD6.match(s):
            if len(_link_texts(s)) >= 3:
                continue   # 一排連結做成的標題（站內選單）
            # 標題是指向本頁錨點的連結（章節的 ¶）：只留文字；連到別頁的（文章列表）留著
            s = _LINK.sub(lambda m: m.group(1) if "#" in m.group(2) else m.group(0), s)
        elif _TPL.search(s) and not _meaningful(_TPL.sub("", s)):
            continue
        elif nav[i]:
            run = sum(1 for j in (i - 1, i + 1) if 0 <= j < len(lines) and nav[j]) + 1
            if run > 1 or len(_link_texts(s)) >= 3:
                continue
        s = _line_clean(s)
        if not _meaningful(s) and not _TABSEP.match(s or "x"):
            s = ""
        kept.append(s)
    lines = kept

    # 頭尾重複的區塊（側欄、頁首頁尾印兩次）整個拿掉；中段重複且夠長的只留第一次
    blocks = _split_blocks(lines)
    seen: dict[str, list[int]] = {}
    for bi, idx in enumerate(blocks):
        key = re.sub(r"\s+", " ", "\n".join(lines[i] for i in idx)).strip()
        if key and not _is_fence(lines[idx[0]]):
            seen.setdefault(key, []).append(bi)
    drop = set()
    nb = len(blocks)
    for key, bis in seen.items():
        if len(bis) < 2:
            continue
        if bis[0] < nb * 0.25 and bis[-1] >= nb * 0.75 and not _HEAD.match(key):
            drop.update(bis)
        elif len(key) >= 40:
            drop.update(bis[1:])
    # 段落後面把文中的 `程式碼` 再一行一個列一次（擷取服務的副產品）：整段都是單獨的 `…` 就拿掉
    span = re.compile(r"^\s*`[^`]{1,80}`\s*$")
    for bi, idx in enumerate(blocks):
        ls = [lines[i] for i in idx if lines[i].strip()]
        if ls and all(span.match(x) for x in ls):
            prev = "\n".join(lines[i] for i in blocks[bi - 1]) if bi else ""
            if len(ls) >= 2 or ls[0].strip() in prev:
                drop.add(bi)
    gone = {i for bi in drop for i in blocks[bi]}
    lines = ["" if i in gone else s for i, s in enumerate(lines)]

    # 底下只有幾個短連結的標題（「Latest commit」「Bookmark」這類介面區塊）：拿掉標題
    heads = [i for i, s in enumerate(lines) if _HEAD6.match(s)]
    lv = {i: len(lines[i]) - len(lines[i].lstrip("#")) for i in heads}
    for k, i in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        if end < len(lines) and lv[end] > lv[i]:
            continue   # 底下還有子標題的是章節
        sec = "\n".join(lines[i + 1:end])
        links = _link_texts(sec)
        if links and not _LINK.search(lines[i]) and len(_JUNK.sub("", _LINK.sub("", sec))) < 15 and sum(len(x) for x in links) < 150:
            lines[i] = ""

    # 沒有內文的標題（底下到下一個同級或更高的標題之間沒有字），反覆做到沒有為止
    while True:
        heads = [(i, len(s) - len(s.lstrip("#"))) for i, s in enumerate(lines) if _HEAD6.match(s)]
        empty = []
        for k, (i, lv) in enumerate(heads):
            end = next((j for j, l2 in heads[k + 1:] if l2 <= lv), len(lines))
            has = any(lines[j].strip() and not _HEAD6.match(lines[j]) for j in range(i + 1, end))
            if not has:
                empty.append(i)
        if not empty:
            break
        for i in empty:
            lines[i] = ""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def plain(s: str, host: str = "") -> str:
    """給模型看的版本：站內、相對連結只留文字，外站連結保留；拿掉註腳號、表格格線與空欄。"""
    def link(m):
        t, u = m.group(1), m.group(2)
        h = urlparse(u).hostname or ""
        if h in _MIRROR:
            return ""
        if u.startswith("http") and h and host and h != host and not h.endswith("." + host) and not host.endswith("." + h):
            return f"[{t}]({u})"
        if u.startswith("http") and "#" not in u and _width(t) >= 30:
            return f"[{t}]({u})"   # 站內的長標題連結（新聞、文章列表）留網址，才點得進去
        return t
    s = _LINK.sub(link, _CITE.sub("", s))
    # 頭像卡的徽章數字（畫面數字＋給螢幕閱讀器的數字黏在一起）：28.1k2222 gold badges → 28.1k 22 gold badges
    s = re.sub(r"(\d+)\1 (gold|silver|bronze) badges", r" \1 \2 badges", s)
    out = []
    fence = False
    for ln in s.split("\n"):
        if _is_fence(ln):
            fence = not fence
        if not fence and "{{" in ln:
            ln = _TPL.sub("", ln)
            if not _meaningful(ln):
                continue
        if ln.lstrip().startswith("|"):
            if _TABSEP.match(ln):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip().strip("|"))]
            cells = [c.replace("\\|", "|") for c in cells if c]
            if not cells:
                continue
            ln = " | ".join(cells)
        if _EDIT.match(ln):
            continue
        if ln.endswith("\\") and not ln.endswith("\\\\"):
            ln = ln[:-1]   # Markdown 強制換行的反斜線
        if not fence and ln.strip() and not _meaningful(ln) and not _is_fence(ln):
            continue   # 連結拿掉後只剩項目符號、分隔線
        if not fence:
            ln = re.sub(r"(\S[^\n]{6,60}?)\1", r"\1", ln)   # 畫面字＋提示字重複一次（on Oct 13, 2015on Oct 13, 2015）
        out.append(re.sub(r"[ \t]{2,}", " ", ln).strip() if not ln.startswith("    ") else ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def _title(raw: str) -> str:
    t = plain(_LINK.sub(lambda m: m.group(1), raw))
    t = re.sub(r"\s*\[\s*(编辑|編輯|edit|编辑源代码)\s*\]\s*$", "", t, flags=re.I)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    return t.replace("\u200b", "").replace("\\", "").strip(" ¶")


def outline(text: str) -> list[tuple[int, str, int]]:
    """頁面上最上面三層標題（通常是 H1–H3；只有 H4 以下的頁面就往下取）：(層級, 標題, 行號)，行號對應存檔。"""
    res, fence, seen = [], False, set()
    for n, s in enumerate(text.split("\n"), 1):
        if _is_fence(s):
            fence = not fence
            continue
        if not fence:
            m = _HEAD.match(s)
            if m:
                t = _title(m.group(2))
                if t and not _COUNT.match(t) and t not in seen:
                    seen.add(t)
                    res.append((len(m.group(1)), t, n))
    top = sorted({lv for lv, _t, _n in res})[:3]
    return [h for h in res if h[0] in top]


def outline_text(heads: list[tuple[int, str, int]], budget: int) -> str:
    def render(hs):
        top = min(h[0] for h in hs)
        return "\n".join(f"{'  ' * (lv - top)}{t[:80]}（{n}）" for lv, t, n in hs)
    if not heads:
        return ""
    lvs = sorted({h[0] for h in heads})
    for top in reversed(lvs):
        hs = [h for h in heads if h[0] <= top]
        if not hs:
            continue
        s = render(hs)
        if len(s) <= budget:
            return s + (f"\n（另有 {len(heads) - len(hs)} 個較小的標題）" if len(hs) < len(heads) else "")
    lv0 = min(h[0] for h in heads)
    keep, used = [], 0
    for h in [h for h in heads if h[0] == lv0]:
        line = render([h])
        if used + len(line) > budget:
            break
        keep.append(h)
        used += len(line) + 1
    return render(keep) + f"\n（另有 {len(heads) - len(keep)} 個標題）"


def _blocks(text: str, host: str = "") -> list[dict]:
    """分段（程式碼區塊算一段；太長的段按句子或「|」切成約 500 字），記行號範圍、所屬標題、是否像導覽。
    text 是給模型看的 plain() 版本。"""
    lines = text.split("\n")
    out, path = [], {}
    for idx in _split_blocks(lines):
        a, b = idx[0] + 1, idx[-1] + 1
        body = "\n".join(lines[i] for i in idx)
        m = _HEAD.match(body) if len(idx) == 1 else None
        if m:
            lv = len(m.group(1))
            for k in [k for k in path if k >= lv]:
                del path[k]
            path[lv] = _title(m.group(2))
        sec = " > ".join(path[k] for k in sorted(path))
        vis = len(_JUNK.sub("", _LINK.sub(r"\1", body))) or 1
        ltxt = sum(len(_JUNK.sub("", t)) for t in _link_texts(body))
        navy = len(_link_texts(body)) >= 3 and ltxt / vis > 0.6
        rows = [x for x in body.split("\n") if x.strip()]
        if len(rows) >= 8 and sum(len(_JUNK.sub("", x)) for x in rows) / len(rows) < 18:
            navy = True   # 一長串短行（語言清單、標籤雲）
        table = body.lstrip().startswith("|")
        body = plain(body, host)
        if not body.strip():
            continue
        pieces = [body]
        if not m and not _is_fence(body) and len(body) > 800:
            pieces, cur = [], ""
            for part in (body.split("\n") if "\n" in body else [body]):
                for seg in (_SENT.split(part) if len(part) > 500 else [part]):
                    if cur and len(cur) + len(seg) > 500:
                        pieces.append(cur)
                        cur = ""
                    if seg.strip():
                        cur = cur + ("\n" if cur and seg is part else " " if cur else "") + seg.strip()
            if cur:
                pieces.append(cur)
        for p in pieces:
            out.append({"a": a, "b": b, "text": p, "sec": sec, "head": bool(m), "lv": len(m.group(1)) if m else 9,
                        "nav": navy, "table": table})
    return out


def find(text: str, keys: list[str], budget: int, host: str = "", each: int = 900,
         skip: set | None = None) -> tuple[str, int, int]:
    """關鍵字命中的段落加前後文。回傳（內容, 命中段數, 列出處數）。

    排序：命中小標題最優先，再看命中幾種關鍵字、次數；像導覽的連結清單、符號很密的資料排最後。
    每處最多 each 字，讓預算裡能放好幾處，不被一個大表格吃光。
    """
    keys = [fold(k) for k in keys if k.strip()]
    bl = _blocks(text, host)
    skip = skip or set()
    h1 = [b for b in bl if b["head"] and b["text"].startswith("# ")]
    strip = (h1[0]["sec"] + " > ") if len(h1) == 1 else ""
    scored = []
    for i, b in enumerate(bl):
        if b["text"] in skip:
            continue
        low = fold(b["text"])
        hit = [k for k in keys if k in low]
        if not hit:
            continue
        score = len(hit) * 10 + min(sum(low.count(k) for k in hit), 5)
        if any(low.lstrip("#*_`>- ").startswith(k) for k in hit):
            score += 8   # 以關鍵字開頭的段落，多半是那個參數、函式的定義
        if b["head"]:
            score += 40
        if _is_fence(b["text"]):
            score -= 5   # 同分時說明文字排在範例程式前面
        if b["nav"]:
            score -= 25
        if len(low) > 400 and len(_SYM.findall(low)) / len(low) > 0.12:
            score -= 30
        scored.append((score, i))
    if not scored:
        return "", 0, 0
    scored.sort(key=lambda x: (-x[0], x[1]))
    real = sum(1 for _s, i in scored if not bl[i]["nav"])
    taken, out, used = set(), [], 0
    for _score, i in scored:
        if i in taken:
            continue
        b = bl[i]
        if b["nav"] and (real >= 2 or any(bl[k]["nav"] for k in taken)):
            continue   # 導覽清單：有正文命中就不列，最多列一處
        idx = [i]
        if b["head"]:
            # 命中標題：給這一節（含底下的子標題），到同級或更高的下一個標題為止
            j = i + 1
            while j < len(bl) and not (bl[j]["head"] and bl[j]["lv"] <= b["lv"]) \
                    and sum(len(bl[k]["text"]) for k in idx) < each:
                idx.append(j)
                j += 1
        else:
            if i > 0 and not bl[i - 1]["head"] and not bl[i - 1]["nav"] and len(bl[i - 1]["text"]) < each // 3:
                idx.insert(0, i - 1)
            j = i + 1
            while j < len(bl) and not bl[j]["head"] and sum(len(bl[k]["text"]) for k in idx) < each // 2:
                idx.append(j)
                j += 1
        idx = [k for k in idx if k not in taken and bl[k]["text"] not in skip]
        if not idx or (b["head"] and len(idx) == 1):
            continue
        piece = "\n\n".join(bl[k]["text"] for k in idx)
        cap = min(300 if b["nav"] else each, budget - used)
        if cap < 200:
            break
        if len(piece) > cap:
            low = fold(piece)
            pos = min([p for p in (low.find(k) for k in keys) if p >= 0] or [0])
            st = max(0, min(pos - cap // 4, len(piece) - cap))
            piece = ("…" if st else "") + piece[st:st + cap] + ("…" if st + cap < len(piece) else "")
        sec = (b["sec"] + " > ").removeprefix(strip).removesuffix(" > ") if strip else b["sec"]
        sec = sec or "開頭"
        blk = f"§ {sec}（{bl[idx[0]]['a']}–{bl[idx[-1]]['b']} 行）\n{piece}"
        out.append(blk)
        used += len(blk)
        taken.update(idx)
    return "\n\n".join(out), len(scored), len(out)


def lead(text: str, n: int, host: str = "", picked: set | None = None) -> str:
    """開頭：第一個 H1 之後（沒有 H1 就從頭）的內文段落，跳過標題、導覽清單、表格；不夠再補。"""
    bl = _blocks(text, host)
    lines = text.split("\n")
    h1 = next((i + 1 for i, s in enumerate(lines) if s.startswith("# ")), 0)
    nh = sum(b["head"] for b in bl)
    listy = nh >= 6 and sum(len(b["text"]) for b in bl if not b["head"]) / nh < 200   # 文章列表：標題就是內容
    body = [b for b in bl if b["a"] > h1 and (listy or not b["head"])] or [b for b in bl if listy or not b["head"]]
    prose = [b for b in body if not b["nav"] and not b["table"]]
    pick, used = [], 0
    # 很短又沒有句末標點的（「跳到內容」「某某百科，自由的百科全書」）多半是網站外框，先不取
    sent = prose if listy else [b for b in prose if len(b["text"]) >= 25 or
                                b["text"].rstrip("*_ ").endswith(("。", "！", "？", ".", "!", "?", "：", ":"))]
    for group in (sent, prose, body):
        for b in group:
            if b in pick:
                continue
            t = b["text"]
            pick.append(b)
            used += len(t) + 2
            if used >= n:
                break
        if used >= n * 0.6:
            break
    pick.sort(key=lambda b: bl.index(b))
    if picked is not None:
        picked.update(b["text"] for b in pick)
    s = "\n\n".join(b["text"] for b in pick)
    if len(s) <= n:
        return s
    cut = s.rfind("\n", 0, n)
    return s[:cut if cut > n // 2 else n] + "…"


def preview(d: dict, keys: list[str], full: bool, cfg: dict) -> str:
    """d：url、text（存檔內容）、title、desc、path。cfg：preview、hit、lead、outline 四個字數。"""
    text, url = d["text"], d["url"]
    host = urlparse(url).hostname or ""
    heads = outline(text)
    title = (d.get("title") or "").strip() or next((t for lv, t, _n in heads if lv == 1), "") or "（無標題）"
    out = [f"## {title}", url]
    if (d.get("desc") or "").strip():
        out.append(f"摘要：{d['desc'].strip()[:200]}")
    shown = plain(text, host)
    out.append(f"清理後 {len(shown)} 字；存檔（含連結網址）：{d['path']}")
    if full or len(shown) <= cfg["preview"]:
        return "\n".join(out) + "\n\n" + (shown or "（沒有內容）")
    toc = outline_text(heads, cfg["outline"])
    if toc:
        out += ["", "### 目錄（括號是存檔行號）", toc]
    shown_lead: set = set()
    ld = lead(text, cfg["lead"] if keys else cfg["lead"] + cfg["hit"], host, shown_lead)
    body, nhit, nshow = find(text, keys, cfg["hit"], host, skip=shown_lead) if keys else ("", 0, 0)
    if keys and not body:
        ld = lead(text, cfg["lead"] + cfg["hit"], host)
    if ld:
        out += ["", "### 開頭", ld]
    if keys:
        kw = "、".join(keys)
        out += ["", f"### 「{kw}」{nhit} 段命中，列出 {nshow} 處" if body else f"### 「{kw}」沒有找到", body] if body \
            else ["", f"### 「{kw}」沒有找到"]
    out += ["", "（預覽。整篇：同網址 full=true；某一節：read_file 存檔 offset=行號；換字：同網址改 find，讀存檔不重抓）"]
    return "\n".join(x for x in out if x is not None)
