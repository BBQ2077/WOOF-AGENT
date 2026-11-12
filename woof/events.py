"""事件匯流排：插件在固定時機插手。

掛鉤名稱：
  build_prompt(ctx) -> str | None            組系統提示，回傳要加入的段落
  before_request(messages, ctx) -> messages  送出模型請求前
  tool_specs(specs, ctx) -> specs            送出模型請求前，調整這一步要送的工具清單（工具延遲載入用）
  before_tool(name, args, ctx) -> args | None   執行工具前；回傳 None 表示照原樣
  after_tool(name, args, result, ctx) -> result | None
  on_message(message, ctx)                    模型回覆完成
  on_session(event, session)                  對話新增、切換、刪除
  prepare_turn(ctx)                           回合開始前（可改寫歷史、發通知）
  plugin_unload(name)                         插件被卸載前
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("woof.events")


@dataclass
class Hook:
    name: str
    fn: Callable
    plugin: str
    order: int = 100


class EventBus:
    def __init__(self) -> None:
        self.hooks: dict[str, list[Hook]] = {}

    def on(self, name: str, fn: Callable, plugin: str, order: int = 100) -> None:
        self.hooks.setdefault(name, []).append(Hook(name, fn, plugin, order))
        self.hooks[name].sort(key=lambda h: h.order)

    def remove_plugin(self, plugin: str) -> None:
        for name in list(self.hooks):
            self.hooks[name] = [h for h in self.hooks[name] if h.plugin != plugin]

    async def _call(self, h: Hook, *args) -> Any:
        try:
            r = h.fn(*args)
            if inspect.isawaitable(r):
                r = await r
            return r
        except Exception:
            log.exception("掛鉤 %s（插件 %s）執行失敗", h.name, h.plugin)
            return None

    async def collect(self, name: str, *args) -> list[Any]:
        """依序呼叫，收集非 None 的回傳值。"""
        out = []
        for h in self.hooks.get(name, []):
            r = await self._call(h, *args)
            if r is not None:
                out.append(r)
        return out

    async def collect_named(self, name: str, *args) -> list[tuple[str, Any]]:
        """同 collect，但每筆附上回傳它的插件名（面板分段顯示用）。"""
        out = []
        for h in self.hooks.get(name, []):
            r = await self._call(h, *args)
            if r is not None:
                out.append((h.plugin, r))
        return out

    async def pipe(self, name: str, value: Any, *args) -> Any:
        """把 value 依序交給每個掛鉤修改；回傳 None 表示不改。"""
        for h in self.hooks.get(name, []):
            r = await self._call(h, value, *args)
            if r is not None:
                value = r
        return value

    async def emit(self, name: str, *args) -> None:
        for h in self.hooks.get(name, []):
            await self._call(h, *args)
