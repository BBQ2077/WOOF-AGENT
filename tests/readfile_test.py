"""read_file 分段（不被工具輸出上限從中間截掉）、docx／pptx 轉文字、附件用 HTTP 上傳。"""
import asyncio
import os
import re
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path

TMP = os.environ.get("WOOF_DATA") or tempfile.mkdtemp(prefix="woof_test_")
os.environ["WOOF_DATA"] = TMP

import plugins_files_probe as probe  # noqa: E402
from woof import docread, retention  # noqa: E402
from woof.agent import loop  # noqa: E402

TOOLS = {}


class _W:
    def add_tool(self, name, desc, schema, fn, **k):
        TOOLS[name] = fn

    def __getattr__(self, n):
        return lambda *a, **k: None


probe._mod.register(_W())


def read(path, **kw):
    return asyncio.run(TOOLS["read_file"]({"path": str(path), **kw}, types.SimpleNamespace(cwd=".")))


def read_all(path):
    """照結尾給的 offset 一路讀完，回 (次數, 讀到的行號)。"""
    off, seen, n = 1, [], 0
    while True:
        out = read(path, offset=off)["output"]
        n += 1
        assert len(out) <= loop.MAX_TOOL_CHARS, len(out)
        seen += [int(x) for x in re.findall(r"^(\d+)\|", out, re.M)]
        m = re.search(r"下一段 offset=(\d+)", out)
        if not m:
            return n, seen, out
        off = int(m.group(1))


class Paging(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_762_lines_41kb_not_clipped(self):
        p = self.d / "a.md"
        p.write_text("\n".join(f"第{i}行 " + "字" * 50 for i in range(1, 763)), encoding="utf-8")
        self.assertGreater(p.stat().st_size, 40_000)
        out = read(p)["output"]
        self.assertEqual(loop._clip(out, "t"), out)          # 不會被從中間截掉
        self.assertIn("下一段 offset=", out)
        n, seen, _ = read_all(p)
        self.assertEqual(seen, list(range(1, 763)))          # 一行都沒少
        self.assertGreater(n, 1)

    def test_short_file_whole(self):
        p = self.d / "b.txt"
        p.write_text("a\nb\nc", encoding="utf-8")
        self.assertEqual(read(p)["output"], "1|a\n2|b\n3|c")

    def test_limit_still_counts_lines(self):
        p = self.d / "c.txt"
        p.write_text("\n".join(map(str, range(100))), encoding="utf-8")
        out = read(p, offset=10, limit=5)["output"]
        self.assertEqual(re.findall(r"^(\d+)\|", out, re.M), ["10", "11", "12", "13", "14"])
        self.assertIn("下一段 offset=15", out)

    def test_one_huge_line(self):
        p = self.d / "d.txt"
        p.write_text("x" * 50_000 + "\nok", encoding="utf-8")
        out = read(p)["output"]
        self.assertLessEqual(len(out), loop.MAX_TOOL_CHARS)
        self.assertIn("這一行共 50,000 字", out)
        self.assertIn("下一段 offset=2", out)

    def test_offset_past_end(self):
        p = self.d / "e.txt"
        p.write_text("a\nb", encoding="utf-8")
        self.assertIn("超過檔尾", read(p, offset=9)["output"])

    def test_binary_hints(self):
        for ext, want in ((".pdf", "轉文字失敗"), (".doc", "舊版 Office")):
            p = self.d / f"f{ext}"
            p.write_bytes(b"\x00\x01" * 100)
            r = read(p)
            self.assertTrue(r["is_error"])
            self.assertIn(want, r["output"])


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63f8cfc0f01f0005000201a5f0a6"
                    "e90000000049454e44ae426082")
NSW = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
       'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
       'xmlns:v="urn:schemas-microsoft-com:vml"')
REL = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'


