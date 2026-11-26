"""web_search 插件的網頁整理（plugins/web_search/page.py）：清雜訊、目錄、關鍵字段落、預覽長度。

測試資料是仿照常見網站結構編的（文件站側欄、程式碼託管站的空標題、百科導覽框、沒有換行的列表頁）。
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "web_page_t", Path(__file__).resolve().parents[1] / "plugins" / "web_search" / "page.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
CFG = {"preview": 4000, "hit": 3000, "lead": 500, "outline": 600}


def para(tag, n=40):
    return "".join(f"{tag}的第{i}句說明，內容各不相同。" for i in range(n))


def doc(text, url="https://docs.example.org/a"):
    return {"url": url, "text": P.clean(text), "title": "T", "desc": "", "path": "X.md"}


class Clean(unittest.TestCase):
    def test_empty_headings_dropped(self):
        md = "## Navigation Menu\n\n## About\n\n### Resources\n\n### Stars\n\n# 專案\n\n真的內文。\n\n## Latest commit\n\n## 安裝\n\n先下載。"
        heads = [t for _lv, t, _n in P.outline(P.clean(md))]
        self.assertEqual(heads, ["專案", "安裝"])

    def test_sidebar_top_and_bottom_dropped(self):
        side = "### [目錄](../contents.html)\n\n#### 上個主題\n\n[string --- 字串](string.html)\n\n### 此頁面\n\n### 導航"
        body = "\n\n".join(para(f"第{i}節", 3) for i in range(12))
        t = P.clean(f"{side}\n\n# re 模組\n\n{body}\n\n{side}")
        self.assertNotIn("上個主題", t)
        self.assertNotIn("此頁面", t)
        self.assertEqual(P.outline(t)[0][1], "re 模組")

    def test_echoed_code_spans_dropped(self):
        t = P.clean("# A\n\n設定在 `data` 和 `config.toml`。\n\n`data`\n`config.toml`\n\n下一段。")
        self.assertEqual(t.count("`data`"), 1)

    def test_machine_data_and_images(self):
        blob = '{"a":[1,2,3],"b":{"c":"d"},' * 120
        t = P.clean(f"# A\n\n![logo](x.png)\n\n內文。\n\n{blob}")
        self.assertNotIn("logo", t)
        self.assertNotIn('"a"', t)


class Plain(unittest.TestCase):
    def test_links(self):
        s = P.plain("看 [目](https://zh.wikipedia.org/wiki/%E7%9B%AE_(%E5%9C%8D%E6%A3%8B) \"目 (圍棋)\") 和 "
                    "[外站](https://other.com/x)[[註 2]](https://zh.wikipedia.org/wiki/A#cite_note-1)[3]", "zh.wikipedia.org")
        self.assertEqual(s, "看 目 和 [外站](https://other.com/x)")

    def test_table(self):
        s = P.plain("|  |  |\n| --- | --- |\n| 活躍時期 | 周朝至今 |")
        self.assertEqual(s, "活躍時期 | 周朝至今")


class Find(unittest.TestCase):
    def test_section_beats_navbox(self):
        nav = "| 圍棋百科 |\n| --- |\n| " + " ".join(f"[规则{i}](/wiki/R{i}) [贴目{i}](/wiki/K{i})" for i in range(200)) + " |"
        md = (f"# 围棋\n\n{para('开头')}\n\n## 规则\n\n### 基本规则\n\n{para('基本规则')}\n\n### 贴目\n\n黑棋贴给白棋6目半。\n\n"
              f"## 文化\n\n{para('文化')}\n\n## 外部链接\n\n{nav}")
        body, nhit, nshow = P.find(P.clean(md), ["規則", "貼目"], 3000, "zh.wikipedia.org")
        secs = [ln for ln in body.split("\n") if ln.startswith("§ ")]
        self.assertTrue(secs[0].startswith("§ 规则"), secs)
        self.assertIn("6目半", body)
        self.assertNotIn("圍棋百科", body)
        self.assertLessEqual(len(body), 3200)

    def test_single_line_page(self):
        items = " | ".join(f"{i}. Story number {i} about topic{i} ( site{i}.com ) {i} points" for i in range(300))
        t = P.clean(items)
        self.assertEqual(t.count("\n"), 0)
        body, nhit, nshow = P.find(t, ["topic250", "topic20 "], 3000)
        self.assertEqual(nshow, 2)
        self.assertIn("topic250", body)
        self.assertLess(len(body), 1500)

    def test_heading_with_only_subsections(self):
        md = f"# 圍棋\n\n{para('開頭')}\n\n## 棋盘坐标系\n\n### 四隅旋分\n\n古代記譜法。\n\n### 字母坐标\n\n橫坐標用字母。\n\n## 文化\n\n{para('文化')}"
        body, _n, nshow = P.find(P.clean(md), ["棋盤坐標"], 3000)
        self.assertTrue(body.startswith("§ 棋盘坐标系"), body[:60])
        self.assertIn("古代記譜法", body)
        self.assertNotIn("文化的第", body)

    def test_traditional_matches_simplified(self):
        self.assertEqual(P.fold("規則貼目"), P.fold("规则贴目"))


class Preview(unittest.TestCase):
    def test_short_page_full(self):
        out = P.preview(doc("# A\n\n短短一頁。"), [], False, CFG)
        self.assertIn("短短一頁", out)
        self.assertNotIn("### 目錄", out)

    def test_long_page_budget(self):
        md = "# 手冊\n\n" + "\n\n".join(f"## 第{i}章\n\n{para(f'第{i}章')}" for i in range(40))
        d = doc(md)
        for keys in ([], ["第7章的第3句"], ["不存在的字"]):
            out = P.preview(d, keys, False, CFG)
            self.assertIn("### 目錄", out)
            self.assertLess(len(out), 4800, keys)
        self.assertIn("沒有找到", P.preview(d, ["不存在的字"], False, CFG))
        self.assertGreater(len(P.preview(d, [], True, CFG)), 30000)

    def test_outline_uses_levels_present(self):
        md = "#### Authorizations\n\n" + para("a") + "\n\n#### Body\n\n" + para("b")
        self.assertEqual([t for _l, t, _n in P.outline(P.clean(md))], ["Authorizations", "Body"])

    def test_lead_skips_language_list(self):
        langs = "\n".join(f"* {x}" for x in "Afrikaans Alemannisch Asturianu Bosanski Català Čeština Dansk Deutsch Eesti Suomi".split())
        md = f"# 圍棋\n\n{langs}\n\n**圍棋**是一種策略棋類。\n\n" + "\n\n".join(para(f"段{i}", 2) for i in range(80))
        self.assertTrue(P.lead(P.clean(md), 300).startswith("**圍棋**"))


if __name__ == "__main__":
    unittest.main()
