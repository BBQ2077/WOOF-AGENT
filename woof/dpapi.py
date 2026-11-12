"""Windows DPAPI：用目前登入的 Windows 帳號加解密（keys.toml 的 key 用）。

加密後的字串長這樣：dpapi:BASE64。只有同一台電腦、同一個 Windows 帳號解得開；
換電腦或換帳號就要重新貼 key。非 Windows 不加密，照原樣存。
"""
from __future__ import annotations

import base64
import sys

PREFIX = "dpapi:"
_ENTROPY = b"WOOF keys v1"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(fn, data: bytes) -> bytes:
        src, _k1 = _blob(data)
        ent, _k2 = _blob(_ENTROPY)
        out = _Blob()
        if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "DPAPI 失敗")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def available() -> bool:
        return True

    def encrypt(text: str) -> str:
        return PREFIX + base64.b64encode(_call(_crypt32.CryptProtectData, text.encode("utf-8"))).decode("ascii")

    def decrypt(value: str) -> str:
        if not value.startswith(PREFIX):
            return value
        return _call(_crypt32.CryptUnprotectData, base64.b64decode(value[len(PREFIX):])).decode("utf-8")
else:
    def available() -> bool:
        return False

    def encrypt(text: str) -> str:
        return text

    def decrypt(value: str) -> str:
        if value.startswith(PREFIX):
            raise OSError("這台電腦不是 Windows，解不開 DPAPI 加密的 key")
        return value
