import os
import asyncio, json, sys, urllib.request, base64
import websockets
P,T="8765","testtoken"
def api(m,p,b=None):
    r=urllib.request.Request(f"http://127.0.0.1:{P}{p}",method=m,data=json.dumps(b).encode() if b is not None else None,headers={"x-woof-token":T,"content-type":"application/json"})
    return json.loads(urllib.request.urlopen(r).read())
png=base64.b64encode(open(os.path.join(os.path.dirname(__file__),"assets","sample.png"),"rb").read()).decode()
async def turn(ws,sid,text,images=None,stop_after=None):
    await ws.send(json.dumps({"type":"send","session_id":sid,"text":text,"images":images or []}))
    loop=asyncio.get_running_loop(); t0=loop.time(); deltas={}
    while True:
        if stop_after and loop.time()-t0>stop_after:
            await ws.send(json.dumps({"type":"stop","session_id":sid})); stop_after=None; print("  >>stop")
        try: ev=json.loads(await asyncio.wait_for(ws.recv(),0.5))
        except asyncio.TimeoutError: continue
        if ev.get("session_id") not in (sid,None) and ev["type"]!="session_updated": continue
        t=ev["type"]
        if t=="delta": deltas[ev["kind"]]=deltas.get(ev["kind"],"")+ev["text"]; continue
        if t=="message": print("  message",ev["message"]["role"],[b["type"] for b in ev["message"]["content"]]); continue
        print(" ",t,json.dumps({k:v for k,v in ev.items() if k not in("type","session_id","session")},ensure_ascii=False)[:200])
        if t=="run_end": break
    print("  deltas",deltas)
async def main():
    model=sys.argv[1]
    s=api("POST","/api/sessions",{"model":model}); sid=s["id"]; print("session",sid)
    async with websockets.connect(f"ws://127.0.0.1:{P}/ws?token={T}",max_size=None) as ws:
        print("== 看圖"); await turn(ws,sid,"這張圖是什麼介面？左側、中間、右側各有什麼？三句話內回答。",[{"media_type":"image/png","data":png}])
        print("== 停止測試"); await turn(ws,sid,"用 terminal 執行 sleep 45 && echo finished（不要放背景）",stop_after=8)
asyncio.run(main())
