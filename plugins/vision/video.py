"""watch_video 的下載、抽畫面、字幕。

- yt-dlp 不裝進 WOOF：用 `uv run --with yt-dlp --exclude-newer "7 days"` 跑，只用發佈滿 7 天的版本
  （被駭的版本通常幾天內就下架）。沒有 uv 時，WOOF 的 Python 裡有 yt_dlp 就用它。
- 只抓 480p 以下的純畫面（不用合併音軌），存在 data/spill/video/<網址雜湊>/，同一支再看不重抓，放 1 天就刪。
- 下載：一整個檔案的格式由 WOOF 分段平行下載（PARTS 條連線各要一段，影片站常對每條連線限速）；
  伺服器不支援分段（回整個檔案而不是 206）就單線下載。開始後太慢就重讀影片資訊找其他鏡像站一起下載（見 download）。
  WOOF 自己下載失敗才交給 yt-dlp；切片格式（HLS／DASH）交給 yt-dlp -N 平行抓切片。
  超過 IDLE 秒沒有進度才算失敗，長影片不會被總時間卡掉。
- 抽畫面用 ffmpeg：PATH 上找，沒有就同樣用 uv 拿 imageio-ffmpeg 附的。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COOLDOWN = "7 days"
MAX_FRAMES = 16
SUB_CHARS = 6000
FMT = "bv*[height<=480]/b[height<=480]/wv*/w"
SORT = "res:480,+size"
PARTS, PIECE, IDLE = 8, 4 << 20, 60
# 開始 TRY_SEC 秒後合計不到 SLOW（位元組／秒）就重讀 REROLL 次影片資訊找其他鏡像站
SLOW, TRY_SEC, REROLL, PROBE = 2 << 20, 5, 3, 1 << 20
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
_LOCKS: dict[str, asyncio.Lock] = {}
_FF: list[str] = []


def _uv(pkg: str) -> list[str] | None:
    uv = shutil.which("uv")
    return [uv, "run", "--no-project", "--quiet", "--with", pkg, "--exclude-newer", COOLDOWN] if uv else None


def _ytdlp() -> list[str] | None:
    base = _uv("yt-dlp[default]")
    if base:
        return base + ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"] if importlib.util.find_spec("yt_dlp") else None


def _kill(pid: int) -> None:
    # uv 底下還有 python 子程序：整棵樹一起結束
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)


async def _run(cmd: list[str], timeout: float, on_line=None, idle: float | None = None) -> tuple[int, str, str]:
    """idle：超過這麼多秒沒有新的一行輸出就結束（TimeoutError）"""
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             env=_ENV, creationflags=_NOWIN, limit=64 * 1024 * 1024)

    async def out() -> bytes:
        buf = []
        while True:
            line = await (asyncio.wait_for(p.stdout.readline(), idle) if idle else p.stdout.readline())
            if not line:
                break
            buf.append(line)
            if on_line:
                await on_line(line.decode("utf-8", "replace").strip())
        return b"".join(buf)

    try:
        o, e, _ = await asyncio.wait_for(asyncio.gather(out(), p.stderr.read(), p.wait()), timeout)
    except BaseException:
        _kill(p.pid)
        raise
    return p.returncode, o.decode("utf-8", "replace"), e.decode("utf-8", "replace")


def _last_err(err: str) -> str:
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    bad = [ln for ln in lines if ln.startswith("ERROR")]
    return ((bad or lines or ["（沒有訊息）"])[-1])[:300]


def _stream(c, url: str, hdr: dict, rng: str | None):
    return c.stream("GET", url, headers={**hdr, "Range": rng} if rng else hdr)


def _host(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


async def _mirrors(more, seen: set) -> list[tuple[str, dict]]:
    """重讀 REROLL 次影片資訊（同時跑），留下主機沒看過的網址"""
    if not more:
        return []
    got = await asyncio.gather(*(more() for _ in range(REROLL)), return_exceptions=True)
    out = []
    for g in got:
        if isinstance(g, tuple) and g[0] and _host(g[0]) not in seen:
            seen.add(_host(g[0]))
            out.append(g)
    return out


async def _speed(c, url: str, hdr: dict, size: int) -> float:
    """要開頭 PROBE 位元組（最多量 3 秒）量這台一條連線的速度（位元組／秒）；拿不到或檔案大小不同＝0"""
    import httpx
    t, n = time.monotonic(), 0
    try:
        async with _stream(c, url, hdr, f"bytes=0-{min(size, PROBE) - 1}") as r:
            m = re.fullmatch(r"bytes 0-\d+/(\d+)", r.headers.get("content-range", "").strip())
            if r.status_code != 206 or not m or int(m.group(1)) != size:
                return 0.0
            async for ch in r.aiter_bytes():
                n += len(ch)
                if time.monotonic() - t > 3:
                    break
    except httpx.HTTPError:
        return 0.0
    return n / max(time.monotonic() - t, 1e-3)


def _mb(x: float) -> str:
    return f"{x / (1 << 20):.1f} MB/s"


async def download(url: str, headers: dict, dest: Path, say, more=None) -> None:
    """下載一整個檔案到 dest。more()＝重讀一次影片資訊，回 (網址, 標頭)（可能分到別的鏡像站）。
    - 伺服器支援分段：切成 PIECE 大小的段，PARTS 條連線輪流領下一段；斷線從斷點接著要，同一段連續 5 次沒拿到才放棄。
      開始 TRY_SEC 秒後合計不到 SLOW，就找其他鏡像站：每台量一條連線的速度，不到最快那台三分之一的不用，
      連線分到留下的鏡像站上（快的自然多領幾段），被淘汰的那台正在下載的段由別台接著要。
    - 不支援分段：單線下載；TRY_SEC 秒後不到 SLOW 就讓其他鏡像站也各自從頭下載，再過 TRY_SEC 秒留預計最快完成的那條。"""
    import httpx
    hdr = {k: v for k, v in (headers or {}).items() if k.lower() != "range"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(IDLE / 2, connect=15), follow_redirects=True,
                                 limits=httpx.Limits(max_connections=PARTS * (REROLL + 1))) as c:
        async with _stream(c, url, hdr, "bytes=0-0") as r:
            m = re.fullmatch(r"bytes 0-0/(\d+)", r.headers.get("content-range", "").strip())
            ranged = r.status_code == 206 and m
        if ranged:
            await _ranged(c, url, hdr, int(m.group(1)), dest, say, more)
        else:
            await say("伺服器不支援分段，單線下載…")
            await _race(c, url, hdr, dest, say, more)


async def _ranged(c, url: str, hdr: dict, size: int, dest: Path, say, more) -> None:
    import httpx
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as f:
        f.truncate(size)
    todo = [(a, min(a + PIECE, size) - 1) for a in range(0, size, PIECE)][::-1]
    srcs = [{"url": url, "hdr": hdr, "dead": False}]
    got, shown, t0 = [0], [-10.0], time.monotonic()

    def pick(i: int) -> dict:
        live = [s for s in srcs if not s["dead"]]
        return live[i % len(live)]

    async def worker(i: int) -> None:
        with open(tmp, "r+b") as f:
            while todo:
                a, b = todo.pop()
                pos, tries = a, 0
                while pos <= b:
                    s, start = pick(i), pos
                    try:
                        async with _stream(c, s["url"], s["hdr"], f"bytes={pos}-{b}") as r:
                            if r.status_code != 206:
                                raise httpx.HTTPError(f"HTTP {r.status_code}")
                            async for chunk in r.aiter_bytes(256 * 1024):
                                chunk = chunk[:b - pos + 1]
                                f.seek(pos)
                                f.write(chunk)
                                pos += len(chunk)
                                got[0] += len(chunk)
                                pct = got[0] * 100 / size
                                if pct - shown[0] >= 10:
                                    shown[0] = pct
                                    await say(f"下載 {pct:.1f}%")
                                if s["dead"]:
                                    break
                    except httpx.HTTPError:
                        if pos == start:
                            tries += 1
                    if pos <= b and not s["dead"]:
                        if tries >= 5:
                            raise RuntimeError(f"第 {a:,} 位元組起的那段連續 5 次沒拿到資料")
                        await asyncio.sleep(tries)

    async def watch() -> None:
        await asyncio.sleep(TRY_SEC)
        rate = got[0] / (time.monotonic() - t0)
        if rate >= SLOW or not todo or not more:
            return
        await say(f"速度 {_mb(rate)} 偏慢，找其他伺服器…")
        cands = await _mirrors(more, {_host(url)})
        if not cands:
            await say("沒有拿到其他伺服器，照原本的下載")
            return
        allc = [(url, hdr)] + cands
        sp = await asyncio.gather(*(_speed(c, u, h, size) for u, h in allc))
        best = max(sp)
        if best <= 0:
            return
        for (u, h), v in list(zip(allc, sp))[1:]:
            if v >= best / 3:
                srcs.append({"url": u, "hdr": h, "dead": False})
        srcs[0]["dead"] = sp[0] < best / 3
        live = [f"{_host(s['url'])}（{_mb(v)}）" for s, v in zip(srcs, [sp[0]] + [v for v in sp[1:] if v >= best / 3])
                if not s["dead"]]
        await say(f"改從 {len(live)} 台伺服器同時下載：" + "、".join(live))

    tasks = [asyncio.create_task(worker(i)) for i in range(min(PARTS, len(todo)))]
    mon = asyncio.create_task(watch())
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tmp.unlink(missing_ok=True)
        raise
    finally:
        mon.cancel()
        await asyncio.gather(mon, return_exceptions=True)
    if got[0] != size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"只收到 {got[0]:,}／{size:,} 位元組")
    tmp.replace(dest)


async def _race(c, url: str, hdr: dict, dest: Path, say, more) -> None:
    runs: list[dict] = []
    lead = [None]

    async def one(e: dict) -> None:
        shown = -10.0
        async with _stream(c, e["url"], e["hdr"], None) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            e["size"] = int(r.headers.get("content-length") or 0)
            with open(e["tmp"], "wb") as f:
                async for chunk in r.aiter_bytes(256 * 1024):
                    f.write(chunk)
                    e["n"] += len(chunk)
                    if lead[0] is e and e["size"] and (pct := e["n"] * 100 / e["size"]) - shown >= 10:
                        shown = pct
                        await say(f"下載 {pct:.1f}%")
        if e["size"] and e["n"] != e["size"]:
            raise RuntimeError(f"只收到 {e['n']:,}／{e['size']:,} 位元組")

    def start(u: str, h: dict) -> dict:
        e = {"url": u, "hdr": h, "n": 0, "size": 0, "t0": time.monotonic(),
             "tmp": dest.with_name(f"{dest.name}.{len(runs)}.part")}
        e["task"] = asyncio.create_task(one(e))
        runs.append(e)
        return e

    def rate(e: dict) -> float:
        return e["n"] / max(time.monotonic() - e["t0"], 1e-3)

    def eta(e: dict) -> float:
        if e["task"].done():
            return -1.0 if not e["task"].exception() else float("inf")
        left = (e["size"] - e["n"]) if e["size"] else 1
        return left / rate(e) if rate(e) else float("inf")

    lead[0] = start(url, hdr)
    try:
        await asyncio.wait({lead[0]["task"]}, timeout=TRY_SEC)
        if not lead[0]["task"].done() and rate(lead[0]) < SLOW and more:
            await say(f"速度 {_mb(rate(lead[0]))} 偏慢，同時從其他伺服器下載，{TRY_SEC} 秒後留最快的…")
            for u, h in await _mirrors(more, {_host(url)}):
                start(u, h)
            if len(runs) > 1:
                await asyncio.wait({e["task"] for e in runs}, timeout=TRY_SEC, return_when=asyncio.FIRST_COMPLETED)
                lead[0] = min(runs, key=eta)
                await say(f"留下 {_host(lead[0]['url'])}（{_mb(rate(lead[0]))}）")
            else:
                await say("沒有拿到其他伺服器，照原本的下載")
        for e in runs:
            if e is not lead[0]:
                e["task"].cancel()
        await lead[0]["task"]
    except BaseException:
        for e in runs:
            e["task"].cancel()
        await asyncio.gather(*(e["task"] for e in runs), return_exceptions=True)
        for e in runs:
            e["tmp"].unlink(missing_ok=True)
        raise
    await asyncio.gather(*(e["task"] for e in runs), return_exceptions=True)
    for e in runs:
        if e is not lead[0]:
            e["tmp"].unlink(missing_ok=True)
    lead[0]["tmp"].replace(dest)


def _pick_sub(info: dict) -> tuple[str, bool]:
    """手動字幕優先（中文 → 影片原文 → 英日），沒有才用自動字幕（原文辨識優先於翻譯）。回傳 (語言, 是否自動)。"""
    man = [k for k in (info.get("subtitles") or {}) if k not in ("danmaku", "live_chat", "rechat")]
    lang = info.get("language") or ""
    for k in ("zh-Hant", "zh-TW", "zh-HK", "zh", "zh-Hans", "zh-CN", "ai-zh", lang, "en", "ja"):
        if k and k in man:
            return k, False
    if man:
        return man[0], False
    auto = info.get("automatic_captions") or {}
    for k in (f"{lang}-orig", lang, "zh-Hant", "en"):
        if k and k in auto:
            return k, True
    return "", False


def _cached(d: Path) -> Path | None:
    vids = [p for p in d.glob("video.*") if p.suffix not in (".part", ".ytdl", ".json", ".temp")]
    if not (vids and (d / "info.json").is_file()):
        return None
    for p in d.iterdir():   # 再看一次就延後清掉
        os.utime(p)
    return vids[0]


async def fetch(url: str, root: Path, say) -> tuple[Path, dict]:
    """下載到 root/<雜湊>/：video.*、sub.*、info.json。say(text) 回報進度。"""
    d = root / hashlib.sha1(url.encode()).hexdigest()[:12]
    async with _LOCKS.setdefault(d.name, asyncio.Lock()):
        d.mkdir(parents=True, exist_ok=True)
        v = _cached(d)
        if v:
            return v, json.loads((d / "info.json").read_text("utf-8"))
        yt = _ytdlp()
        if not yt:
            raise RuntimeError("看網址影片要 uv（https://docs.astral.sh/uv/），或在 WOOF 的 Python 裝 yt-dlp")
        await say("讀影片資訊…")
        # 帶上畫質條件：頂層就是選好的那個檔案（網址、協定、要帶的標頭）
        rc, out, err = await _run(yt + ["-J", "--no-playlist", "-f", FMT, "-S", SORT, url], 120)
        if rc or not out.strip():
            raise RuntimeError(f"yt-dlp 讀不到影片：{_last_err(err)}")
        info = json.loads(out)
        if info.get("is_live"):
            raise RuntimeError("直播中的影片看不了")
        (d / "info.json").write_text(json.dumps(info, ensure_ascii=False), "utf-8")
        base = yt + ["--load-info-json", str(d / "info.json"), "-P", str(d), "-f", FMT, "-S", SORT]
        lang, auto = _pick_sub(info)
        subs = (["--write-auto-subs" if auto else "--write-subs", "--sub-langs", lang,
                 "--sub-format", "vtt/srt/best", "-o", "subtitle:sub"] if lang else [])
        await say("下載中…")
        if info.get("url") and info.get("protocol") in ("http", "https"):
            async def more() -> tuple[str, dict] | None:
                # 重讀一次影片資訊，可能分到別的鏡像站；要同一個格式才算
                rc, out, _ = await _run(yt + ["-J", "--no-playlist", "-f", FMT, "-S", SORT, url], 120)
                j = json.loads(out) if rc == 0 and out.strip() else {}
                if j.get("format_id") == info.get("format_id") and j.get("protocol") in ("http", "https") and j.get("url"):
                    return j["url"], j.get("http_headers") or {}
                return None

            try:
                await download(info["url"], info.get("http_headers") or {}, d / f"video.{info.get('ext') or 'mp4'}", say, more)
            except Exception as e:
                await say(f"分段下載失敗（{str(e)[:120]}），改用 yt-dlp 下載…")
            else:
                if subs:
                    await _run(base + ["--skip-download", "--quiet"] + subs, 120)
                return _cached(d), info
        cmd = base + ["-o", "video.%(ext)s", "-N", str(PARTS), "--quiet", "--progress", "--newline",
                      "--progress-template", "download:%(progress._percent_str)s"] + subs
        if str(info.get("extractor_key", "")).lower().startswith("bilibili"):
            cmd += ["--add-headers", "Range:bytes=0-"]   # B 站部分 CDN 沒這個會回 567
        shown = [-10.0]

        async def prog(line: str) -> None:
            m = re.search(r"([\d.]+)%", line)
            if m and float(m.group(1)) - shown[0] >= 10:
                shown[0] = float(m.group(1))
                await say(f"下載 {m.group(1)}%")

        try:
            rc, _, err = await _run(cmd, 6 * 3600, prog, IDLE)
        except TimeoutError:
            raise RuntimeError(f"下載超過 {IDLE} 秒沒有進度") from None
        v = _cached(d)
        if rc or not v:
            raise RuntimeError(f"下載失敗：{_last_err(err)}")
        return v, info


async def ffmpeg() -> str:
    if _FF:
        return _FF[0]
    ff = shutil.which("ffmpeg")
    if not ff and (base := _uv("imageio-ffmpeg")):
        rc, out, _ = await _run(base + ["python", "-c", "import imageio_ffmpeg as m; print(m.get_ffmpeg_exe())"], 300)
        p = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
        ff = p if p and Path(p).is_file() else None
    if not ff:
        raise RuntimeError("抽畫面要 ffmpeg：裝好放進 PATH，或裝 uv 讓 WOOF 自己拿")
    _FF.append(ff)
    return ff


async def probe(ff: str, path: Path) -> tuple[float, str]:
    """(秒數, 寬×高)；讀不到秒數回 0。"""
    _, _, err = await _run([ff, "-hide_banner", "-i", str(path)], 30)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return dur, (f"{size.group(1)}×{size.group(2)}" if size else "")


def pick_times(dur: float, start: float | None, end: float | None, n: int | None, times: list | None) -> list[float]:
    top = dur - 0.05 if dur else float("inf")
    if times:
        return sorted({round(min(max(0.0, float(t)), top), 2) for t in times})[:MAX_FRAMES]
    a = max(0.0, float(start or 0))
    b = min(float(end), dur or float("inf")) if end is not None else (dur or a + 1)
    if b <= a:
        raise ValueError(f"start／end 不對：影片長 {clock(dur, False)}")
    n = max(1, min(MAX_FRAMES, int(n or 9)))
    step = (b - a) / n
    return [round(a + step * (i + 0.5), 2) for i in range(n)]   # 取每段中間，避開片頭黑畫面


async def grab(ff: str, path: Path, times: list[float], width: int) -> list[Image.Image | None]:
    sem = asyncio.Semaphore(4)

    async def one(t: float):
        async with sem:
            _, out, _ = await _run_bytes([ff, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                                          "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "png", "-"], 60)
        try:
            return Image.open(io.BytesIO(out)).convert("RGB") if out else None
        except Exception:
            return None

    return list(await asyncio.gather(*(one(t) for t in times)))


async def _run_bytes(cmd: list[str], timeout: float) -> tuple[int, bytes, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             creationflags=_NOWIN)
    try:
        o, e = await asyncio.wait_for(p.communicate(), timeout)
    except BaseException:
        _kill(p.pid)
        raise
    return p.returncode, o, e.decode("utf-8", "replace")


def clock(t: float, frac: bool = True) -> str:
    """秒數 → m:ss（滿一小時 h:mm:ss）；有小數就接著寫到 0.01 秒（65.5 → 1:05.5）。"""
    c = round(float(t) * 100) if frac else int(t) * 100
    s, f = divmod(c, 100)
    out = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    return out + (f".{f:02d}".rstrip("0") if f else "")


def tile_width(n: int) -> int:
    cols = n if n <= 3 else math.ceil(math.sqrt(n))
    return max(320, min(854, 1600 // cols))


def sheet(frames: list[tuple[float, Image.Image | None]]) -> Image.Image:
    """拼成一張：格子左上角標時間。"""
    n = len(frames)
    cols = n if n <= 3 else math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    tw = max((im.width for _, im in frames if im), default=tile_width(n))
    th = max((im.height for _, im in frames if im), default=tw * 9 // 16)
    gap = 4
    out = Image.new("RGB", (cols * tw + (cols - 1) * gap, rows * th + (rows - 1) * gap), (24, 24, 24))
    font = ImageFont.load_default(size=max(14, tw // 18))
    dr = ImageDraw.Draw(out)
    for i, (t, im) in enumerate(frames):
        x, y = i % cols * (tw + gap), i // cols * (th + gap)
        if im:
            out.paste(im, (x, y))
        label = clock(t) if im else f"{clock(t)} 沒有畫面"
        box = dr.textbbox((x + 4, y + 2), label, font=font)
        dr.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=(0, 0, 0))
        dr.text((x + 4, y + 2), label, font=font, fill=(255, 255, 255))
    return out


_CUE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->")


def parse_subs(text: str) -> list[tuple[float, str]]:
    """vtt／srt → [(秒, 文字)]；自動字幕一行一行捲動重複的只留一次。"""
    cues, last, t, buf = [], None, None, []

    def flush():
        nonlocal last
        keep = []
        for ln in buf:
            if ln != last:
                keep.append(ln)
                last = ln
        if t is not None and keep:
            cues.append((t, " ".join(keep)))

    for raw in text.splitlines():
        m = _CUE.search(raw)
        if m:
            flush()
            h, mi, s, ms = (int(g or 0) for g in m.groups())
            t, buf = h * 3600 + mi * 60 + s + ms / 1000, []
        elif not raw.strip():
            flush()
            t, buf = None, []
        elif t is not None:
            ln = re.sub(r"<[^>]+>", "", raw).replace("&nbsp;", " ").strip()
            if ln and not ln.isdigit():
                buf.append(ln)
    flush()
    return cues


def subs_text(d: Path, a: float, b: float) -> tuple[str, str]:
    """(字幕檔名, a～b 秒的字幕)；太長就截斷並說明從哪接著看。"""
    files = [p for p in d.glob("sub.*") if p.suffix in (".vtt", ".srt")]
    if not files:
        return "", ""
    lines, size = [], 0
    for t, s in parse_subs(files[0].read_text("utf-8", "replace")):
        if not a <= t < b:
            continue
        ln = f"[{clock(t)}] {s}"
        if size + len(ln) > SUB_CHARS:
            lines.append(f"…字幕太長，後面用 start={int(t)} 接著看")
            break
        lines.append(ln)
        size += len(ln) + 1
    return files[0].name, "\n".join(lines)
