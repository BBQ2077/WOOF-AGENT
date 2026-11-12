"""對外連線：綁定位址、登入密碼與有期限的登入憑證。

預設只聽本機（bind = "local"）、沒有密碼，存取 token 由桌面程式每次啟動時產生，
行為和開放之前完全一樣。要讓別台電腦連進來，在 config.toml 寫：

    [server]
    bind = "lan"                        # local（預設）/ lan / public
    password_hash = "pbkdf2_sha256$…"   # 執行 python -m woof set-password 產生
    session_days = 30                   # 登入憑證可以用幾天
    # 對外（public）需要 HTTPS，下面兩者擇一：
    https_cert = "C:/certs/fullchain.pem"
    https_key = "C:/certs/privkey.pem"
    trust_proxy = true                  # 已經在反向代理後面，HTTPS 由它處理

密碼只存 pbkdf2 雜湊；登入換到的是 HMAC 簽章、有有效期限的憑證，主機重開也還能用，
過期就重新登入。簽章用的金鑰存在 data/server_secret，不會外流。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import tomllib

import tomli_w

from .config import CONFIG_PATH, DATA

BINDS = ("local", "lan", "public")
PUBLIC_API = frozenset({"/api/login", "/api/session"})
PBKDF2_ROUNDS = 240_000
DEFAULTS: dict = {"bind": "local", "session_days": 30, "trust_proxy": False}
SECRET_FILE = DATA / "server_secret"

_STATE: dict = {}
_SECRET: bytes = b""


# ---------------------------------------------------------------- 設定
def load(cfg: dict | None = None) -> dict:
    """讀 [server] 這一段，補上預設值。cfg 傳入時不碰設定檔（測試用）。"""
    src = cfg if cfg is not None else _read_config()
    sec = src.get("server") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if sec.get(k) not in (None, ""):
            out[k] = sec[k]
    out["bind"] = str(out["bind"]).lower()
    if out["bind"] not in BINDS:
        out["bind"] = "local"
    try:
        out["session_days"] = max(1, min(365, int(out["session_days"])))
    except (TypeError, ValueError):
        out["session_days"] = DEFAULTS["session_days"]
    out["trust_proxy"] = bool(out["trust_proxy"])
    out["password_hash"] = str(sec.get("password_hash") or "")
    out["https_cert"] = str(sec.get("https_cert") or "")
    out["https_key"] = str(sec.get("https_key") or "")
    return out


def _read_config() -> dict:
    path = CONFIG_PATH  # 跟著目前這組設定走（多組設定）
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_password_hash(pw_hash: str) -> None:
    """只動 [server] 的 password_hash，其他欄位照原樣留著。"""
    path = CONFIG_PATH
    cfg: dict = {}
    if path.exists():
        with path.open("rb") as f:
            cfg = tomllib.load(f)
    sec = dict(cfg.get("server") or {})
    sec["password_hash"] = pw_hash
    cfg["server"] = sec
    path.write_text(tomli_w.dumps(cfg), encoding="utf-8")


def configure(cfg: dict | None = None, **kw) -> dict:
    """後端啟動時套用一次：之後 middleware 都用這份。cfg 傳入時不讀設定檔（測試用）。"""
    global _STATE
    st = load(cfg)
    st.update({k: v for k, v in kw.items() if v is not None})
    st["bind"] = str(st["bind"]).lower()
    if st["bind"] not in BINDS:
        st["bind"] = "local"
    st["open_mode"] = st["bind"] != "local"
    _STATE = st
    return st


def reset() -> None:
    global _STATE
    _STATE = {}


def state() -> dict:
    if not _STATE:
        configure()
    return _STATE


def bind_host(bind: str) -> str:
    return "127.0.0.1" if bind == "local" else "0.0.0.0"


def startup_error(st: dict) -> str:
    """回傳不能開下去的原因；空字串＝可以開。"""
    if st["bind"] == "local":
        return ""
    if not st["password_hash"]:
        return "開放區網／外網要先設登入密碼：python -m woof set-password"
    if st["bind"] == "public" and not (st["https_cert"] and st["https_key"]) and not st["trust_proxy"]:
        return "對外開放要 HTTPS：在 [server] 填 https_cert 與 https_key，或放在反向代理後面並設 trust_proxy = true"
    return ""


# ---------------------------------------------------------------- 密碼
def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _ub64(text: str) -> bytes:
    import base64

    return base64.b64decode(text.encode("ascii"))


def _same(a: str, b: str) -> bool:
    """固定時間比較。用 bytes 比，非 ASCII 的輸入不會拋錯，直接算不一樣。"""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${_b64(salt)}${_b64(dk)}"


def verify_password(pw: str | None, stored: str | None) -> bool:
    if not pw or not stored:
        return False
    try:
        algo, rounds, salt, dk = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), _ub64(salt), int(rounds))
        return hmac.compare_digest(calc, _ub64(dk))
    except Exception:  # noqa: BLE001  編碼壞掉或格式不對就當密碼不對
        return False


# ---------------------------------------------------------------- 登入憑證
def _secret() -> bytes:
    global _SECRET
    if _SECRET:
        return _SECRET
    raw = b""
    try:
        if SECRET_FILE.exists():
            raw = SECRET_FILE.read_bytes()
        if len(raw) < 32:
            raw = secrets.token_bytes(32)
            SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SECRET_FILE.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, SECRET_FILE)
    except OSError:
        if len(raw) < 32:
            raw = secrets.token_bytes(32)   # 檔案寫不了就拿這次的，重開要重新登入
    _SECRET = raw
    return raw


def issue(days: int) -> tuple[str, int]:
    """發一張憑證，回 (token, 到期時間)。"""
    exp = int(time.time()) + max(1, int(days)) * 86400
    payload = _b64(json.dumps({"exp": exp}, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"v1.{payload}.{sig}", exp


def verify(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        ver, payload, sig = str(token).split(".")
        if ver != "v1":
            return None
        want = _b64(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())
        if not _same(want, sig):
            return None
        data = json.loads(_ub64(payload))
        if float(data.get("exp") or 0) <= time.time():
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 判斷
def check(open_mode: bool, path: str, token: str | None, machine_token: str) -> bool:
    """這個請求能不能過。

    open_mode＝有開放區網或外網（要登入憑證或密碼）；machine_token＝啟動時給後端的
    TOKEN（桌面程式每次換）。只綁本機時跟以前一樣：有 TOKEN 就一定要對，沒有 TOKEN 就全放行。
    """
    if not open_mode:
        return not machine_token or _same(token or "", machine_token)
    if path in PUBLIC_API:
        return True
    if token:
        if machine_token and _same(token, machine_token):
            return True
        return verify(token) is not None
    return False