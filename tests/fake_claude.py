import uuid
TID = 'toolu_' + uuid.uuid4().hex[:12]
"""模擬 claude -p stream-json 輸出，用來測試 claude_cli 插件的串接。"""
import json, sys, time, uuid
args = sys.argv[1:]
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
line = sys.stdin.readline()
msg = json.loads(line)
text = "".join(c.get("text", "") for c in msg["message"]["content"] if c["type"] == "text")
nimg = sum(1 for c in msg["message"]["content"] if c["type"] == "image")
def out(o): print(json.dumps(o, ensure_ascii=False), flush=True)
print("some stderr noise", flush=True)
out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5-5"})
for ch in ["想一下", "……"]:
    out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ch}}})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"model": "claude-opus-5-5", "content": [{"type": "thinking", "thinking": "想一下……"}], "usage": {"input_tokens": 10, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 200, "output_tokens": 30}}})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": TID, "name": "Bash", "input": {"command": "echo hi"}}], "usage": {"input_tokens": 10, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 200, "output_tokens": 40}}})
time.sleep(30 if "慢" in text else 0.2)
out({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result", "tool_use_id": TID, "content": [{"type": "text", "text": "hi"}], "is_error": False}]}})
reply = f"收到：{text}（圖片 {nimg} 張，resume={'--resume' in args}，effort={args[args.index('--effort')+1] if '--effort' in args else None}）"
for i in range(0, len(reply), 6):
    out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply[i:i+6]}}})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": reply}], "usage": {"input_tokens": 12, "cache_read_input_tokens": 5300, "cache_creation_input_tokens": 50, "output_tokens": 60}}})
out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid, "total_cost_usd": 0.0123})
sys.stdin.read()  # 串流輸入模式：每輪送出 result 後，等 stdin 關閉才結束
