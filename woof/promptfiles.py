"""提示詞檔：設定檔、人物檔、記憶檔、使用者檔。

全部放在 WOOF 自己的 data/prompts/，由「設定 → 提示詞與記憶」直接編輯。
記憶與使用者檔用一行 § 分隔條目。
"""
from __future__ import annotations

from pathlib import Path

from .config import DATA

WOOF_ROOT = Path(__file__).resolve().parents[1]
ROOT = DATA / "prompts"

FILES = {
    "instructions": {"label": "設定檔", "file": "instructions.md",
                     "hint": "你寫給助手的全域規則與偏好格式，每個對話都會看到。"},
    "soul": {"label": "人物檔", "file": "SOUL.md",
             "hint": "助手的人物設定：身分、語氣、說話風格。"},
    "user": {"label": "使用者檔", "file": "USER.md",
             "hint": "你的偏好與背景。條目之間用一行 § 分隔。"},
    "memory": {"label": "記憶檔", "file": "MEMORY.md",
               "hint": "環境與過往經驗。條目之間用一行 § 分隔。"},
}
KEYS = list(FILES)
# 放進系統提示的順序：人物 → 設定 → 使用者偏好 → 記憶
ORDER = ["soul", "instructions", "user", "memory"]


def _spec(key: str) -> dict:
    if key not in FILES:
        raise KeyError(key)
    return FILES[key]


def path(key: str) -> Path:
    return ROOT / _spec(key)["file"]


def read(key: str) -> str:
    p = path(key)
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def write(key: str, text: str) -> None:
    p = path(key)
    ROOT.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        p.with_name(p.name + ".bak").write_bytes(p.read_bytes())  # 上一版留一份
    p.write_text(text, encoding="utf-8", newline="\n")
    if p.read_text(encoding="utf-8") != text:
        raise OSError(f"寫入後讀回不一致：{p}")


# 字數上限（WOOF 自訂預設）：超過只在 UI 提示，不截斷，避免悄悄丟內容
LIMITS = {"instructions": 4000, "soul": 3000, "user": 2500, "memory": 4000}


def info(key: str) -> dict:
    spec = _spec(key)
    p = path(key)
    text_now = read(key)
    return {
        "chars": len(text_now), "limit": LIMITS.get(key),
        "key": key, "label": spec["label"], "hint": spec["hint"], "file": spec["file"],
        "path": str(p), "exists": p.is_file(), "text": text_now,
        "entries": _count_entries(read(key)) if key in ("user", "memory") else None,
    }


def _count_entries(text: str) -> int:
    return len([e for e in text.split("§") if e.strip()])


def _entries(text: str) -> list[str]:
    return [e.strip() for e in text.split("§") if e.strip()]


def build_prompt() -> str | None:
    parts: list[str] = []
    for key in ORDER:
        text = read(key)
        if not text.strip():
            continue
        if key == "soul":
            parts.append("## 人物\n" + text.strip())
        elif key == "instructions":
            parts.append("## 設定（使用者寫的規則）\n" + text.strip())
        elif key == "user":
            ents = _entries(text)
            if ents:
                parts.append("## 使用者偏好（務必遵守）\n" + "\n".join(f"- {e}" for e in ents))
        elif key == "memory":
            ents = _entries(text)
            if ents:
                parts.append("## 記憶（環境與過往經驗）\n" + "\n".join(f"- {e}" for e in ents))
    return "\n\n".join(parts) or None
