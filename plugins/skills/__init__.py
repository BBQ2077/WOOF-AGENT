"""Skills：讀取 SKILL.md（含附屬檔案），清單放進系統提示，需要時載入全文；也能建立或覆寫 skill。

權限分兩個開關，預設值在插件設定，每個對話也能各自覆寫（對話 → 插件 → Skills 權限）：
  讀取 read ：列出 skill 清單、skill_view 載入全文
  寫入 write：skill_write 建立或覆寫 SKILL.md；只能寫到 write_dir（WOOF 自己的資料夾），覆寫前先備份舊檔
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
WOOF_ROOT = Path(__file__).resolve().parents[2]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


def _field(fm: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _on(v, default: bool) -> bool:
    """設定值可能是 bool、字串或數字，統一轉成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str) and v.strip():
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _enabled(raw) -> set[str]:
    """插件設定 enabled_skills：逗號或換行分隔的 skill 名稱，空＝全部關閉。"""
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = re.split(r"[,\n]", str(raw or ""))
    return {str(x).strip() for x in items if str(x).strip()}


def _write_dir(raw) -> Path:
    """相對路徑一律解成「目前這組的 data」底下的子目錄（data/skills 與 skills 指同一處）。"""
    from woof.config import DATA

    p = Path(os.path.expanduser(str(raw or "data/skills")))
    if p.is_absolute():
        return p
    parts = p.parts[1:] if p.parts and p.parts[0] == "data" else p.parts
    return DATA.joinpath(*parts) if parts else DATA


