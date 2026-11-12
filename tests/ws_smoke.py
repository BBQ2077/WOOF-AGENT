"""後端整合測試：建立對話、送訊息、收事件。用法：python tests/ws_smoke.py <port> <token> <model> <prompt> [stop_after_s]"""
import asyncio
import json
import sys
import time
import urllib.request

import websockets


def api(port, token, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-woof-token": token, "content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


async def main():
    port, token, model, prompt = sys.argv[1:5]
    stop_after = float(sys.argv[5]) if len(sys.argv) > 5 else 0
    s = api(port, token, "POST", "/api/sessions", {"model": model})
    sid = s["id"]
    print("session", sid, s["model"], s["effort"])
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}", max_size=None) as ws:
        await ws.send(json.dumps({"type": "send", "session_id": sid, "text": prompt}))
        t0 = time.time()
        stopped = False
        kinds = {}
        while True:
            if stop_after and not stopped and time.time() - t0 > stop_after:
                await ws.send(json.dumps({"type": "stop", "session_id": sid}))
                stopped = True
                print(">>> 送出停止")
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
            except asyncio.TimeoutError:
                continue
            t = ev["type"]
            if t == "delta":
                kinds[ev["kind"]] = kinds.get(ev["kind"], "") + ev["text"]
                continue
            if t == "tool_output":
                print("  [tool_output]", ev["chunk"][:200].replace("\n", " | "))
                continue
            if t == "message":
                m = ev["message"]
                summary = [(b["type"], (b.get("text") or b.get("name") or b.get("output") or "")[:300]) for b in m["content"]]
                print(f"[message:{m['role']}]", json.dumps(summary, ensure_ascii=False))
                continue
            print(f"[{t}]", json.dumps({k: v for k, v in ev.items() if k not in ('type',)}, ensure_ascii=False)[:400])
            if t == "run_end":
                break
    print("耗時 %.1fs" % (time.time() - t0))
    print("思考字數", len(kinds.get("thinking", "")), "文字字數", len(kinds.get("text", "")))
    msgs = api(port, token, "GET", f"/api/sessions/{sid}/messages")
    print("資料庫訊息數", len(msgs), [m["role"] for m in msgs])
    print("標題", api(port, token, "GET", "/api/sessions")[0]["title"])


asyncio.run(main())
