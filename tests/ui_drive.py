"""用 Chrome DevTools Protocol 操作執行中的 WOOF 視窗：下指令、等待、截圖。

用法：python tests/ui_drive.py <debug_port> <動作檔.json>
動作：{"mouse": [[type, x, y], ...]} / {"eval": "js"} / {"wait": "js 條件", "timeout": 秒} / {"sleep": 秒} / {"shot": "檔名.png"}
"""
import asyncio
import base64
import json
import sys
import time
import urllib.request

import websockets

PORT = int(sys.argv[1])
STEPS = json.load(open(sys.argv[2], encoding="utf-8"))


async def main():
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        n = 0

        async def call(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n:
                    if "error" in m:
                        raise RuntimeError(m["error"])
                    return m["result"]

        async def ev(js):
            r = await call("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
            if r.get("exceptionDetails"):
                return "JS錯誤: " + json.dumps(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]), ensure_ascii=False)[:300]
            return r["result"].get("value")

        for st in STEPS:
            if "eval" in st:
                print("eval", st["eval"][:70].replace("\n", " "), "=>", json.dumps(await ev(st["eval"]), ensure_ascii=False)[:400])
            elif "wait" in st:
                t0 = time.time()
                ok = False
                while time.time() - t0 < st.get("timeout", 30):
                    if await ev(st["wait"]):
                        ok = True
                        break
                    await asyncio.sleep(0.3)
                print("wait", st["wait"][:70], "=>", "OK" if ok else "逾時", f"{time.time() - t0:.1f}s")
            elif "mouse" in st:
                # {"mouse": [[type, x, y], ...]}：type = mousePressed / mouseMoved / mouseReleased
                for typ, x, y in st["mouse"]:
                    await call("Input.dispatchMouseEvent", type=typ, x=x, y=y, button="left",
                               buttons=0 if typ == "mouseReleased" else 1, clickCount=1)
                    await asyncio.sleep(0.03)
                print("mouse", len(st["mouse"]))
            elif "cdp" in st:
                # {"cdp": "Emulation.setDeviceMetricsOverride", "params": {...}}：直接下 CDP 指令（例如模擬窄視窗）
                print("cdp", st["cdp"], json.dumps(await call(st["cdp"], **st.get("params", {})), ensure_ascii=False)[:200])
            elif "sleep" in st:
                await asyncio.sleep(st["sleep"])
            elif "shot" in st:
                r = await call("Page.captureScreenshot", format="png")
                open(st["shot"], "wb").write(base64.b64decode(r["data"]))
                print("shot", st["shot"])


asyncio.run(main())
