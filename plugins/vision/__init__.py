"""看圖：本機圖片或網址圖片，可指定裁切區域；網頁、SVG 截圖（view_page）。

使用者貼上的圖由模型直接看；這兩個工具讓 agent 自己看圖、看自己做的頁面。
圖片先裁切（若有 region）再縮小；主模型能看圖就直接放進它的上下文，不能看就交給看圖模型轉成文字。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageOps

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_DOWNLOAD = 15 * 1024 * 1024  # 網址圖片上限 15 MB


def _shrink(im: Image.Image, max_px: int, region: tuple[int, int, int, int] | None) -> tuple[str, str]:
    if region:
        im = im.crop(region)
    im.thumbnail((max_px, max_px))
    _shrink.last = im.size
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


def _native_model(woof, turn_model: dict | None) -> dict | None:
    """這個回合的模型能不能自己看工具給的圖。「模型用途 → 看圖」有指定模型時照指定的另外看（明確指定優先）。"""
    from woof import caps
    from woof.loader import REG

    if turn_model is None or (REG.config.get("aux") or {}).get("vision"):
        return None
    cfg = REG.model_cfg(turn_model["id"]) or turn_model
    if not cfg.get("vision") or caps.down(cfg["id"]) or cfg.get("provider") not in ("openai_compat", "anthropic"):
        return None
    return cfg


# ── view_page：無頭瀏覽器開頁面，截圖直接看 ─────────────────────────────

def _find_browser(setting: str) -> str | None:
    if setting and Path(setting).is_file():
        return setting
    la = os.environ.get("LOCALAPPDATA", "")
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.join(la, r"Google\Chrome\Application\chrome.exe") if la else "",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if p and Path(p).is_file():
            return p
    for n in ("chrome", "google-chrome", "chromium", "msedge"):
        if shutil.which(n):
            return shutil.which(n)
    return None


class _CDP:
    """最小的 Chrome DevTools 連線：送指令等回應，事件收進清單。"""

    def __init__(self, ws):
        self.ws, self.n, self.pending, self.events, self.waiters = ws, 0, {}, [], []

    async def pump(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m:
                f = self.pending.pop(m["id"], None)
                if f and not f.done():
                    f.set_result(m)
                continue
            self.events.append(m)
            for meth, f in list(self.waiters):
                if m.get("method") == meth and not f.done():
                    f.set_result(m)

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self.n += 1
        f = asyncio.get_running_loop().create_future()
        self.pending[self.n] = f
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        r = await asyncio.wait_for(f, timeout)
        if "error" in r:
            raise RuntimeError(f"{method}：{r['error'].get('message')}")
        return r.get("result") or {}

    def expect(self, method: str):
        f = asyncio.get_running_loop().create_future()
        self.waiters.append((method, f))
        return f


def _page_errors(events: list[dict]) -> list[str]:
    out: list[str] = []
    for e in events:
        m, p = e.get("method"), e.get("params") or {}
        line = ""
        if m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            line = "例外：" + str((d.get("exception") or {}).get("description") or d.get("text") or "")
        elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert", "warning"):
            line = f"console.{p['type']}：" + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
        elif m == "Log.entryAdded" and (p.get("entry") or {}).get("level") in ("error", "warning"):
            en = p["entry"]
            line = f"{en.get('source', '')}：{en.get('text', '')}" + (f"（{en['url']}）" if en.get("url") else "")
        line = line.strip()[:400]
        if line and line not in out:
            out.append(line)
    return out


def _kill_tree(proc: subprocess.Popen) -> None:
    # 只殺外層會留下 renderer／GPU 子程序：整棵樹一起結束
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.wait(5)
        except Exception:
            proc.kill()


async def _shoot_page(exe: str, url: str, shots: list[dict], first_wait: int, max_px: int) -> tuple[list[dict], list[str]]:
    """開頁面依序截每一張；回傳 [{png(b64), w, h, sw, sh, desc, js}…] 和頁面錯誤。"""
    import httpx
    from websockets.asyncio.client import connect

    udd = tempfile.mkdtemp(prefix="woof_page_")
    proc = subprocess.Popen(
        [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={udd}", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars", "--mute-audio",
         "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--allow-file-access-from-files", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pump = None
    try:
        port_file = Path(udd) / "DevToolsActivePort"
        t0 = time.time()
        while not (port_file.is_file() and port_file.read_text(errors="ignore").strip()):
            if proc.poll() is not None:
                raise RuntimeError(f"瀏覽器啟動後馬上結束（{exe}）")
            if time.time() - t0 > 20:
                raise RuntimeError("瀏覽器 20 秒內沒啟動完成")
            await asyncio.sleep(0.1)
        port = port_file.read_text().split()[0]
        async with httpx.AsyncClient(timeout=10) as cl:
            pages = (await cl.get(f"http://127.0.0.1:{port}/json/list")).json()
        page = next(p for p in pages if p.get("type") == "page")
        async with connect(page["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            cdp = _CDP(ws)
            pump = asyncio.create_task(cdp.pump())
            for m in ("Page.enable", "Runtime.enable", "Log.enable"):
                await cdp.call(m)
            vw, vh = shots[0]["width"], shots[0]["height"]
            await cdp.call("Emulation.setDeviceMetricsOverride", {"width": vw, "height": vh, "deviceScaleFactor": 1, "mobile": False})
            loaded = cdp.expect("Page.loadEventFired")
            nav = await cdp.call("Page.navigate", {"url": url})
            if nav.get("errorText"):
                raise RuntimeError(f"頁面打不開：{nav['errorText']}")
            notes: list[str] = []
            try:
                await asyncio.wait_for(loaded, 30)
            except asyncio.TimeoutError:
                notes.append("頁面 30 秒內沒有載完，照當下的畫面截")
            await asyncio.sleep(first_wait / 1000)
            out = []
            for s in shots:
                if (s["width"], s["height"]) != (vw, vh):
                    vw, vh = s["width"], s["height"]
                    await cdp.call("Emulation.setDeviceMetricsOverride", {"width": vw, "height": vh, "deviceScaleFactor": 1, "mobile": False})
                js_out = None
                if s.get("js"):
                    r = await cdp.call("Runtime.evaluate", {"expression": s["js"], "awaitPromise": True, "returnByValue": True}, timeout=60)
                    if r.get("exceptionDetails"):
                        d = r["exceptionDetails"]
                        js_out = "js 出錯：" + str((d.get("exception") or {}).get("description") or d.get("text"))[:600]
                    else:
                        v = (r.get("result") or {}).get("value")
                        js_out = None if v is None else json.dumps(v, ensure_ascii=False)[:1500]
                if s.get("wait_ms"):
                    await asyncio.sleep(s["wait_ms"] / 1000)
                scale = s.get("scale") or 1
                clip, desc = None, f"視窗 {vw}×{vh}"
                if s.get("selector"):
                    r = await cdp.call("Runtime.evaluate", {"returnByValue": True, "expression":
                        "(()=>{const e=document.querySelector(" + json.dumps(s["selector"]) + ");if(!e)return null;"
                        "const r=e.getBoundingClientRect();return [r.left+scrollX,r.top+scrollY,r.width,r.height]})()"})
                    box = (r.get("result") or {}).get("value")
                    if not box or box[2] < 1 or box[3] < 1:
                        out.append({"error": f"找不到元素或大小是 0：{s['selector']}", "desc": desc, "js": js_out})
                        continue
                    clip = {"x": box[0], "y": box[1], "width": box[2], "height": box[3]}
                    desc += f"，元素 {s['selector']}（頁面座標 {[round(v) for v in box]}，x, y, 寬, 高）"
                elif s.get("region"):
                    x1, y1, x2, y2 = s["region"]
                    clip = {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}
                    desc += f"，區塊 {s['region']}（頁面座標）"
                elif s.get("full_page"):
                    lm = await cdp.call("Page.getLayoutMetrics")
                    cs = lm.get("cssContentSize") or lm.get("contentSize") or {}
                    clip = {"x": 0, "y": 0, "width": max(1, cs.get("width", vw)), "height": min(16384, max(1, cs.get("height", vh)))}
                    desc += f"，整頁 {round(clip['width'])}×{round(clip['height'])}"
                elif scale != 1:
                    clip = {"x": 0, "y": 0, "width": vw, "height": vh}
                params = {"format": "png", "captureBeyondViewport": clip is not None}
                if clip:
                    params["clip"] = {**clip, "scale": scale}
                if scale != 1:
                    desc += f"，放大 {scale}×"
                data = (await cdp.call("Page.captureScreenshot", params, timeout=90))["data"]
                im = Image.open(io.BytesIO(base64.b64decode(data)))
                ow, oh = im.size
                media, b64 = _shrink(im, max_px, None)
                sw, sh = _shrink.last
                out.append({"media": media, "png": b64, "w": ow, "h": oh, "sw": sw, "sh": sh, "desc": desc, "js": js_out})
            return out, notes + _page_errors(cdp.events)
    finally:
        if pump:
            pump.cancel()
        await asyncio.to_thread(_kill_tree, proc)
        for _ in range(5):
            shutil.rmtree(udd, ignore_errors=True)
            if not Path(udd).exists():
                break
            await asyncio.sleep(0.4)


def _norm_shots(raw, limit: int) -> list[dict]:
    """shots 參數整理成固定欄位；不合法拋 ValueError。"""
    raw = raw if isinstance(raw, list) and raw else [{}]
    if len(raw) > limit:
        raise ValueError(f"一次最多 {limit} 張，這次給了 {len(raw)} 張")
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ValueError(f"第 {i} 張的設定要是物件")
        n = {"label": str(s.get("label") or ""), "js": str(s.get("js") or ""), "selector": str(s.get("selector") or ""),
             "full_page": bool(s.get("full_page")),
             "width": max(200, min(3840, int(s.get("width") or 1280))),
             "height": max(200, min(2400, int(s.get("height") or 800))),
             "wait_ms": max(0, min(30000, int(s.get("wait_ms") or 0))),
             "scale": max(0.25, min(4.0, float(s.get("scale") or 1)))}
        if s.get("region") is not None:
            r = s["region"]
            if not (isinstance(r, (list, tuple)) and len(r) == 4):
                raise ValueError(f"第 {i} 張的 region 要是 [x1, y1, x2, y2]")
            x1, y1, x2, y2 = (float(v) for v in r)
            if not (0 <= x1 < x2 and 0 <= y1 < y2):
                raise ValueError(f"第 {i} 張的 region 要 0≤x1<x2 且 0≤y1<y2")
            n["region"] = [x1, y1, x2, y2]
        out.append(n)
    return out


_VIS_HINT = "\n看圖用「設定 → 模型用途 → 看圖」指定的模型，沒指定就用主要模型；要換請到那裡改"


def register(woof):
    async def _prep(item: dict, max_px: int, cwd: str | None) -> dict:
        """讀一張圖、裁切縮小；回傳 media、data（b64）、來源與尺寸說明。錯誤拋 ValueError。"""
        raw = str(item.get("path") or "").strip()
        url = str(item.get("url") or "").strip()
        if bool(raw) == bool(url):
            raise ValueError("path 與 url 要給一個（只能一個）")
        try:
            if url:
                if not url.lower().startswith(("http://", "https://")):
                    raise ValueError("url 只接受 http:// 或 https://")
                im, src = await _load_url(url), url
            else:
                path = Path(raw).expanduser()
                if not path.is_absolute():
                    path = Path(cwd or ".") / path
                if not path.is_file():
                    raise ValueError(f"找不到圖片：{path}")
                if path.suffix.lower() not in _IMAGE_EXTS:
                    raise ValueError(f"不支援的格式：{path.suffix}")
                im, src = Image.open(path), str(path)
            im = ImageOps.exif_transpose(im)
            region = _parse_region(item.get("region"), im.width, im.height)
            ow, oh = im.width, im.height
            media, data = _shrink(im, max_px, region)
            sw, sh = _shrink.last
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"無法讀取圖片：{type(e).__name__}: {e}")
        note = f"{src}{'，裁切 ' + str(list(region)) if region else ''}"
        # 告訴模型尺寸與座標怎麼換算：不知道原圖多大、不知道裁切保留原解析度，模型就不會放大看細節
        if region:
            cw, ch = region[2] - region[0], region[3] - region[1]
            size = (f"原圖 {ow}×{oh}；這是原圖 {list(region)} 這塊（{cw}×{ch}）"
                    + (f"，以原解析度給你看" if (sw, sh) == (cw, ch) else f"，縮成 {sw}×{sh} 給你看")
                    + ("。你看到的座標" + ("" if (sw, sh) == (cw, ch) else f"乘 {cw / sw:.2f} 再")
                       + f"加上 ({region[0]}, {region[1]}) 就是原圖座標。"))
        else:
            size = (f"原圖 {ow}×{oh}" + ("，原尺寸給你看。" if (sw, sh) == (ow, oh)
                    else f"，縮成 {sw}×{sh} 給你看（座標乘 {ow / sw:.2f} 換回原圖）。細節看不清就用 region 裁切放大，裁切保留原解析度。"))
        return {"media": media, "data": data, "note": note, "size": size}

    async def vision_analyze(args: dict, ctx) -> dict:
        # 一次看好幾張（images）：同一張圖的整張和放大、改前改後、大圖和縮圖，一個回合看完
        items = args.get("images")
        if items is not None:
            if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
                return {"output": "images 要是物件清單：[{path 或 url, region}…]", "is_error": True}
            if args.get("path") or args.get("url"):
                return {"output": "images 和 path／url 只能用一種", "is_error": True}
            limit = max(1, int(woof.settings.get("max_shots") or 6))
            if len(items) > limit:
                return {"output": f"一次最多 {limit} 張，這次給了 {len(items)} 張", "is_error": True}
        else:
            items = [{"path": args.get("path"), "url": args.get("url"), "region": args.get("region")}]
        max_px = max(256, min(int(woof.settings.get("max_px") or 1568), 2576))
        cwd = getattr(ctx, "cwd", None)
        pics, fails = [], []
        for k, it in enumerate(items, 1):
            try:
                pics.append((k, await _prep(it, max_px, cwd)))
            except ValueError as e:
                fails.append(f"第 {k} 張：{e}" if len(items) > 1 else str(e))
        if not pics:
            return {"output": "\n".join(fails), "is_error": True}
        multi = len(items) > 1
        info = "\n".join((f"第 {k} 張（{p['note']}）" if multi else f"（來源：{p['note']}）") + p["size"] for k, p in pics)
        if fails:
            info += "\n" + "\n".join(fails)
        question = (args.get("question") or ("描述這幾張圖片的內容，包含文字、版面和明顯的問題。" if multi
                                               else "描述這張圖片的內容，包含文字、版面和明顯的問題。")).strip()

        from woof.agent.loop import complete
        from woof.config import DATA

        turn = getattr(ctx, "turn", None)
        tm = _native_model(woof, turn.model_cfg if turn is not None else None)
        if tm is not None:
            # 主模型自己能看圖：圖直接放進它的上下文，不另外叫模型轉述（少一次請求，看的人也知道前因後果）
            from woof.uploads import DIR
            DIR.mkdir(parents=True, exist_ok=True)
            imgs = []
            for _, p in pics:
                pic = DIR / f"{uuid.uuid4().hex[:12]}.png"
                pic.write_bytes(base64.b64decode(p["data"]))
                imgs.append({"type": "image", "path": str(pic), "media_type": p["media"]})
            head = f"{len(imgs)} 張圖已依序放進你的上下文。" if multi else "圖片已放進你的上下文。"
            return {"output": f"{head}\n{info}\n直接用你自己的視覺看圖回答：{question}", "images": imgs}
        from woof.agent.loop import vision_chain
        chain = vision_chain(turn.model_cfg if turn is not None else None)
        if not chain:
            return {"output": "沒有可用的看圖模型：在「設定 → 模型用途 → 看圖」選一個能看圖的模型", "is_error": True}
        tmp = DATA / "tmp"
        tmp.mkdir(exist_ok=True)
        files = []
        for k, p in pics:
            f = tmp / f"vision_{ctx.call_id}_{k}.png"
            f.write_bytes(base64.b64decode(p["data"]))
            files.append((f, p["media"]))
        from woof.prompts import get as _prompt
        errs, text, model = [], None, None
        try:
            for model in chain[:3]:
                try:
                    text = await complete(model, _prompt("vision"), [{"role": "user", "content": [
                        *[{"type": "image", "path": str(f), "media_type": m} for f, m in files],
                        {"type": "text", "text": f"{info}\n\n{question}" if multi else question},
                    ]}])
                    break
                except Exception as e:  # 這個看圖模型不行（暫停、不收圖、限流）：換下一個
                    errs.append(f"{model.get('label') or model['id']}：{str(e)[:200]}")
                    model = None
        finally:
            for f, _ in files:
                f.unlink(missing_ok=True)
        if model is None:
            return {"output": "看圖失敗：\n" + "\n".join(errs) + _VIS_HINT, "is_error": True}
        who = model.get("label") or model["id"]
        tried = f"（{'；'.join(errs)}，改用 {who}）" if errs else ""
        return {"output": f"（{who}）{tried}\n{info}\n{text or '（模型沒有回覆）'}"}

    async def view_page(args: dict, ctx) -> dict:
        raw = (args.get("path") or "").strip()
        url = (args.get("url") or "").strip()
        if bool(raw) == bool(url):
            return {"output": "path 與 url 要給一個（只能一個）", "is_error": True}
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path(ctx.cwd or ".") / p
            if not p.is_file():
                return {"output": f"找不到檔案：{p}", "is_error": True}
            url, src = p.resolve().as_uri(), str(p)
        else:
            if not url.lower().startswith(("http://", "https://", "file:///")):
                return {"output": "url 只接受 http://、https:// 或 file:///", "is_error": True}
            src = url
        exe = _find_browser(str(woof.settings.get("browser") or "").strip())
        if not exe:
            return {"output": "找不到 Chrome 或 Edge：在「設定 → 插件 → vision」填瀏覽器路徑", "is_error": True}
        try:
            shots = _norm_shots(args.get("shots"), max(1, int(woof.settings.get("max_shots") or 6)))
        except (TypeError, ValueError) as e:
            return {"output": str(e), "is_error": True}
        max_px = max(256, min(int(woof.settings.get("max_px") or 1568), 2576))
        first_wait = max(0, min(30000, int(args.get("wait_ms") if args.get("wait_ms") is not None else 1000)))
        try:
            res, errs = await asyncio.wait_for(_shoot_page(exe, url, shots, first_wait, max_px), 300)
        except asyncio.TimeoutError:
            return {"output": "截圖超過 300 秒沒完成，已關掉瀏覽器", "is_error": True}
        except Exception as e:
            return {"output": f"截圖失敗：{type(e).__name__}: {e}", "is_error": True}
        question = (args.get("question") or "看這幾張截圖，描述畫面並指出明顯的問題（錯位、重疊、縫隙、破圖、文字被切掉）。").strip()
        lines, pics = [], []
        for i, (r, s) in enumerate(zip(res, shots), 1):
            head = f"第 {i} 張" + (f"「{s['label']}」" if s["label"] else "")
            if r.get("error"):
                lines.append(f"{head}：{r['error']}" + (f"；js 回傳 {r['js']}" if r.get("js") else ""))
                continue
            size = f"截圖 {r['w']}×{r['h']}" + ("" if (r["sw"], r["sh"]) == (r["w"], r["h"])
                                               else f"，縮成 {r['sw']}×{r['sh']} 給你看（座標乘 {r['w'] / r['sw']:.2f} 換回截圖）")
            lines.append(f"{head}：{r['desc']}；{size}" + (f"；js 回傳 {r['js']}" if r.get("js") else ""))
            pics.append(r)
        if errs:
            lines.append(f"頁面錯誤（{len(errs)}）：\n" + "\n".join(errs[:20]))
        info = f"（來源：{src}）\n" + "\n".join(lines)
        if not pics:
            return {"output": info, "is_error": True}
        turn = getattr(ctx, "turn", None)
        tm = _native_model(woof, turn.model_cfg if turn is not None else None)
        if tm is not None:
            from woof.uploads import DIR
            DIR.mkdir(parents=True, exist_ok=True)
            imgs = []
            for r in pics:
                pic = DIR / f"{uuid.uuid4().hex[:12]}.png"
                pic.write_bytes(base64.b64decode(r["png"]))
                imgs.append({"type": "image", "path": str(pic), "media_type": r["media"]})
            return {"output": f"{len(imgs)} 張截圖已依序放進你的上下文。{info}\n細節看不清就用 region 加 scale 放大再截。直接用你自己的視覺看圖回答：{question}",
                    "images": imgs}
        from woof.agent.loop import complete, vision_chain
        from woof.config import DATA
        from woof.prompts import get as _prompt
        chain = vision_chain(turn.model_cfg if turn is not None else None)
        if not chain:
            return {"output": info + "\n沒有可用的看圖模型：在「設定 → 模型用途 → 看圖」選一個能看圖的模型", "is_error": True}
        tmp = DATA / "tmp"
        tmp.mkdir(exist_ok=True)
        files = []
        for k, r in enumerate(pics):
            f = tmp / f"page_{ctx.call_id}_{k}.png"
            f.write_bytes(base64.b64decode(r["png"]))
            files.append(f)
        errs2, text, model = [], None, None
        try:
            for model in chain[:3]:
                try:
                    text = await complete(model, _prompt("vision"), [{"role": "user", "content": [
                        *[{"type": "image", "path": str(f), "media_type": "image/png"} for f in files],
                        {"type": "text", "text": f"{info}\n\n{question}"}]}])
                    break
                except Exception as e:
                    errs2.append(f"{model.get('label') or model['id']}：{str(e)[:200]}")
                    model = None
        finally:
            for f in files:
                f.unlink(missing_ok=True)
        if model is None:
            return {"output": info + "\n看圖失敗：\n" + "\n".join(errs2) + _VIS_HINT, "is_error": True}
        return {"output": f"（{model.get('label') or model['id']}）{info}\n{text or '（模型沒有回覆）'}"}

    woof.add_tool(
        "vision_analyze",
        "看圖片（本機 path 或 url）。多張（改前改後、整張和局部）用 images 一次給齊。整張會縮小，小字、對齊、錯位容易漏：先看整張，"
        "再用 region 裁切放大。網頁、HTML、SVG 用 view_page；使用者直接貼的圖不用這個工具。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "本機圖片絕對路徑（或用 url）"},
            "url": {"type": "string", "description": "圖片網址"},
            "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                       "description": "裁切 [x1, y1, x2, y2]，原圖像素；省略＝整張"},
            "images": {"type": "array", "description": "多張，取代 path／url",
                       "items": {"type": "object", "properties": {
                           "path": {"type": "string"}, "url": {"type": "string"},
                           "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}}}},
            "question": {"type": "string", "description": "要看什麼；空白＝描述圖片"},
        }},
        vision_analyze,
    )

    woof.add_tool(
        "view_page",
        "開網頁或本機 HTML／SVG 截圖直接給你看，不用自己跑指令截圖。shots 一次截多張（不同寬度、整頁、某元素、某區域）；每張前可跑 j"
        "s（捲動、點擊、讀尺寸），效果延續到下一張，回傳值也給你。console 錯誤和載入失敗會一起回報。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "本機 HTML、SVG 或圖片（或用 url；相對路徑以工作目錄為準）"},
            "url": {"type": "string", "description": "http(s) 或 file:/// 網址"},
            "shots": {"type": "array", "description": "依序截；省略＝一張 1280×800",
                      "items": {"type": "object", "properties": {
                          "label": {"type": "string"},
                          "width": {"type": "integer"},
                          "height": {"type": "integer"},
                          "js": {"type": "string", "description": "截前執行（可 await）"},
                          "wait_ms": {"type": "integer", "description": "js 後等幾毫秒"},
                          "selector": {"type": "string", "description": "只截這個 CSS 元素"},
                          "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                                     "description": "只截 [x1, y1, x2, y2]，CSS 像素"},
                          "full_page": {"type": "boolean", "description": "整頁含捲動"},
                          "scale": {"type": "number", "description": "放大 0.25～4"},
                      }}},
            "wait_ms": {"type": "integer", "description": "載入後等幾毫秒，預設 1000"},
            "question": {"type": "string", "description": "要檢查什麼；空白＝描述畫面並指出問題"},
        }},
        view_page,
    )
