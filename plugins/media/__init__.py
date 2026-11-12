"""生成圖片、影片的工具：聊天模型呼叫 image_generate／video_generate，用設定的生成模型做，檔案存在 data/media。

對話直接選生成型模型時不走這裡（openai_compat 直接生成）；這裡是給一般聊天模型「順手生一張圖」用的。
"""
from __future__ import annotations

from pathlib import Path


def register(woof):
    from woof import media

    def pick(kind: str) -> dict | None:
        want = str(woof.settings.get(f"{kind}_model") or "")
        models = woof.config.get("models", [])
        if want:
            return next((m for m in models if m["id"] == want), None)
        for m in models:
            if media.kind_of(m) != kind or m.get("enabled") is False:
                continue
            pool = woof.keys(m.get("key_pool") or m.get("provider"))
            if pool.entries:
                return m
        return None

    def missing(kind: str) -> dict:
        return {"output": f"沒有可用的{media.KIND_LABEL[kind]}模型：到「設定 → 模型商」加一個（例如 Agnes 的 agnes-{kind}-…），"
                          f"或在插件「media」指定", "is_error": True}

    async def image_generate(args: dict, tctx) -> dict:
        m = pick("image")
        if not m:
            return missing("image")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"output": "prompt 不能空白", "is_error": True}
        refs = []
        for p in args.get("images") or []:
            fp = Path(str(p))
            if not fp.is_absolute():
                fp = Path(tctx.cwd) / fp
            if not fp.is_file():
                return {"output": f"參考圖不存在：{fp}", "is_error": True}
            refs.append(str(fp))
        await tctx.output(f"用 {m.get('label', m['id'])} 生成圖片中…\n")
        r = await media.gen_image(woof, m, prompt, images=refs, size=str(args.get("size") or ""),
                                  ratio=str(args.get("ratio") or ""))
        return {"output": f"已生成（{m.get('label', m['id'])}，{r['seconds']} 秒）：{r['path']}\n"
                          f"回覆使用者時寫上 MEDIA:{r['path']} 讓介面顯示這張圖。"}

    async def video_generate(args: dict, tctx) -> dict:
        m = pick("video")
        if not m:
            return missing("video")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"output": "prompt 不能空白", "is_error": True}
        await tctx.output(f"用 {m.get('label', m['id'])} 生成影片中（通常 1–3 分鐘）…\n")

        async def prog(pct, st):
            await tctx.output(f"進度 {pct}%（{st}）\n")

        r = await media.gen_video(woof, m, prompt, seconds=int(args.get("seconds") or 5),
                                  aspect=str(args.get("aspect_ratio") or "16:9"), progress=prog)
        return {"output": f"已生成影片（{m.get('label', m['id'])}，{r['video_seconds']} 秒長）：{r['path']}\n"
                          f"回覆使用者時寫上 MEDIA:{r['path']} 讓介面顯示這段影片。"}

    woof.add_tool(
        "image_generate",
        "生成圖片（文字生圖；給 images 就是圖生圖或多圖合成）。prompt 寫清楚主體、場景、風格、光線、構圖。"
        "完成後回傳檔案路徑，回覆時寫 MEDIA:<路徑> 讓使用者看到。",
        {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "要生成的畫面描述"},
            "ratio": {"type": "string", "enum": ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"], "description": "長寬比，預設 1:1"},
            "size": {"type": "string", "description": "解析度級距，例如 1K、2K（預設 1K）"},
            "images": {"type": "array", "items": {"type": "string"}, "description": "參考圖的本機路徑（選填）"},
        }, "required": ["prompt"]},
        image_generate,
    )
    woof.add_tool(
        "video_generate",
        "生成短影片（文字生影片，4–12 秒）。要等 1–3 分鐘，使用者明確要影片才用。完成後回覆時寫 MEDIA:<路徑>。",
        {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "畫面、動作、鏡頭運動、光線"},
            "seconds": {"type": "integer", "minimum": 4, "maximum": 12, "description": "長度，預設 5"},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "description": "預設 16:9"},
        }, "required": ["prompt"]},
        video_generate,
    )
