"""圖片／影片生成：給「生成型」模型用（OpenAI 相容的 /images/generations、/videos）。

兩種用法：
1. 對話直接選生成型模型（例如 agnes-image-2.1-flash）：使用者那則訊息就是提示詞，附的圖當參考圖，回覆是生成的檔案。
2. 一般聊天模型呼叫 image_generate／video_generate 工具（media 插件），用設定裡指定的生成模型。

生成的檔案存在 data/media/<日期>/，回覆裡寫 MEDIA:<路徑>，介面直接顯示。
"""
from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from woof import caps
from woof.config import DATA

MEDIA = DATA / "media"

_IMG = re.compile(r"(^|[-_/.:])(image|img|imagen|dall-?e|gpt-image|flux|sdxl|stable-diffusion|seedream|ideogram|recraft)([-_/.:\d]|$)", re.I)
_VID = re.compile(r"(^|[-_/.:])(video|veo|kling|sora|seedance|hailuo|wan2|cogvideo|gemini-omni)([-_/.:\d]|$)", re.I)
KIND_LABEL = {"image": "生圖", "video": "影片"}


def kind_of(m: dict | None) -> str:
    """chat／image／video。設定檔寫明的優先，再來是撞錯學到的，最後看模型名稱。"""
    if not m:
        return "chat"
    k = m.get("kind")
    if k in ("chat", "image", "video"):
        return k
    k = caps.learned(m["id"]).get("kind")
    if k in ("chat", "image", "video"):
        return k
    if "openrouter.ai" in str(m.get("base_url") or ""):
        return "chat"  # OpenRouter 的生圖模型走 chat completions，不走 /images/generations
    name = str(m.get("model") or m["id"])
    if _VID.search(name):
        return "video"
    if _IMG.search(name):
        return "image"
    return "chat"


def kind_from_error(text: str) -> str | None:
    """服務商回「這是圖片／影片模型，請用 /images/generations、/videos」時，學起來。"""
    t = (text or "").lower()
    if "images/generations" in t or "is an image model" in t:
        return "image"
    if "/videos" in t or "is a video model" in t:
        return "video"
    return None


def _agnes(m: dict) -> bool:
    return "agnes-ai.com" in str(m.get("base_url") or "")


def _key(woof, m: dict) -> str:
    pool = woof.keys(m.get("key_pool") or m.get("provider"))
    e = pool.get()
    if e is None:
        raise RuntimeError(f"「{m.get('label', m['id'])}」沒有可用的 API key（金鑰池「{pool.name}」），請到「設定 → API 金鑰」貼上")
    return e.key





def _sdir(session_id: str) -> Path:
    sid = "".join(ch for ch in str(session_id or "") if ch.isalnum() or ch in "-_")
    return MEDIA / (sid or "_none")


def _out(ext: str, session_id: str = "") -> Path:
    """生成檔放在對話自己的資料夾（data/media/<對話 id>/），刪對話時整個一起刪；要留的成品另外存到 save_to。"""
    d = _sdir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"


def drop(session_id: str) -> None:
    """刪對話時呼叫：這個對話生成的圖片、影片一起刪（存到 save_to 的成品不在這裡，不受影響）。"""
    import shutil

    if session_id:
        shutil.rmtree(_sdir(session_id), ignore_errors=True)


def save_to(path: str, dest: str, cwd: str = "") -> str:
    """把生成檔複製一份到成品位置：dest 是資料夾（或結尾 / \\）就用原檔名，否則當檔名。回複製後的路徑。"""
    import shutil

    d = Path(dest).expanduser()
    if not d.is_absolute():
        d = Path(cwd or ".") / d
    if d.is_dir() or str(dest).endswith(("/", "\\")) or not d.suffix:
        d.mkdir(parents=True, exist_ok=True)
        d = d / Path(path).name
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.suffix.lower() != Path(path).suffix.lower():
            d = d.with_suffix(Path(path).suffix)
    shutil.copy2(path, d)
    return str(d)


