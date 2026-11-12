"""看圖：本機圖片或網址圖片，可指定裁切區域。

使用者貼上的圖由模型直接看；這個工具讓 agent 自己讀取圖片。
圖片先裁切（若有 region）再縮小，描述由支援看圖的模型產生。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
from PIL import Image, ImageOps

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_DOWNLOAD = 15 * 1024 * 1024  # 網址圖片上限 15 MB


def _shrink(im: Image.Image, max_px: int, region: tuple[int, int, int, int] | None) -> tuple[str, str]:
    if region:
        im = im.crop(region)
    im.thumbnail((max_px, max_px))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "image/png", base64.b64encode(buf.getvalue()).decode()


def _parse_region(raw, w: int, h: int) -> tuple[int, int, int, int] | None:
    """region = [x1, y1, x2, y2]（原圖像素，左上為 0,0）。不合法拋 ValueError。"""
    if raw is None:
        return None
    if not (isinstance(raw, (list, tuple)) and len(raw) == 4):
        raise ValueError("region 要是 [x1, y1, x2, y2] 四個整數")
    x1, y1, x2, y2 = (int(v) for v in raw)
    if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
        raise ValueError(f"region 超出範圍：圖片是 {w}×{h}，要 0≤x1<x2≤{w} 且 0≤y1<y2≤{h}")
    return x1, y1, x2, y2


async def _load_url(url: str) -> Image.Image:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cl:
        async with cl.stream("GET", url, headers={"User-Agent": "WOOF-vision/0.1"}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"下載失敗：HTTP {r.status_code}")
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                raise RuntimeError(f"網址不是圖片（content-type={ctype or '未知'}）")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_DOWNLOAD:
                    raise RuntimeError("圖片超過 15 MB，不下載")
    return Image.open(io.BytesIO(bytes(buf)))


def register(woof):
    async def vision_analyze(args: dict, ctx) -> dict:
        raw = (args.get("path") or "").strip()
        url = (args.get("url") or "").strip()
        if not raw and not url:
            return {"output": "path 或 url 要給一個", "is_error": True}
        if raw and url:
            return {"output": "path 與 url 只能給一個", "is_error": True}
        max_px = max(256, int(woof.settings.get("max_px") or 1568))
        try:
            if url:
                if not url.lower().startswith(("http://", "https://")):
                    return {"output": "url 只接受 http:// 或 https://", "is_error": True}
                im = await _load_url(url)
                src = url
            else:
                path = Path(raw)
                if not path.is_file():
                    return {"output": f"找不到圖片：{path}", "is_error": True}
                if path.suffix.lower() not in _IMAGE_EXTS:
                    return {"output": f"不支援的格式：{path.suffix}", "is_error": True}
                im = Image.open(path)
                src = str(path)
            im = ImageOps.exif_transpose(im)
            region = _parse_region(args.get("region"), im.width, im.height)
            media, data = _shrink(im, max_px, region)
        except ValueError as e:
            return {"output": str(e), "is_error": True}
        except Exception as e:
            return {"output": f"無法讀取圖片：{type(e).__name__}: {e}", "is_error": True}
        question = (args.get("question") or "描述這張圖片的內容，包含文字、版面和明顯的問題。").strip()

        from woof.agent.loop import complete
        from woof.config import DATA

        turn = getattr(ctx, "turn", None)
        from woof.agent.loop import vision_chain
        chain = vision_chain(turn.model_cfg if turn is not None else None)
        want = (woof.settings.get("model") or "").strip()
        if want:
            chain = [m for m in turn.registry.config.get("models") or [] if m.get("id") == want] + chain if turn else chain
        if not chain:
            return {"output": "沒有可用的看圖模型：在「設定 → 輔助模型 → 看圖」選一個能看圖的模型", "is_error": True}
        tmp = DATA / "tmp"
        tmp.mkdir(exist_ok=True)
        shot = tmp / f"vision_{ctx.call_id}.png"
        shot.write_bytes(base64.b64decode(data))
        from woof.prompts import get as _prompt
        errs, text, model = [], None, None
        try:
            for model in chain[:3]:
                try:
                    text = await complete(model, _prompt("vision"), [{"role": "user", "content": [
                        {"type": "image", "path": str(shot), "media_type": media},
                        {"type": "text", "text": question},
                    ]}])
                    break
                except Exception as e:  # 這個看圖模型不行（暫停、不收圖、限流）：換下一個
                    errs.append(f"{model.get('label') or model['id']}：{str(e)[:200]}")
                    model = None
        finally:
            shot.unlink(missing_ok=True)
        if model is None:
            return {"output": "看圖失敗：\n" + "\n".join(errs), "is_error": True}
        who = model.get("label") or model["id"]
        note = f"（來源：{src}{'，裁切 ' + str(list(region)) if region else ''}）"
        tried = f"（{'；'.join(errs)}，改用 {who}）" if errs else ""
        return {"output": f"（{who}）{note}{tried}\n{text or '（模型沒有回覆）'}"}

    woof.add_tool(
        "vision_analyze",
        "看一張圖片並回答問題。可給本機路徑（path）或網址（url，http/https）；region 可裁切原圖的 [x1,y1,x2,y2]。使用者直接貼上的圖不用這個工具。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "本機圖片檔的絕對路徑（與 url 二選一）"},
            "url": {"type": "string", "description": "圖片網址，http 或 https（與 path 二選一）"},
            "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                       "description": "裁切區域 [x1, y1, x2, y2]，單位是原圖像素，左上角為 0,0；省略則看整張"},
            "question": {"type": "string", "description": "要問的問題；空白則描述整張圖"},
        }},
        vision_analyze,
    )
