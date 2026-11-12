"""插件範本：複製整個資料夾、改名，修改 plugin.toml 與這個檔案。

可用的註冊方法：
  woof.add_tool(name, description, json_schema, handler)   handler: async (args, ctx) -> str | {"output", "is_error"}
  woof.add_provider(name, provider)                         模型轉換器，需實作 async stream(...)
  woof.add_engine(name, engine)                             自帶工具迴圈的引擎，需實作 async run(ctx, blocks)
  woof.on(hook, fn, order=100)                              掛鉤（名稱以程式為準）：
                                                            build_prompt   組系統提示詞片段
                                                            prepare_turn   每回合開始前（可能有副作用，例如壓縮、連線）
                                                            before_request 送模型前改訊息
                                                            before_tool / after_tool  工具執行前後
                                                            tool_exclude   決定這回合排除哪些工具
                                                            after_turn     回合結束後
                                                            force_compact  手動壓縮時
                                                            mcp_status / plugin_unload  MCP 狀態、插件卸載
  woof.settings                                             此插件的設定值
  woof.keys(pool_name)                                      金鑰池
"""


def register(woof):
    async def hello(args: dict, ctx) -> dict:
        return {"output": f"你好，{args.get('name', '世界')}！設定值：{woof.settings.get('example')}"}

    woof.add_tool(
        "hello",
        "範例工具：打招呼。",
        {"type": "object", "properties": {"name": {"type": "string"}}},
        hello,
    )