def _data_uri(path: str) -> str:
    p = Path(path)
    mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(p.suffix.lower(), "image/png")
    return f"data:{mt};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _err(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error") if isinstance(j, dict) else None
        msg = (e.get("message") if isinstance(e, dict) else e) or j.get("message") or j
    except Exception:
        msg = r.text
    return f"HTTP {r.status_code}：{str(msg)[:300]}"


def family(m: dict) -> str:
    """用哪一種 API 格式送：agnes／xai／gemini／openai（其他都當 OpenAI 相容）。看連線網址，不看模型名稱。"""
    base = str(m.get("base_url") or "").lower()
    if "agnes-ai.com" in base:
        return "agnes"
    if "api.x.ai" in base:
        return "xai"
    if "generativelanguage.googleapis.com" in base:
        return "gemini"
    return "openai"


_XAI_IMG_R = ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "19.5:9", "9:19.5", "20:9", "9:20", "21:9", "5:2"]
_GEM_IMG_R31 = ["1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"]
_GEM_IMG_R = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]


def spec(m: dict) -> dict:
    """這個生成模型能調的參數（官方文件）：ratios 比例、sizes 圖片解析度、resolutions 影片解析度、seconds 秒數範圍、
    default 不指定時用的值、note 給使用者看的說明。模型設定裡的 gen 會蓋掉 default。"""
    kind, fam = kind_of(m), family(m)
    name = str(m.get("model") or m["id"]).lower()
    if kind == "image":
        if fam == "xai":
            s = {"ratios": _XAI_IMG_R, "sizes": ["1k", "2k"], "default": {"ratio": "auto", "size": "1k"},
                 "note": "xAI Grok Imagine：比例 auto 由模型自己挑；解析度 1k／2k。參考圖（改圖）還沒接，只用文字生成。",
                 "doc": "https://docs.x.ai/developers/model-capabilities/images/generation"}
        elif fam == "gemini":
            r31 = "3.1-flash-image" in name
            s = {"ratios": _GEM_IMG_R31 if r31 else _GEM_IMG_R, "sizes": [] if "2.5-flash-image" in name else ["1K", "2K", "4K"],
                 "default": {"ratio": "1:1", "size": "1K"},
                 "note": "Gemini（Nano Banana，Interactions API）：比例、解析度 1K／2K／4K；參考圖最多 14 張。解析度越高越貴（1K 約 1,120 token、4K 約 2,520 token）。",
                 "doc": "https://ai.google.dev/gemini-api/docs/image-generation"}
        elif fam == "agnes":
            s = {"ratios": ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"], "sizes": ["1K", "2K"],
                 "default": {"ratio": "1:1", "size": "1K"}, "note": "Agnes：比例、解析度 1K／2K；參考圖最多 8 張。"}
        else:
            s = {"ratios": ["1:1", "3:2", "2:3"], "sizes": [], "default": {"ratio": "1:1"},
                 "note": "OpenAI 相容（/images/generations）：比例換成 1024x1024、1536x1024、1024x1536。"}
    elif kind == "video":
        if fam == "xai":
            s = {"ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2"], "resolutions": ["480p", "720p", "1080p"],
                 "seconds": [1, 15], "default": {"ratio": "16:9", "resolution": "720p", "seconds": 8},
                 "note": "xAI Grok Imagine Video：1–15 秒、480p／720p／1080p（越高越慢）。目前只接文字生影片。",
                 "doc": "https://docs.x.ai/developers/model-capabilities/video/generation"}
        elif fam == "gemini":
            s = {"ratios": ["16:9", "9:16"], "resolutions": ["360p", "720p", "1080p", "4k"], "seconds": [3, 15],
                 "default": {"ratio": "16:9", "resolution": "720p", "seconds": 10},
                 "note": "Gemini Omni Flash：3–15 秒、16:9／9:16、360p／720p，1080p 和 4k 是放大的；有原生聲音。（Veo 3.1 Preview 已在 2026/10/22 停用）",
                 "doc": "https://ai.google.dev/gemini-api/docs/omni"}
        elif fam == "agnes":
            s = {"ratios": ["16:9", "9:16", "1:1"], "resolutions": ["720P"], "seconds": [4, 12],
                 "default": {"ratio": "16:9", "resolution": "720P", "seconds": 5}, "note": "Agnes：4–12 秒、720P。"}
        else:
            s = {"ratios": ["16:9", "9:16"], "resolutions": [], "seconds": [4, 12], "default": {"ratio": "16:9", "seconds": 5},
                 "note": "OpenAI 相容（/videos）：16:9 送 1280x720、9:16 送 720x1280。"}
    else:
        return {}
    s = {"ratios": [], "sizes": [], "resolutions": [], "seconds": None, **s, "kind": kind, "family": fam}
    s["current"] = {**s["default"], **{k: v for k, v in (m.get("gen") or {}).items() if v not in (None, "")}}
    return s


