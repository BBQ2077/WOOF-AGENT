"""網頁內容整理：清雜訊、H1–H3 目錄、關鍵字段落、預覽（純文字比對，不叫模型）。"""
from __future__ import annotations

import re

_IMG_B64 = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HEAD = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_JUNK = re.compile(r"[\s*\-+|•·>#:：,，.。/\\()（）\[\]_`'\"!?！？\u200b]+")


_SYM = re.compile(r'[{}\[\]":,\\<>=;]')


def _machine(s: str) -> bool:
    """壓縮過的 JSON／JS 這類機器資料（很長、符號很密）。"""
    return len(s) > 1500 and len(_SYM.findall(s)) / len(s) > 0.12


def _nav_line(s: str) -> bool:
    """幾乎整行都是連結（選單、導覽、頁尾）。"""
    links = _LINK.findall(s)
    if not links:
        return False
    if not _JUNK.sub("", _LINK.sub("", s)) and not any(_JUNK.sub("", t) for t in links):
        return True   # 只有空錨點連結（例如 [​](#xxx)）
    rest = _JUNK.sub("", _LINK.sub("", s))
    return len(rest) <= 3


def clean(md: str) -> str:
    """去掉 base64 圖、純圖片行、連續的選單連結行；多個空行合成一個。程式碼區塊原樣保留。"""
    md = _IMG_B64.sub("", (md or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines, out, fence = md.split("\n"), [], False
    nav = [False] * len(lines)
    for i, s in enumerate(lines):
        if s.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            nav[i] = _nav_line(s)
    fence = False
    for i, s in enumerate(lines):
        if s.lstrip().startswith("```"):
            fence = not fence
            out.append(s.rstrip())
            continue
        if fence:
            out.append(s.rstrip())
            continue
        if _machine(s):
            out.append(f"[省略一段 {len(s)} 字的程式資料]")
            continue
        if nav[i]:
            # 單獨一行連結（例如內文裡的「參考：某某」）保留；連續好幾行或一行好幾個連結才當選單
            run = 1
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and nav[j]:
                    run += 1
            if run > 1 or len(_LINK.findall(s)) >= 3 or not any(_JUNK.sub("", t) for t in _LINK.findall(s)):
                continue
        s = _IMG.sub(lambda m: f"[圖：{m.group(1)}]" if m.group(1).strip() else "", s).rstrip()
        if not s.strip() and s != "":
            s = ""
        out.append(s)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return text


def outline(text: str) -> list[tuple[int, str, int]]:
    """H1–H3：(層級, 標題, 行號)，行號從 1 起，對應存檔。"""
    res, fence = [], False
    for n, s in enumerate(text.split("\n"), 1):
        if s.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            m = _HEAD.match(s)
            if m:
                t = _LINK.sub(r"\1", m.group(2)).replace("\u200b", "").strip()
                if t:
                    res.append((len(m.group(1)), t, n))
    return res


def outline_text(heads: list[tuple[int, str, int]], budget: int) -> str:
    def render(hs):
        return "\n".join(f"{'  ' * (lv - 1)}{t}（第 {n} 行）" for lv, t, n in hs)
    if not heads:
        return ""
    for top in (3, 2, 1):
        hs = [h for h in heads if h[0] <= top]
        s = render(hs)
        if len(s) <= budget:
            return s + (f"\n（另有 {len(heads) - len(hs)} 個較小的標題沒列）" if len(hs) < len(heads) else "")
    keep, used = [], 0
    for h in [h for h in heads if h[0] == 1] or heads:
        line = render([h])
        if used + len(line) > budget:
            break
        keep.append(h)
        used += len(line) + 1
    return render(keep) + f"\n（另有 {len(heads) - len(keep)} 個標題沒列）"


def _blocks(text: str) -> list[dict]:
    """依空行分段（程式碼區塊算一段），每段記行號範圍和所屬標題。"""
    lines = text.split("\n")
    blocks, cur, start, fence, path = [], [], 0, False, {}

    def flush(end):
        if cur:
            blocks.append({"a": start + 1, "b": end, "text": "\n".join(cur),
                           "sec": " > ".join(path[k] for k in sorted(path)),
                           "head": len(cur) == 1 and bool(_HEAD.match(cur[0]))})

    for i, s in enumerate(lines):
        if s.lstrip().startswith("```"):
            if not fence and cur:
                flush(i)
                cur, start = [], i
            if not cur:
                start = i
            cur.append(s)
            fence = not fence
            if not fence:
                flush(i + 1)
                cur = []
            continue
        if fence:
            cur.append(s)
            continue
        m = _HEAD.match(s)
        if m:
            flush(i)
            lv = len(m.group(1))
            for k in [k for k in path if k >= lv]:
                del path[k]
            path[lv] = _LINK.sub(r"\1", m.group(2)).replace("\u200b", "").strip()
            cur, start = [s], i
            flush(i + 1)
            cur = []
            continue
        if not s.strip():
            flush(i)
            cur = []
            continue
        if not cur:
            start = i
        cur.append(s)
    flush(len(lines))
    return blocks


def _clip_around(text: str, keys: list[str], n: int) -> str:
    if len(text) <= n:
        return text
    low = text.lower()
    pos = min([p for p in (low.find(k) for k in keys) if p >= 0] or [0])
    a = max(0, min(pos - n // 3, len(text) - n))
    return ("…" if a else "") + text[a:a + n] + ("…" if a + n < len(text) else "")


def find(text: str, keys: list[str], budget: int) -> tuple[str, int, int]:
    """關鍵字命中的段落加前後各一段。回傳（內容, 命中段數, 列出段數）。"""
    keys = [k.lower() for k in keys if k.strip()]
    bl = _blocks(text)
    scored = []
    for i, b in enumerate(bl):
        low = b["text"].lower()
        hit = [k for k in keys if k in low]
        if hit:
            score = len(hit) * 10 + min(sum(low.count(k) for k in hit), 9) + (15 if b["head"] else 0)
            if len(low) > 400 and len(_SYM.findall(low)) / len(low) > 0.12:
                score -= 30   # 符號很密的段落（資料、設定）排在一般內文後面
            scored.append((score, i))
    if not scored:
        return "", 0, 0
    scored.sort(key=lambda x: (-x[0], x[1]))
    taken, out, used = set(), [], 0
    for _score, i in scored:
        if i in taken:
            continue
        lo = i if bl[i]["head"] else max(0, i - 1)   # 命中小標題就從標題開始
        hi = min(len(bl) - 1, i + (2 if bl[i]["head"] else 1))
        # 段落切得很碎時（API 文件常見），往後延伸到約 600 字或下一個標題
        while hi + 1 < len(bl) and not bl[hi + 1]["head"] and sum(len(bl[j]["text"]) for j in range(i, hi + 1)) < 600:
            hi += 1
        idx = [j for j in range(lo, hi + 1) if j not in taken]
        if not idx:
            continue
        piece = "\n\n".join(_clip_around(bl[j]["text"], keys, 1500) for j in idx)
        sec = bl[i]["sec"] or "（開頭）"
        block = f"§ {sec}（第 {bl[idx[0]]['a']}–{bl[idx[-1]]['b']} 行）\n{piece}"
        if used + len(block) > budget:
            room = budget - used
            if out and room < 300:
                break
            block = block[:max(room, 300)] + "…"
        out.append(block)
        used += len(block)
        taken.update(idx)
        if used >= budget:
            break
    return "\n\n".join(out), len(scored), len(out)


def lead(text: str, n: int) -> str:
    """開頭：從第一個 H1 之後的內文起（沒有 H1 就從頭），跳過標題行，切在換行處。"""
    lines = text.split("\n")
    start = next((i + 1 for i, s in enumerate(lines) if s.startswith("# ")), 0)
    body = "\n".join(s for s in lines[start:] if not _HEAD.match(s)).strip()
    if len(body) <= n:
        return body
    cut = body.rfind("\n", 0, n)
    return body[:cut if cut > n // 2 else n] + "…"
