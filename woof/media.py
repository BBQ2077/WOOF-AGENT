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
_VID = re.compile(r"(^|[-_/.:])(video|veo|kling|sora|seedance|hailuo|wan2|cogvideo)([-_/.:\d]|$)", re.I)
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


def _out(ext: str) -> Path:
    d = MEDIA / datetime.now().strftime("%Y%m%d")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"


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


async def gen_image(woof, m: dict, prompt: str, *, images: list[str] | None = None, size: str = "", ratio: str = "") -> dict:
    """回 {"path", "size", "seconds"}。images＝參考圖的本機路徑（圖生圖、多圖合成）。"""
    base = str(m.get("base_url") or "").rstrip("/")
    body: dict = {"model": m.get("model") or m["id"], "prompt": prompt}
    if _agnes(m):
        body["size"] = size or "1K"
        if ratio:
            body["ratio"] = ratio
        extra: dict = {"response_format": "url"}
        if images:
            extra["image"] = [_data_uri(p) for p in images[:8]]
        body["extra_body"] = extra
    else:
        body["size"] = size or "1024x1024"
        body["n"] = 1
        if images:
            body["image"] = [_data_uri(p) for p in images[:8]]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=360) as c:
        r = await c.post(f"{base}/images/generations", json=body, headers={"Authorization": f"Bearer {_key(woof, m)}"})
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
    out = _out(ext)
    out.write_bytes(raw)
    return {"path": str(out), "seconds": round(time.time() - t0, 1), "bytes": len(raw)}


async def gen_video(woof, m: dict, prompt: str, *, seconds: int = 5, aspect: str = "16:9", progress=None,
                    timeout: int = 900) -> dict:
    """非同步任務：建立 → 每 3 秒查一次 → 下載 mp4。progress(pct, status) 用來回報進度。"""
    base = str(m.get("base_url") or "").rstrip("/")
    key = _key(woof, m)
    name = m.get("model") or m["id"]
    hdr = {"Authorization": f"Bearer {key}"}
    sec = max(4, min(12, int(seconds or 5)))
    body: dict = {"model": name, "prompt": prompt, "seconds": str(sec)}
    if _agnes(m):
        body.update(mode="text", size="720P", aspect_ratio=aspect or "16:9")
    else:
        body["size"] = "1280x720" if (aspect or "16:9") == "16:9" else "720x1280"
    t0 = time.time()
    async with httpx.AsyncClient(timeout=120) as c:
        # 服務商排隊滿（503 queue full）：等一下再送，最多約 2 分鐘
        for i in range(6):
            r = await c.post(f"{base}/videos", json=body, headers=hdr)
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
        vid = j.get("video_id") or j.get("id") or j.get("task_id")
        if not vid:
            raise RuntimeError(f"{m.get('label', m['id'])} 沒回任務編號：{str(j)[:300]}")
        if _agnes(m):
            root = base[: -len("/v1")] if base.endswith("/v1") else base
            poll = f"{root}/agnesapi"
            params = {"video_id": vid, "model_name": name}
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
            if st == "failed":
                raise RuntimeError(f"影片生成失敗：{str(s.get('error') or s)[:300]}")
            if st == "completed":
                url = (s.get("metadata") or {}).get("url") or s.get("url") or s.get("video_url")
                if url:
                    g = await c.get(url, follow_redirects=True, timeout=300)
                else:
                    g = await c.get(f"{base}/videos/{vid}/content", headers=hdr, follow_redirects=True, timeout=300)
                g.raise_for_status()
                out = _out(".mp4")
                out.write_bytes(g.content)
                return {"path": str(out), "seconds": round(time.time() - t0, 1), "bytes": len(g.content),
                        "video_seconds": s.get("seconds") or sec, "id": vid}


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