ALL_RATIOS = sorted({r for l in (_XAI_IMG_R, _GEM_IMG_R31, ["5:2", "21:9"]) for r in l if r != "auto"}, key=len, reverse=True)


def _rv(r: str) -> float:
    try:
        a, b = r.split(":")
        return float(a) / float(b)
    except Exception:
        return 1.0


def settle(m: dict, **want) -> tuple[dict, list[str]]:
    """這次要用的參數：呼叫時給的 → 模型設定的 gen → 預設。不支援的換成最接近的，回傳（參數, 調整說明）。"""
    import math

    s = spec(m)
    cur, notes = dict(s.get("current") or {}), []
    for k, v in want.items():
        if v not in (None, "", 0):
            cur[k] = v
    r = str(cur.get("ratio") or "")
    if s["ratios"] and r and r not in s["ratios"]:
        ok = [x for x in s["ratios"] if x != "auto"]
        best = min(ok, key=lambda x: abs(math.log(_rv(x)) - math.log(_rv(r))))
        notes.append(f"{r} 不支援，改成 {best}")
        cur["ratio"] = best
    for k, allow in (("size", s["sizes"]), ("resolution", s["resolutions"])):
        v = str(cur.get(k) or "")
        if not v:
            continue
        if not allow:
            cur.pop(k, None)
            continue
        hit = next((x for x in allow if x.lower() == v.lower()), None)
        if not hit:
            hit = s["default"].get(k) or allow[0]
            notes.append(f"{v} 不支援，改成 {hit}")
        cur[k] = hit
    if s["seconds"]:
        lo, hi = s["seconds"]
        try:
            sec = int(float(cur.get("seconds") or s["default"].get("seconds") or lo))
        except (TypeError, ValueError):
            sec = int(s["default"].get("seconds") or lo)
        if not lo <= sec <= hi:
            notes.append(f"{sec} 秒超出 {lo}–{hi} 秒，改成 {max(lo, min(hi, sec))} 秒")
        cur["seconds"] = max(lo, min(hi, sec))
    return cur, notes


def _root(base: str) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(base)
    return f"{u.scheme}://{u.netloc}"


def _find(j, want: str) -> dict | None:
    """Gemini Interactions 回應裡最後一個圖片／影片區塊（steps → model_output → content，格式小改也找得到）。"""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == want and (x.get("data") or x.get("uri")):
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(j)
    return found[-1] if found else None


_EXT = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png", "video/mp4": ".mp4"}


