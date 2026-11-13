"""思考強度：使用者選的是「想要的強度」，實際送出時依模型支援的檔位向下兼容。

檔位由低到高：minimal < low < medium < high < xhigh < max < ultra（標籤：最小／低／中／高／極高／最高／超高）。
- 模型有這一檔 → 原樣送出。
- 模型沒有 → 往下找最近的一檔（例：xhigh 沒有 → high；high 沒有 → medium）。
- 往下都沒有 → 用模型最低的一檔。
- 想關閉（off）但模型不能關 → 用最低一檔。
- 只有開／關的模型（off／on）：off → off，其餘 → on。
"""
from __future__ import annotations

SCALE = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def norm(v: str | None) -> str | None:
    """檔位一律小寫比對：設定檔或舊資料寫 MAX / Max / HIGH 都認（送 API 的值是這組小寫字）。"""
    return v.strip().lower() if isinstance(v, str) else v


def resolve(efforts: list[str] | None, want: str | None, default: str | None = None) -> str | None:
    es = [e for e in (norm(x) for x in (efforts or [])) if e]
    want, default = norm(want), norm(default)
    if not es:
        return None
    if not want:
        return default if default in es else es[-1]
    if want in es:
        return want
    if set(es) <= {"off", "on"}:
        if want in ("off", "none"):
            return "off" if "off" in es else "on"
        return "on" if "on" in es else "off"
    graded = sorted((e for e in es if e in SCALE), key=SCALE.index)
    if want in ("off", "none"):
        return "off" if "off" in es else (graded[0] if graded else es[0])
    if want not in SCALE:  # 例如 "on"：沿用模型預設
        return default if default in es else (graded[-1] if graded else es[-1])
    below = [e for e in graded if SCALE.index(e) <= SCALE.index(want)]
    if below:
        return below[-1]
    return graded[0] if graded else (default if default in es else es[0])
