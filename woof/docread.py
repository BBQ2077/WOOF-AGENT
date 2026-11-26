"""docx／pptx／pdf 轉文字：給 read_file 用，模型不必開 terminal。
docx、pptx 只用標準函式庫；pdf 用 pypdf（圖片要 Pillow）。

照文件順序列出段落（含自動編號）、表格、文字方塊、公式文字、圖表標題與數據；
圖片解出來存檔，在原位置標「[圖N：路徑]」，要看用 vision_analyze。
轉出的文字和圖放 data/spill/docs/<編號>/，放 7 天由 woof.retention 刪；原檔沒變就直接用上次轉好的。
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
VML_IMG = "{urn:schemas-microsoft-com:vml}imagedata"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"

VIEWABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
KINDS = (".docx", ".pptx", ".pdf")
VERSION = "2"   # 轉換規則改了就加一，舊的轉檔結果不再沿用


def _dir() -> Path:
    from woof.config import DATA

    return DATA / "spill" / "docs"


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """這個部件的關聯：rId → 壓縮檔裡的完整路徑。"""
    d, name = posixpath.split(part)
    rp = posixpath.join(d, "_rels", name + ".rels")
    try:
        root = ET.fromstring(z.read(rp))
    except (KeyError, ET.ParseError):
        return {}
    out = {}
    for r in root.iter(PKG_REL):
        if r.get("TargetMode") == "External":
            continue
        t = r.get("Target") or ""
        out[r.get("Id")] = posixpath.normpath(t.lstrip("/") if t.startswith("/") else posixpath.join(d, t))
    return out


# ---------------------------------------------------------------- 自動編號
def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    s = ""
    for v, c in vals:
        while n >= v:
            s, n = s + c, n - v
    return s


def _letters(n: int) -> str:
    return chr(65 + (n - 1) % 26) * ((n - 1) // 26 + 1)


_CN = "〇一二三四五六七八九"


def _cn(n: int) -> str:
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + (_CN[n % 10] if n % 10 else "")
    if n < 100:
        return _CN[n // 10] + "十" + (_CN[n % 10] if n % 10 else "")
    return str(n)


def _fmt(n: int, kind: str) -> str:
    if kind in ("upperLetter",):
        return _letters(n)
    if kind == "lowerLetter":
        return _letters(n).lower()
    if kind == "upperRoman":
        return _roman(n)
    if kind == "lowerRoman":
        return _roman(n).lower()
    if kind == "decimalFullWidth" or kind == "decimalFullWidth2":
        return "".join(chr(ord(c) + 0xFEE0) for c in str(n))
    if kind in ("ideographTraditional",):
        return "甲乙丙丁戊己庚辛壬癸"[(n - 1) % 10]
    if kind in ("ideographZodiac",):
        return "子丑寅卯辰巳午未申酉戌亥"[(n - 1) % 12]
    if kind in ("taiwaneseCountingThousand", "taiwaneseCounting", "chineseCounting", "chineseCountingThousand",
                "ideographDigital", "japaneseCounting"):
        return _cn(n)
    if kind == "decimalEnclosedCircle" and 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    if kind == "decimalEnclosedParen" and 1 <= n <= 20:
        return chr(0x2474 + n - 1)
    if kind == "decimalZero":
        return f"{n:02d}"
    if kind == "none":
        return ""
    return str(n)


class _Numbering:
    def __init__(self, z: zipfile.ZipFile):
        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}   # abstractNumId → ilvl → (格式, 文字, 起始)
        self.num2abs: dict[str, str] = {}
        self.style_num: dict[str, tuple[str, int]] = {}
        self.count: dict[str, dict[int, int]] = {}
        try:
            root = ET.fromstring(z.read("word/numbering.xml"))
            for an in root.findall(W + "abstractNum"):
                lv = {}
                for l in an.findall(W + "lvl"):
                    fmt = l.find(W + "numFmt")
                    txt = l.find(W + "lvlText")
                    st = l.find(W + "start")
                    lv[int(l.get(W + "ilvl", "0"))] = (fmt.get(W + "val") if fmt is not None else "decimal",
                                                       txt.get(W + "val") if txt is not None else "",
                                                       int(st.get(W + "val")) if st is not None else 1)
                self.levels[an.get(W + "abstractNumId")] = lv
            for n in root.findall(W + "num"):
                a = n.find(W + "abstractNumId")
                if a is not None:
                    self.num2abs[n.get(W + "numId")] = a.get(W + "val")
        except (KeyError, ET.ParseError, ValueError):
            pass
        try:
            root = ET.fromstring(z.read("word/styles.xml"))
            for s in root.findall(W + "style"):
                np_ = s.find(f"{W}pPr/{W}numPr")
                if np_ is not None:
                    nid = np_.find(W + "numId")
                    il = np_.find(W + "ilvl")
                    if nid is not None:
                        self.style_num[s.get(W + "styleId")] = (nid.get(W + "val"),
                                                                int(il.get(W + "val")) if il is not None else 0)
        except (KeyError, ET.ParseError, ValueError):
            pass

    def label(self, p) -> str:
        ppr = p.find(W + "pPr")
        if ppr is None:
            return ""
        nid, ilvl = None, 0
        np_ = ppr.find(W + "numPr")
        if np_ is not None:
            e = np_.find(W + "numId")
            nid = e.get(W + "val") if e is not None else None
            e = np_.find(W + "ilvl")
            ilvl = int(e.get(W + "val")) if e is not None else 0
        else:
            ps = ppr.find(W + "pStyle")
            if ps is not None and ps.get(W + "val") in self.style_num:
                nid, ilvl = self.style_num[ps.get(W + "val")]
        if not nid or nid == "0" or nid not in self.num2abs:
            return ""
        aid = self.num2abs[nid]
        lv = self.levels.get(aid, {})
        if ilvl not in lv:
            return ""
        cnt = self.count.setdefault(aid, {})
        cnt[ilvl] = cnt.get(ilvl, lv[ilvl][2] - 1) + 1
        for k in [k for k in cnt if k > ilvl]:
            del cnt[k]
        fmt, text, _ = lv[ilvl]
        if fmt == "bullet":
            return "• "
        out = re.sub(r"%(\d)", lambda m: _fmt(cnt.get(int(m.group(1)) - 1, lv.get(int(m.group(1)) - 1, ("", "", 1))[2]),
                                                lv.get(int(m.group(1)) - 1, ("decimal", "", 1))[0]), text)
        return (out + " ") if out.strip() else ""


# ---------------------------------------------------------------- 共用：圖、圖表
class _Conv:
    def __init__(self, z: zipfile.ZipFile, media: Path):
        self.z, self.media = z, media
        self.n_img = self.n_chart = 0
        self.saved: dict[str, str] = {}
        self.unviewable = 0

    def image(self, rels: dict, rid: str | None) -> str:
        target = rels.get(rid or "")
        if not target:
            return ""
        if target not in self.saved:
            try:
                data = self.z.read(target)
            except KeyError:
                return ""
            self.n_img += 1
            self.media.mkdir(parents=True, exist_ok=True)
            p = self.media / f"{self.n_img:03d}_{posixpath.basename(target)}"
            p.write_bytes(data)
            self.saved[target] = f"[圖{self.n_img}：{p}]"
            if p.suffix.lower() not in VIEWABLE:
                self.unviewable += 1
                self.saved[target] = f"[圖{self.n_img}：{p.suffix[1:]} 格式，vision_analyze 看不了；{p}]"
        return self.saved[target]

    def chart(self, rels: dict, rid: str | None) -> str:
        target = rels.get(rid or "")
        if not target:
            return ""
        try:
            root = ET.fromstring(self.z.read(target))
        except (KeyError, ET.ParseError):
            return ""
        self.n_chart += 1
        title = "".join(t.text or "" for t in root.iterfind(f".//{C}title//{A}t")).strip()
        kind = ""
        pa = root.find(f".//{C}plotArea")
        if pa is not None:
            for ch in pa:
                tag = ch.tag.replace(C, "")
                if tag.endswith("Chart"):
                    kind = tag
                    break
        lines = [f"[圖表{self.n_chart}" + (f"（{kind}）" if kind else "") + (f"：{title}" if title else "") + "]"]
        for ser in root.iter(C + "ser"):
            name = "".join(v.text or "" for v in ser.iterfind(f"{C}tx//{C}v")).strip()
            cats = [v.text or "" for v in ser.iterfind(f"{C}cat//{C}pt/{C}v")]
            vals = [v.text or "" for v in ser.iterfind(f"{C}val//{C}pt/{C}v")]
            if not vals:   # 散佈圖、泡泡圖
                cats = [v.text or "" for v in ser.iterfind(f"{C}xVal//{C}pt/{C}v")]
                vals = [v.text or "" for v in ser.iterfind(f"{C}yVal//{C}pt/{C}v")]
            pairs = ", ".join(f"{c}={v}" if c else v for c, v in zip(cats + [""] * len(vals), vals))
            lines.append(f"  系列 {name or '（無名）'}：{pairs}")
        return "\n".join(lines)


# ---------------------------------------------------------------- docx
class _Docx(_Conv):
    def __init__(self, z, media):
        super().__init__(z, media)
        self.num = _Numbering(z)
        self.rels = _rels(z, "word/document.xml")

    def para(self, p) -> list[str]:
        buf: list[str] = [self.num.label(p)]
        extra: list[str] = []

        def walk(e):
            for ch in e:
                t = ch.tag
                if t == W + "t" or t == M + "t":
                    buf.append(ch.text or "")
                elif t == W + "tab":
                    buf.append("\t")
                elif t in (W + "br", W + "cr"):
                    buf.append("\n")
                elif t in (W + "delText", W + "instrText", W + "pPr", W + "rPr"):
                    continue
                elif t == MC + "AlternateContent":   # 新舊兩種寫法擇一，不然圖、文字方塊會重複
                    first = next(iter(ch), None)
                    if first is not None:
                        walk(first)
                elif t == W + "txbxContent":
                    extra.extend(self.block(ch))
                elif t == A + "blip":
                    buf.append(self.image(self.rels, ch.get(R + "embed")))
                elif t == VML_IMG:
                    buf.append(self.image(self.rels, ch.get(R + "id")))
                elif t == C + "chart":
                    buf.append("\n" + self.chart(self.rels, ch.get(R + "id")) + "\n")
                else:
                    walk(ch)

        walk(p)
        text = "".join(buf).strip("\n")
        out = text.split("\n") if text.strip() else []
        if extra:
            out += ["〔文字方塊〕" + extra[0]] + extra[1:]
        return out

    def table(self, tbl) -> list[str]:
        rows = []
        for tr in tbl.findall(W + "tr"):
            cells = []
            for tc in tr.findall(W + "tc"):
                cells.append(" ".join(l.strip() for l in self.block(tc) if l.strip()).replace("|", "｜"))
            rows.append("| " + " | ".join(cells) + " |")
        return rows

    def block(self, el) -> list[str]:
        out: list[str] = []
        for ch in el:
            t = ch.tag
            if t == W + "p":
                out += self.para(ch)
            elif t == W + "tbl":
                out += self.table(ch)
            elif t == W + "sdt":
                c = ch.find(W + "sdtContent")
                if c is not None:
                    out += self.block(c)
            elif t == MC + "AlternateContent":
                first = next(iter(ch), None)
                if first is not None:
                    out += self.block(first)
            elif t in (W + "customXml", W + "ins", W + "smartTag"):
                out += self.block(ch)
        return out

    def run(self) -> list[str]:
        root = ET.fromstring(self.z.read("word/document.xml"))
        body = root.find(W + "body")
        return self.block(body) if body is not None else []


# ---------------------------------------------------------------- pptx
class _Pptx(_Conv):
    def shapes(self, el, rels) -> list[str]:
        out: list[str] = []
        for ch in el:
            t = ch.tag
            if t == A + "p":
                s = "".join((x.text or "") if x.tag == A + "t" else "\n" if x.tag == A + "br" else ""
                            for x in ch.iter() if x.tag in (A + "t", A + "br")).strip()
                if s:
                    out += s.split("\n")
            elif t == A + "tbl":
                for tr in ch.findall(A + "tr"):
                    cells = [" ".join(self.shapes(tc, rels)).replace("|", "｜") for tc in tr.findall(A + "tc")]
                    out.append("| " + " | ".join(cells) + " |")
            elif t == A + "blip":
                out.append(self.image(rels, ch.get(R + "embed")))
            elif t == C + "chart":
                out += self.chart(rels, ch.get(R + "id")).split("\n")
            elif t == MC + "AlternateContent":
                first = next(iter(ch), None)
                if first is not None:
                    out += self.shapes(first, rels)
            else:
                out += self.shapes(ch, rels)
        return out

    def run(self) -> list[str]:
        prels = _rels(self.z, "ppt/presentation.xml")
        order = []
        try:
            root = ET.fromstring(self.z.read("ppt/presentation.xml"))
            for s in root.iterfind(f".//{P}sldId"):
                if prels.get(s.get(R + "id")):
                    order.append(prels[s.get(R + "id")])
        except (KeyError, ET.ParseError):
            pass
        if not order:
            order = sorted((n for n in self.z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out: list[str] = []
        for i, part in enumerate(order, 1):
            try:
                root = ET.fromstring(self.z.read(part))
            except (KeyError, ET.ParseError):
                continue
            out.append(f"--- 投影片 {i} ---")
            out += self.shapes(root, _rels(self.z, part))
        return out


# ---------------------------------------------------------------- pdf
PDF_STREAM_MAX = 50 * 1024 * 1024   # 單頁內容超過這個不抽字：pypdf 文件說 300 MB 的內容要吃到 10 GB 記憶體
PDF_IMG_MIN = 32                    # 長或寬小於這個的圖（圖示、線條）不解出


def _radical_table() -> dict:
    """PDF 字型常把「一」「人」對到長得一樣的康熙部首（U+2F00 起）、部首補充（U+2E80 起），換回一般漢字。
    只換這兩段，不用整篇 NFKC，全形標點和數字才不會被一起改掉。"""
    import unicodedata

    t = {}
    for c in range(0x2E80, 0x2FE0):
        n = unicodedata.normalize("NFKC", chr(c))
        if n != chr(c) and len(n) == 1:
            t[c] = n
    return t


_RADICALS = _radical_table()


class _Pdf:
    def __init__(self, path: Path, media: Path):
        self.path, self.media = path, media
        self.n_img = self.n_chart = self.unviewable = 0
        self.scanned = 0

    def run(self) -> list[str]:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(self.path.read_bytes()))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception as e:
                raise ValueError(f"PDF 有加密，解不開（{e}）") from e
            if not ok:
                raise ValueError("PDF 有密碼保護，要密碼才能讀")
        out: list[str] = []
        for i, page in enumerate(reader.pages, 1):
            out.append(f"--- 第 {i} 頁 ---")
            try:
                c = page.get_contents()
                big = c is not None and len(c.get_data()) > PDF_STREAM_MAX
            except Exception:
                big = False
            text = ""
            if big:
                out.append("（這頁內容太大，沒有抽字）")
            else:
                try:
                    text = (page.extract_text() or "").strip()
                except Exception as e:
                    out.append(f"（這頁抽字失敗：{type(e).__name__}）")
            if text:
                out += text.translate(_RADICALS).splitlines()
            imgs = self.images(page, i)
            if len(text) < 20 and imgs:
                self.scanned += 1
                out.append("（這頁幾乎沒有文字，內容可能在圖裡）")
            out += imgs
        return out

    def images(self, page, pno: int) -> list[str]:
        out = []
        try:
            names = list(page.images.keys())
        except Exception:
            return out
        for name in names:
            try:
                im = page.images[name]
                pil = im.image
                if pil is None or min(pil.size) < PDF_IMG_MIN:
                    continue
                self.media.mkdir(parents=True, exist_ok=True)
                self.n_img += 1
                ext = ".jpg" if (pil.format or "").upper() == "JPEG" else ".png"
                p = self.media / f"{self.n_img:03d}_p{pno}{ext}"
                if ext == ".jpg":
                    p.write_bytes(im.data)
                else:
                    (pil if pil.mode in ("RGB", "RGBA", "L", "LA", "P") else pil.convert("RGB")).save(p)
                out.append(f"[圖{self.n_img}：{p}]")
            except Exception:
                continue
        return out


def convert(path: Path) -> dict:
    """回 {"text", "dir", "images", "charts", "unviewable"}；原檔沒變就用上次轉好的。"""
    path = Path(path)
    st = path.stat()
    key = hashlib.sha1(f"{VERSION}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    d = _dir() / key
    done = d / "text.txt"
    meta = d / "meta.txt"
    if done.is_file() and meta.is_file():
        n, c, u, s = (int(x) for x in meta.read_text(encoding="utf-8").split())
        return {"text": done.read_text(encoding="utf-8"), "dir": d, "images": n, "charts": c, "unviewable": u,
                "scanned": s}
    d.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext == ".pdf":
        conv = _Pdf(path, d)
        lines = conv.run()
    else:
        import io

        # 整個讀進記憶體再拆：不留著原檔的檔案控制代碼，使用者才刪得掉、改得了
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
            conv = (_Docx if ext == ".docx" else _Pptx)(z, d)
            lines = conv.run()
    text = "\n".join(l.rstrip() for l in lines)
    scanned = getattr(conv, "scanned", 0)
    done.write_text(text, encoding="utf-8")
    meta.write_text(f"{conv.n_img} {conv.n_chart} {conv.unviewable} {scanned}", encoding="utf-8")
    return {"text": text, "dir": d, "images": conv.n_img, "charts": conv.n_chart, "unviewable": conv.unviewable,
            "scanned": scanned}