def scan(dirs: list[Path]) -> dict[str, dict]:
    skills: dict[str, dict] = {}
    for root in dirs:
        if not root.is_dir():
            continue
        for md in root.rglob("SKILL.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _FM.match(text)
            fm = m.group(1) if m else ""
            name = _field(fm, "name") or md.parent.name
            rel = md.parent.relative_to(root).parts
            category = rel[0] if len(rel) > 1 else ""
            skills.setdefault(name, {
                "name": name, "description": _field(fm, "description"), "category": category,
                "dir": md.parent, "file": md,
            })
    return skills


def list_all(woof) -> dict:
    """給設定頁用：列出所有可見的 skill（含分類、來源資料夾）與勾選狀態。不載入全文。"""
    on = _enabled(woof.settings.get("enabled_skills"))
    raw = woof.settings.get("dirs") or ""
    dirs_ = [Path(os.path.expanduser(d.strip())) for d in raw.split(";") if d.strip()]
    wd = _write_dir(woof.settings.get("write_dir"))
    if wd not in dirs_:
        dirs_.append(wd)
    rows = []
    for name, s in sorted(scan(dirs_).items(), key=lambda kv: (kv[1]["category"] or "其他", kv[0])):
        src = "WOOF" if s["dir"].is_relative_to(WOOF_ROOT) else "外部"
        rows.append({"name": name, "description": s["description"], "category": s["category"] or "其他",
                     "source": src, "path": s["dir"].as_posix(), "enabled": name in on})
    return {"dirs": [d.as_posix() for d in dirs_], "skills": rows, "enabled": sorted(on)}


def register(woof):
    def dirs() -> list[Path]:
        raw = woof.settings.get("dirs") or ""
        out = [Path(os.path.expanduser(d.strip())) for d in raw.split(";") if d.strip()]
        # 留空＝不讀任何外部資料夾；只有明確填入的路徑才會被掃描
        wd = _write_dir(woof.settings.get("write_dir"))
        if wd not in out:
            out.append(wd)  # WOOF 自己建立的 skill 也要能讀
        return out

    def perms(ctx) -> tuple[bool, bool]:
        """這個對話實際的讀／寫權限：對話覆寫優先，沒有就跟插件設定。"""
        sk = ((getattr(ctx, "session", None) or {}).get("engine_state") or {}).get("skills") or {}
        r = sk["read"] if isinstance(sk.get("read"), bool) else _on(woof.settings.get("read"), True)
        w = sk["write"] if isinstance(sk.get("write"), bool) else _on(woof.settings.get("write"), False)
        return r, w

    def chosen(sk: dict) -> dict:
        on = _enabled(woof.settings.get("enabled_skills"))
        return {k: v for k, v in sk.items() if k in on}

    def build_prompt(ctx):
        if not perms(ctx)[0]:
            return None
        sk = chosen(scan(dirs()))
        if not sk:
            return None
        native = ctx.model_cfg.get("provider") in ctx.registry.engines
        by_cat: dict[str, list[dict]] = {}
        for s in sk.values():
            by_cat.setdefault(s["category"] or "其他", []).append(s)
        if native:
            lines = ["## Skills",
                     "任務相關時，先用 Read 讀取對應 skill 的 SKILL.md 全文再動手；同資料夾內的附屬檔案視需要讀取。"]
        else:
            lines = ["## Skills",
                     "任務相關時，先用 skill_view 載入對應 skill 全文再動手；附屬檔案用 skill_view 的 file_path 讀取。"]
        for cat in sorted(by_cat):
            lines.append(f"{cat}:")
            for s in sorted(by_cat[cat], key=lambda x: x["name"]):
                desc = f": {s['description']}" if s["description"] else ""
                where = f"  [{s['file'].as_posix()}]" if native else ""
                lines.append(f"  - {s['name']}{desc}{where}")
        return "\n".join(lines)

    def _excl(ctx) -> set:
        """讀取關 → 不給 skill_view；寫入關 → 不給 skill_write（prepare 與面板預覽共用）。"""
        r, w = perms(ctx)
        out = set()
        if not r:
            out.add("skill_view")
        if not w:
            out.add("skill_write")
        return out

    def prepare(ctx):
        """每回合依權限把工具關掉。"""
        ctx.tool_exclude |= _excl(ctx)

    async def skill_view(args: dict, ctx) -> dict:
        sk = chosen(scan(dirs()))
        name = args.get("name", "")
        s = sk.get(name)
        if s is None:
            near = [n for n in sk if name.lower() in n.lower()]
            return {"output": f"找不到 skill「{name}」" + (f"，相近的有：{', '.join(near)}" if near else ""),
                    "is_error": True}
        fp = args.get("file_path")
        if fp:
            target = (s["dir"] / fp).resolve()
            if not target.is_relative_to(s["dir"].resolve()) or not target.is_file():
                return {"output": f"找不到附屬檔案：{fp}", "is_error": True}
            return {"output": target.read_text(encoding="utf-8", errors="replace")}
        body = s["file"].read_text(encoding="utf-8", errors="replace")
        linked = [p.relative_to(s["dir"]).as_posix() for p in s["dir"].rglob("*")
                  if p.is_file() and p.name != "SKILL.md"]
        extra = ("\n\n---\n附屬檔案（用 file_path 讀取）：\n" + "\n".join(f"- {x}" for x in sorted(linked)[:100])) if linked else ""
        return {"output": f"[skill 位置：{s['dir'].as_posix()}]\n\n{body}{extra}"}

    async def skill_write(args: dict, ctx) -> dict:
        name = str(args.get("name") or "").strip()
        content = str(args.get("content") or "")
        if not _NAME.fullmatch(name):
            return {"output": "name 只能用英文、數字、. _ -，例如 my-skill", "is_error": True}
        if not content.lstrip().startswith("---"):
            return {"output": "content 要是完整的 SKILL.md，開頭用 --- 的 frontmatter（至少含 name 與 description）",
                    "is_error": True}
        wd = _write_dir(woof.settings.get("write_dir"))
        s = scan(dirs()).get(name)
        if s is not None and not s["file"].resolve().is_relative_to(wd.resolve()):
            return {"output": f"「{name}」位於 WOOF 以外（{s['dir']}），不能覆寫；請換一個名字", "is_error": True}
        target = s["file"] if s is not None else wd / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        note = ""
        if target.exists():
            bak = target.with_name(f"SKILL.md.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            bak.write_bytes(target.read_bytes())
            note = f"，舊版已備份為 {bak.name}"
        target.write_text(content, encoding="utf-8", newline="")
        ok = target.read_text(encoding="utf-8") == content
        return {"output": f"已寫入 {target}{note}" + ("" if ok else "（讀回內容不一致）"), "is_error": not ok}

    woof._reg.skills_lister = lambda: list_all(woof)  # 設定頁的 Skills 分區用
    woof.on("build_prompt", build_prompt, order=50)
    woof.on("prepare_turn", prepare, order=50)
    woof.on("tool_exclude", _excl, order=50)
    woof.add_tool("skill_view", "載入一個 skill 的全文，或它的附屬檔案。",
                  {"type": "object", "properties": {
                      "name": {"type": "string"}, "file_path": {"type": "string", "description": "附屬檔案相對路徑"}},
                   "required": ["name"]}, skill_view)
    woof.add_tool("skill_write",
                  "建立或覆寫一個 skill 的 SKILL.md（只寫到 WOOF 的 skill 資料夾，覆寫前會備份舊檔）。"
                  "content 必須是完整的 SKILL.md，開頭為 --- 的 frontmatter（含 name、description），之後是內容。",
                  {"type": "object", "properties": {
                      "name": {"type": "string", "description": "skill 名稱，英文數字與 . _ -"},
                      "content": {"type": "string", "description": "完整 SKILL.md 內容"}},
                   "required": ["name", "content"]}, skill_write)
