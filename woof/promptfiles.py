"""提示詞檔：設定檔、人物檔、記憶檔、使用者檔。

全部放在 WOOF 自己的 data/prompts/，由「設定 → 提示詞與記憶」直接編輯。
記憶與使用者檔用一行 § 分隔條目。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import DATA

WOOF_ROOT = Path(__file__).resolve().parents[1]
ROOT = DATA / "prompts"

DEFAULT_IDENTITY = """你是 WOOF，在使用者自己的 Windows 電腦上執行。使用者的要求優先。
- 用繁體中文回覆，說人話，先講結論。
- 需要動手時直接用工具完成，根據實際結果回報，不要編造輸出。
- 改完能驗證就驗證（重讀檔案、跑一次指令）；沒驗證到的明說。
- 終端機是 Git Bash（POSIX 語法），原生 Windows 程式的路徑用 C:/... 形式。
- 產出檔案放進對應的專案資料夾，不要丟在使用者家目錄根層。"""

DEFAULT_IDENTITY_EN = """You are WOOF, running on the user's own Windows computer. The user's actual request comes first.
- Reply in English unless the user asks for another language. Use plain words and lead with the conclusion.
- When something needs doing, use the tools to do it, then report from the actual results. Never make up output.
- After a change, verify it when you can (re-read the file, run the command once) and say plainly what you could not verify.
- The terminal is Git Bash (POSIX syntax); give native Windows programs paths in C:/... form.
- Put output files in the relevant project folder, not in the root of the user's home directory."""

FILES = {
    "identity": {"label": "身份與環境", "file": "IDENTITY.md",
                 "hint": "系統提示的第一段：助手的身分、環境與工作規則。每個對話都會看到；改了就自動儲存，下一則訊息生效。"},
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
ORDER = ["soul", "instructions", "user", "memory"]   # 進系統提示的順序（identity 已在最前面，不重複放）
TABS = ["identity", "soul", "instructions", "user", "memory"]   # 設定頁分頁順序


def _spec(key: str) -> dict:
    if key not in FILES:
        raise KeyError(key)
    return FILES[key]


def path(key: str) -> Path:
    return ROOT / _spec(key)["file"]


def read(key: str) -> str:
    p = path(key)
    if not p.is_file() and key == "identity":
        # 第一次就寫成檔案：設定頁要看得到、改得動，不能只有程式裡的一份死文字
        write(key, DEFAULT_IDENTITY)
        return DEFAULT_IDENTITY
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def write(key: str, text: str) -> None:
    p = path(key)
    ROOT.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        p.with_name(p.name + ".bak").write_bytes(p.read_bytes())  # 上一版留一份
    p.write_text(text, encoding="utf-8", newline="\n")
    if p.read_text(encoding="utf-8") != text:
        raise OSError(f"寫入後讀回不一致：{p}")


# 字數上限預設值（可在「提示詞與記憶」頁直接改，存在 data/prompt_limits.json）
# 模型用 memory 工具寫入時超過就拒絕（不截斷，免得悄悄丟內容）；UI 手動編輯只提示
LIMITS = {"identity": 3000, "instructions": 4000, "soul": 3000, "user": 2500, "memory": 4000}
LIMITS_FILE = DATA / "prompt_limits.json"
LIMIT_MIN, LIMIT_MAX = 200, 200000


def limits() -> dict:
    """預設值 + 使用者改過的值；壞掉或超出範圍的忽略。"""
    out = dict(LIMITS)
    try:
        raw = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return out
    for k, v in (raw or {}).items() if isinstance(raw, dict) else []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if k in out and LIMIT_MIN <= n <= LIMIT_MAX:
            out[k] = n
    return out


def limit(key: str) -> int:
    return int(limits().get(key, 0))


def set_limit(key: str, value) -> int:
    """改某個檔的字數上限（寫回 data/prompt_limits.json）。"""
    if key not in FILES:
        raise KeyError(key)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("上限要填數字")
    if not LIMIT_MIN <= n <= LIMIT_MAX:
        raise ValueError(f"上限要在 {LIMIT_MIN}～{LIMIT_MAX} 字之間")
    cur: dict = {}
    try:
        raw = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cur = {k: int(v) for k, v in raw.items() if k in FILES and str(v).lstrip("-").isdigit()}
    except Exception:
        cur = {}
    cur[key] = n
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIMITS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return n


def info(key: str) -> dict:
    spec = _spec(key)
    p = path(key)
    text_now = read(key)
    return {
        "chars": len(text_now), "limit": limit(key),
        "key": key, "label": spec["label"], "hint": spec["hint"], "file": spec["file"],
        "path": str(p), "exists": p.is_file(), "text": text_now,
        "entries": _count_entries(read(key)) if key in ("user", "memory") else None,
    }


def _count_entries(text: str) -> int:
    return len([e for e in text.split("§") if e.strip()])


def _entries(text: str) -> list[str]:
    return [e.strip() for e in text.split("§") if e.strip()]


def memory_switch(key: str, dflt: bool = True) -> bool:
    """「提示詞與記憶」頁的兩個開關（值存在 memory 插件的設定裡，插件關掉也讀得到）。"""
    try:
        from woof import store

        st = store.get_plugin_state("memory") or {}
        v = (st.get("settings") or {}).get(key)
    except Exception:
        v = None
    return dflt if v is None else bool(v)


def build_prompt_gated() -> str | None:
    """核心用：提示詞檔一律會進系統提示，只有「讀」關掉時才拿掉記憶檔與使用者檔。"""
    return build_prompt() if memory_switch("allow_read") else build_prompt(skip=("user", "memory"))


def build_prompt(skip: tuple[str, ...] = ()) -> str | None:
    parts: list[str] = []
    for key in ORDER:
        if key in skip:
            continue
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