async def _gemini(c: httpx.AsyncClient, m: dict, key: str, inputs: list, fmt: dict, want: str) -> tuple[bytes, str]:
    root = _root(str(m.get("base_url") or ""))
    hdr = {"x-goog-api-key": key}
    body = {"model": m.get("model") or m["id"], "input": inputs, "response_format": fmt, "store": False}
    r = await c.post(f"{root}/v1beta/interactions", json=body, headers=hdr)
    if r.status_code >= 400:
        raise RuntimeError(f"{m.get('label', m['id'])} 生成失敗（{_err(r)}）")
    blk = _find(r.json(), want)
    if not blk:
        raise RuntimeError(f"{m.get('label', m['id'])} 沒有回傳{KIND_LABEL[want]}：{str(r.json())[:300]}")
    ext = _EXT.get(str(blk.get("mime_type") or ""), ".mp4" if want == "video" else ".png")
    if blk.get("data"):
        return base64.b64decode(blk["data"]), ext
    fid = str(blk["uri"]).rstrip("/").split("/files/")[-1]
    for _ in range(200):  # 大影片給 URI：等檔案 ACTIVE 再下載
        f = await c.get(f"{root}/v1beta/files/{fid}", headers=hdr)
        st = str((f.json() if f.status_code < 400 else {}).get("state") or "")
        if st == "ACTIVE" or f.status_code >= 400:
            break
        if st == "FAILED":
            raise RuntimeError("Gemini 檔案處理失敗")
        await asyncio.sleep(3)
    g = await c.get(f"{root}/download/v1beta/files/{fid}:download", params={"alt": "media"}, headers=hdr, follow_redirects=True, timeout=300)
    g.raise_for_status()
    return g.content, ext


_OAI_SIZE = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}