def _p(text, num=None):
    ppr = f'<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr></w:pPr>' if num else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def make_docx(path):
    body = (_p("標題")
            + _p("第一題", 1) + _p("第二題", 1)
            + '<w:tbl><w:tr><w:tc>' + _p("國家") + '</w:tc><w:tc>' + _p("人口") + '</w:tc></w:tr>'
            + '<w:tr><w:tc>' + _p("甲") + '</w:tc><w:tc>' + _p("100") + '</w:tc></w:tr></w:tbl>'
            # 圖：新舊兩種寫法（Choice／Fallback）只能出現一次；文字方塊也一樣
            + '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>'
              '<a:blip r:embed="rIdImg"/><w:txbxContent>' + _p("方塊裡的字") + '</w:txbxContent>'
              '</w:drawing></mc:Choice><mc:Fallback><w:pict><v:imagedata r:id="rIdOld"/>'
              '<w:txbxContent>' + _p("方塊裡的字") + '</w:txbxContent></w:pict></mc:Fallback>'
              '</mc:AlternateContent></w:r></w:p>'
            + '<w:p><w:r><w:drawing><c:chart r:id="rIdChart"/></w:drawing></w:r></w:p>'
            + '<w:p><m:oMath><m:r><m:t>x=2</m:t></m:r></m:oMath></w:p>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NSW}><w:body>{body}</w:body></w:document>")
        z.writestr("word/_rels/document.xml.rels", REL.format(
            '<Relationship Id="rIdImg" Target="media/image1.png"/>'
            '<Relationship Id="rIdOld" Target="media/image2.png"/>'
            '<Relationship Id="rIdChart" Target="charts/chart1.xml"/>'))
        z.writestr("word/media/image1.png", PNG)
        z.writestr("word/media/image2.png", PNG)
        z.writestr("word/numbering.xml",
                   f'<w:numbering {NSW}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0">'
                   '<w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
                   '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
        z.writestr("word/charts/chart1.xml",
                   f'<c:chartSpace {NSW}><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>出口額</a:t></a:r></a:p>'
                   '</c:rich></c:tx></c:title><c:plotArea><c:barChart><c:ser><c:tx><c:strRef><c:strCache><c:pt idx="0">'
                   '<c:v>2024</c:v></c:pt></c:strCache></c:strRef></c:tx><c:cat><c:strRef><c:strCache>'
                   '<c:pt idx="0"><c:v>美國</c:v></c:pt><c:pt idx="1"><c:v>日本</c:v></c:pt></c:strCache></c:strRef></c:cat>'
                   '<c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>30</c:v></c:pt><c:pt idx="1"><c:v>12</c:v></c:pt>'
                   '</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>')


def make_pptx(path):
    P = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
         'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    with zipfile.ZipFile(path, "w") as z:
        # 投影片順序照 presentation.xml，不照檔名
        z.writestr("ppt/presentation.xml", f'<p:presentation {P}><p:sldIdLst>'
                   '<p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", REL.format(
            '<Relationship Id="rId1" Target="slides/slide1.xml"/><Relationship Id="rId2" Target="slides/slide2.xml"/>'))
        z.writestr("ppt/slides/slide2.xml", f'<p:sld {P}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>開場</a:t>'
                   '</a:r></a:p></p:txBody></p:sp><p:pic><p:blipFill><a:blip r:embed="rId9"/></p:blipFill></p:pic>'
                   '</p:spTree></p:cSld></p:sld>')
        z.writestr("ppt/slides/_rels/slide2.xml.rels", REL.format('<Relationship Id="rId9" Target="../media/image1.png"/>'))
        z.writestr("ppt/media/image1.png", PNG)
        z.writestr("ppt/slides/slide1.xml", f'<p:sld {P}><p:cSld><p:spTree><p:graphicFrame><a:graphic><a:graphicData>'
                   '<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>A</a:t></a:r></a:p></a:txBody></a:tc>'
                   '<a:tc><a:txBody><a:p><a:r><a:t>B</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>'
                   '</a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>')


class Office(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_docx(self):
        p = self.d / "試卷.docx"
        make_docx(p)
        r = read(p)
        self.assertFalse(r.get("is_error"), r["output"])
        out = r["output"]
        self.assertIn("docx 轉成的文字", out)
        self.assertIn("1. 第一題", out)                     # 自動編號
        self.assertIn("2. 第二題", out)
        self.assertIn("| 國家 | 人口 |", out)              # 表格
        self.assertIn("| 甲 | 100 |", out)
        self.assertEqual(out.count("方塊裡的字"), 1)         # Choice／Fallback 不重複
        self.assertEqual(len(re.findall(r"\[圖\d+：", out)), 1)
        img = re.search(r"\[圖1：(.+?)\]", out).group(1)
        self.assertEqual(Path(img).read_bytes(), PNG)        # 圖解出成檔案
        self.assertIn("出口額", out)                          # 圖表標題與數據
        self.assertIn("美國=30, 日本=12", out)
        self.assertIn("x=2", out)                             # 公式文字

    def test_pptx(self):
        p = self.d / "簡報.pptx"
        make_pptx(p)
        out = read(p)["output"]
        self.assertLess(out.index("開場"), out.index("| A | B |"))   # 照 presentation.xml 的順序
        self.assertIn("--- 投影片 1 ---", out)
        self.assertIn("[圖1：", out)

    def test_cache_and_change(self):
        p = self.d / "x.docx"
        make_docx(p)
        a = docread.convert(p)
        self.assertEqual(docread.convert(p)["dir"], a["dir"])        # 沒改：用上次轉好的
        time.sleep(0.05)
        make_docx(p)
        os.utime(p, None)
        self.assertNotEqual(docread.convert(p)["dir"], a["dir"])     # 改了：重轉

    def test_broken(self):
        p = self.d / "壞.docx"
        p.write_bytes(b"PK\x03\x04 not really")
        r = read(p)
        self.assertTrue(r["is_error"])
        self.assertIn("轉文字失敗", r["output"])

    def test_retention_drops_old_conversions(self):
        p = self.d / "old.docx"
        make_docx(p)
        d = docread.convert(p)["dir"]
        old = time.time() - 8 * 86400
        for f in d.iterdir():
            os.utime(f, (old, old))
        retention._drop_files(retention._files(time.time() - 7 * 86400))
        self.assertFalse(d.exists())


def make_pdf(path, pages=("Hello 115 PDF",), image_page=True):
    """手寫最小的 PDF：每頁一行 Helvetica 字；最後加一頁只有一張 40x40 的圖。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for t in pages:
        s = f"BT /F1 18 Tf 50 700 Td ({t}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(s) + s + b"\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {len(objs)} 0 R >>")
        kids.append(len(objs))
    if image_page:
        px = bytes([200, 40, 40]) * 1600
        objs.append(b"<< /Type /XObject /Subtype /Image /Width 40 /Height 40 /ColorSpace /DeviceRGB "
                    b"/BitsPerComponent 8 /Length %d >>\nstream\n" % len(px) + px + b"\nendstream")
        img = len(objs)
        s = b"q 200 0 0 200 50 500 cm /Im1 Do Q"
        objs.append(b"<< /Length %d >>\nstream\n" % len(s) + s + b"\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 {img} 0 R >> >> "
                    f"/Contents {len(objs)} 0 R >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + (o.encode() if isinstance(o, str) else o) + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    Path(path).write_bytes(bytes(out))


class Pdf(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_text_and_image(self):
        p = self.d / "卷.pdf"
        make_pdf(p, pages=("Hello 115 PDF", "Page two"))
        r = read(p)
        self.assertFalse(r.get("is_error"), r["output"])
        out = r["output"]
        self.assertIn("pdf 轉成的文字", out)
        self.assertIn("--- 第 1 頁 ---", out)
        self.assertIn("Hello 115 PDF", out)
        self.assertLess(out.index("Hello 115"), out.index("Page two"))
        self.assertIn("1 頁幾乎只有圖", out)
        img = re.search(r"\[圖1：(.+?)\]", out).group(1)
        from PIL import Image

        with Image.open(img) as im:
            self.assertEqual(im.size, (40, 40))

    def test_encrypted(self):
        from pypdf import PdfReader, PdfWriter

        src = self.d / "a.pdf"
        make_pdf(src, image_page=False)
        w = PdfWriter(clone_from=PdfReader(src))
        w.encrypt(user_password="pw", owner_password="own", algorithm="RC4-128")
        p = self.d / "鎖.pdf"
        with open(p, "wb") as f:
            w.write(f)
        r = read(p)
        self.assertTrue(r["is_error"])
        self.assertIn("密碼", r["output"])

    def test_without_pypdf_hints_uv(self):
        import sys
        from unittest import mock

        p = self.d / "x.pdf"
        make_pdf(p, pages=("no pypdf",))
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            r = read(p)
        self.assertTrue(r["is_error"])
        self.assertIn("uv run --with pypdf", r["output"])

    def test_radicals_mapped(self):
        self.assertEqual("第⼀題 ⼈⼝，１？".translate(docread._RADICALS), "第一題 人口，１？")

    def test_files_not_held_open(self):
        """讀完原檔馬上放開：使用者刪得掉（Windows 上有人開著就刪不了）。"""
        import openpyxl

        pdf, docx, xlsx = self.d / "a.pdf", self.d / "b.docx", self.d / "c.xlsx"
        make_pdf(pdf)
        make_docx(docx)
        wb = openpyxl.Workbook()
        wb.active.append(["a", 1])
        wb.save(xlsx)
        from woof import pet

        for p in (pdf, docx, xlsx):
            read(p)
            pet._doc_text(p)
            os.remove(p)
            self.assertFalse(p.exists())


class Upload(unittest.TestCase):
    """附件改用 HTTP 存檔：WebSocket 只帶位置，不再因為訊息太大斷線。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        from woof import server

        self.srv = server
        self.c = TestClient(server.app)
        self.h = {"x-woof-token": server.TOKEN or ""}

    def test_upload_then_reference(self):
        raw = os.urandom(3 * 1024 * 1024)
        r = self.c.post("/api/files?name=05-115學測社會試卷.docx", content=raw, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["bytes"], len(raw))
        self.assertTrue(j["path"].endswith(".docx"))
        self.assertEqual(Path(j["path"]).read_bytes(), raw)
        b = self.srv._save_files([{"name": j["name"], "path": j["path"]}])
        self.assertEqual([(x["name"], x["bytes"]) for x in b], [("05-115學測社會試卷.docx", len(raw))])

    def test_path_outside_files_ignored(self):
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("x", encoding="utf-8")
        self.assertEqual(self.srv._save_files([{"name": "a", "path": str(outside)}]), [])
        self.assertEqual(self.srv._save_files([{"name": "a", "path": str(self.srv.FILES / "nope.txt")}]), [])

    def test_too_big_rejected_and_removed(self):
        before = set(self.srv.FILES.iterdir())
        old = self.srv.FILE_UPLOAD_MAX
        self.srv.FILE_UPLOAD_MAX = 1000
        try:
            r = self.c.post("/api/files?name=big.bin", content=b"x" * 5000, headers=self.h)
        finally:
            self.srv.FILE_UPLOAD_MAX = old
        self.assertEqual(r.status_code, 413)
        self.assertEqual(set(self.srv.FILES.iterdir()), before)

    def test_ws_limit_raised(self):
        self.assertGreaterEqual(self.srv.WS_MAX, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
