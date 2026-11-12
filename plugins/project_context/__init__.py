"""專案指示檔：讀工作目錄（往上到 git 根）的 AGENTS.md、CLAUDE.md，放進系統提示。

跟 Codex 的 AGENTS.md、Claude Code 的 CLAUDE.md 是同一個慣例。檔案只讀，不改。
"""
from __future__ import annotations

from pathlib import Path

NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_CHARS = 12000
MAX_LEVELS = 6


def find_files(cwd: str) -> list[Path]:
    """從 cwd 往上找，遇到含 .git 的目錄就停。同一層只取第一個（AGENTS.md 優先）。回傳外層在前、內層在後。"""
    out: list[Path] = []
    if not cwd:
        return out
    try:
        d = Path(cwd).resolve()
    except OSError:
        return out
    for _ in range(MAX_LEVELS):
        for n in NAMES:
            f = d / n
            if f.is_file():
                out.append(f)
                break
        if (d / ".git").exists() or d.parent == d:
            break
        d = d.parent
    return list(reversed(out))


def build_text(cwd: str) -> str | None:
    budget = MAX_CHARS
    parts: list[str] = []
    for f in find_files(cwd):
        try:
            t = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not t:
            continue
        if len(t) > budget:
            t = t[:max(budget, 0)] + "\n…（超過上限，後面省略）"
        budget -= len(t)
        parts.append(f"### 專案指示：{f}\n{t}")
        if budget <= 0:
            break
    if not parts:
        return None
    return "以下是這個專案的指示檔（AGENTS.md／CLAUDE.md），請照著做：\n\n" + "\n\n".join(parts)


def register(woof):
    def build_prompt(ctx):
        return build_text(getattr(ctx, "cwd", "") or "")

    woof.on("build_prompt", build_prompt, order=20)