async def gen_image(woof, m: dict, prompt: str, *, images: list[str] | None = None, size: str = "", ratio: str = "",
                    session_id: str = "") -> dict:
    """回 {"path", "seconds", "bytes", "params", "notes"}。images＝參考圖的本機路徑（圖生圖、多圖合成）。
    比例、解析度：呼叫時給的 → 模型設定的生成參數 → 預設；不支援的換成最接近的（notes）。"""
    base = str(m.get("base_url") or "").rstrip("/")
    fam = family(m)
    p, notes = settle(m, ratio=ratio, size=size if not re.fullmatch(r"\d+x\d+", size or "") else "")
    body: dict = {"model": m.get("model") or m["id"], "prompt": prompt}
    key = _key(woof, m)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=360) as c:
        if fam == "gemini":
            inputs = [{"type": "text", "text": prompt}]
            for ip in (images or [])[:14]:
                uri = _data_uri(ip)
                inputs.append({"type": "image", "mime_type": uri[5:uri.index(";")], "data": uri.split(",", 1)[1]})
            fmt = {"type": "image", "aspect_ratio": p.get("ratio") or "1:1"}
            if p.get("size"):
                fmt["image_size"] = p["size"]
            raw, ext = await _gemini(c, m, key, inputs, fmt, "image")
        else:
            if fam == "agnes":
                body["size"] = p.get("size") or "1K"
                if p.get("ratio"):
                    body["ratio"] = p["ratio"]
                extra: dict = {"response_format": "url"}
                if images:
                    extra["image"] = [_data_uri(x) for x in images[:8]]
                body["extra_body"] = extra
            elif fam == "xai":
                body.update(n=1, aspect_ratio=p.get("ratio") or "auto", resolution=p.get("size") or "1k", response_format="b64_json")
                if images:
                    notes.append("xAI 參考圖還沒接，這張只用文字生成")
            else:
                body["size"] = size if re.fullmatch(r"\d+x\d+", size or "") else _OAI_SIZE.get(p.get("ratio") or "1:1", "1024x1024")
                body["n"] = 1
                if images:
                    body["image"] = [_data_uri(x) for x in images[:8]]
            r = await c.post(f"{base}/images/generations", json=body, headers={"Authorization": f"Bearer {key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"{m.get('label', m['id'])} 生成圖片失敗（{_err(r)}）")
            d = (r.json().get("data") or [{}])[0]
            if d.get("b64_json"):
                raw, ext = base64.b64decode(d["b64_json"]), ".png"
            elif d.get("url"):
                g = await c.get(d["url"], follow_redirects=True)
                g.raise_for_status()
                raw = g.content
                ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(g.headers.get("content-type", "").split(";")[0], ".png")
            else:
                raise RuntimeError(f"{m.get('label', m['id'])} 沒有回傳圖片：{str(r.json())[:300]}")
    out = _out(ext, session_id)
    out.write_bytes(raw)
    return {"path": str(out), "seconds": round(time.time() - t0, 1), "bytes": len(raw), "params": p, "notes": notes}


async def gen_video(woof, m: dict, prompt: str, *, seconds: int | None = None, aspect: str = "", resolution: str = "",
                    progress=None, timeout: int = 900, session_id: str = "") -> dict:
    """非同步任務：建立 → 查進度 → 下載 mp4。progress(pct, status) 回報進度。
    秒數、比例、解析度：呼叫時給的 → 模型設定的生成參數 → 預設；超出範圍的換成最接近的（notes）。"""
    base = str(m.get("base_url") or "").rstrip("/")
    fam = family(m)
    key = _key(woof, m)
    name = m.get("model") or m["id"]
    hdr = {"Authorization": f"Bearer {key}"}
    p, notes = settle(m, ratio=aspect, seconds=seconds, resolution=resolution)
    sec, ratio = p.get("seconds") or 5, p.get("ratio") or "16:9"
    t0 = time.time()
    if fam == "gemini":  # Interactions API 同步回傳，等它做完
        if progress:
            await progress(0, f"Gemini 生成中（{sec} 秒、{ratio}、{p.get('resolution') or ''}）")
        fmt = {"type": "video", "aspect_ratio": ratio, "duration": f"{sec}s", "delivery": "uri"}
        if p.get("resolution"):
            fmt["resolution"] = p["resolution"]
        async with httpx.AsyncClient(timeout=timeout) as c:
            raw, _ = await _gemini(c, m, key, [{"type": "text", "text": prompt}], fmt, "video")
        out = _out(".mp4", session_id)
        out.write_bytes(raw)
        return {"path": str(out), "seconds": round(time.time() - t0, 1), "bytes": len(raw), "video_seconds": sec,
                "params": p, "notes": notes}
    if fam == "xai":
        url, body = f"{base}/videos/generations", {"model": name, "prompt": prompt, "duration": sec, "aspect_ratio": ratio,
                                                   "resolution": p.get("resolution") or "720p"}
    elif fam == "agnes":
        url, body = f"{base}/videos", {"model": name, "prompt": prompt, "seconds": str(sec), "mode": "text",
                                       "size": p.get("resolution") or "720P", "aspect_ratio": ratio}
    else:
        url, body = f"{base}/videos", {"model": name, "prompt": prompt, "seconds": str(sec),
                                       "size": "720x1280" if ratio == "9:16" else "1280x720"}
    async with httpx.AsyncClient(timeout=120) as c:
        # 服務商排隊滿（503 queue full）：等一下再送，最多約 2 分鐘
        for i in range(6):
            r = await c.post(url, json=body, headers=hdr)
            if r.status_code not in (429, 503) or i == 5:
                break
            if progress:
                await progress(0, f"服務商排隊滿，{10 + i * 5} 秒後重試（第 {i + 1} 次）")
            await asyncio.sleep(10 + i * 5)
        if r.status_code in (429, 503):
            raise RuntimeError(f"{m.get('label', m['id'])} 的影片佇列一直是滿的（服務商忙，重試 6 次約 2 分鐘），過幾分鐘再送一次")
        if r.status_code >= 400:
            raise RuntimeError(f"{m.get('label', m['id'])} 建立影片任務失敗（{_err(r)}）")
        j = r.json()
        vid = j.get("request_id") or j.get("video_id") or j.get("id") or j.get("task_id")
        if not vid:
            raise RuntimeError(f"{m.get('label', m['id'])} 沒回任務編號：{str(j)[:300]}")
        if fam == "agnes":
            root = base[: -len("/v1")] if base.endswith("/v1") else base
            poll, params = f"{root}/agnesapi", {"video_id": vid, "model_name": name}
        else:
            poll, params = f"{base}/videos/{vid}", None
        last = None
        while True:
            if time.time() - t0 > timeout:
                raise RuntimeError(f"影片生成超過 {timeout // 60} 分鐘還沒好（任務 {vid}），之後可再查")
            await asyncio.sleep(2)
            q = await c.get(poll, params=params, headers=hdr)
            if q.status_code >= 400:
                raise RuntimeError(f"查詢影片任務失敗（{_err(q)}）")
            s = q.json()
            st = str(s.get("status") or "")
            pct = int(s.get("progress") or 0)
            if progress and (st, pct) != last:
                last = (st, pct)
                await progress(pct, st)
            if st in ("failed", "expired"):
                raise RuntimeError(f"影片生成失敗：{str(s.get('error') or s)[:300]}")
            if st in ("completed", "done"):
                v = s.get("video") if isinstance(s.get("video"), dict) else {}
                url2 = v.get("url") or (s.get("metadata") or {}).get("url") or s.get("url") or s.get("video_url")
                if url2:
                    g = await c.get(url2, follow_redirects=True, timeout=300)
                else:
                    g = await c.get(f"{base}/videos/{vid}/content", headers=hdr, follow_redirects=True, timeout=300)
                g.raise_for_status()
                out = _out(".mp4", session_id)
                out.write_bytes(g.content)
                return {"path": str(out), "seconds": round(time.time() - t0, 1), "bytes": len(g.content),
                        "video_seconds": v.get("duration") or s.get("seconds") or sec, "id": vid, "params": p, "notes": notes}


def last_request(messages: list[dict]) -> tuple[str, list[str]]:
    """生成型模型當對話模型時：最後一則使用者訊息的文字（提示詞）和附圖（參考圖）。"""
    for m in reversed(messages):
        if m["role"] != "user" or (m.get("meta") or {}).get("internal"):
            continue
        text = "\n".join(b.get("text") or "" for b in m["content"] if b.get("type") == "text").strip()
        imgs = [b["path"] for b in m["content"] if b.get("type") == "image" and b.get("path")]
        return text, imgs
    return "", []


async def chat_turn(woof, m: dict, messages: list[dict], kind: str):
    """對話模型本身是生成型：產生 provider 事件（text_delta 進度、最後一則 message）。"""
    prompt, imgs = last_request(messages)
    label = m.get("label", m["id"])
    if not prompt:
        raise RuntimeError(f"{label} 是{KIND_LABEL[kind]}模型：直接打要生成的內容描述（可以附參考圖）")
    if kind == "image":
        yield {"type": "text_delta", "text": f"{label} 生成圖片中（通常 5–30 秒）…"}
        r = await gen_image(woof, m, prompt, images=imgs)
        text = f"已生成圖片，花了 {r['seconds']} 秒。\nMEDIA:{r['path']}"
        if imgs:
            text = f"已依 {len(imgs)} 張參考圖生成，花了 {r['seconds']} 秒。\nMEDIA:{r['path']}"
    else:
        q: asyncio.Queue = asyncio.Queue()

        async def prog(pct, st):
            await q.put(f"\n進度 {pct}%（{st}）")

        yield {"type": "text_delta", "text": f"{label} 生成影片中（通常 1–3 分鐘）…"}
        if imgs:
            yield {"type": "text_delta", "text": "\n（附圖沒有用上：影片參考圖要公開網址，目前只用文字生成）"}
        task = asyncio.create_task(gen_video(woof, m, prompt, progress=prog))
        while not task.done():
            try:
                yield {"type": "text_delta", "text": await asyncio.wait_for(q.get(), 1.0)}
            except asyncio.TimeoutError:
                pass
        r = task.result()
        text = f"已生成影片，{r['video_seconds']} 秒長，花了 {r['seconds']} 秒。\nMEDIA:{r['path']}"
    yield {"type": "message", "content": [{"type": "text", "text": text}], "meta": {"media": kind}}
