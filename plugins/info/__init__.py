"""基礎資訊插件：current_time（時間、日期、時區）與 system_info（系統、Python、工作目錄）。"""
from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timedelta, timezone

WEEK = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# 內建時差表（不依賴系統時區資料庫；夏令時間不處理）
FIXED = {
    "UTC": 0, "Asia/Taipei": 8, "Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Macau": 8,
    "Asia/Tokyo": 9, "Asia/Seoul": 9, "Asia/Bangkok": 7, "Asia/Singapore": 8, "Asia/Kolkata": 5.5,
    "Europe/London": 0, "Europe/Paris": 1, "Europe/Berlin": 1,
    "America/New_York": -5, "America/Los_Angeles": -8, "Australia/Sydney": 10,
}


def register(woof):
    async def current_time(args: dict, tctx) -> dict:
        tz = str(args.get("timezone") or "").strip()
        local = datetime.now().astimezone()
        if tz and tz != "local":
            off = FIXED.get(tz)
            if off is None:
                names = "、".join(sorted(FIXED))
                return {"output": f"不認得時區 {tz}。可用：{names}（或留空用本機時間）", "is_error": True}
            now = datetime.now(timezone.utc) + timedelta(hours=off)
            label = f"{tz}（UTC{off:+g}，不含夏令時間）"
        else:
            now = local
            label = f"本機（UTC{local.strftime('%z')[:3]}:{local.strftime('%z')[3:]}）"
        return {"output": (f"現在時間：{now:%Y-%m-%d %H:%M:%S}\n"
                           f"星期：{WEEK[now.weekday()]}\n時區：{label}\n"
                           f"UTC 時間：{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}")}

    async def system_info(args: dict, tctx) -> dict:
        cwd = os.getcwd()
        return {"output": (f"作業系統：{platform.system()} {platform.release()}（{platform.version()}）\n"
                           f"機器：{platform.machine()}\n"
                           f"Python：{sys.version.split()[0]}\n"
                           f"工作目錄：{cwd}\n"
                           f"使用者目錄：{os.path.expanduser('~')}")}

    woof.add_tool(
        "current_time",
        "查現在的日期、時間、星期與時區。需要知道今天幾號、現在幾點、星期幾時使用。"
        "timezone 可留空（本機時間）或填 Asia/Taipei、Asia/Tokyo、UTC、America/New_York 等。",
        {"type": "object", "properties": {"timezone": {"type": "string", "description": "時區名稱，留空＝本機"}}},
        current_time,
    )
    woof.add_tool(
        "system_info",
        "查作業系統、Python 版本、機器架構與目前工作目錄。",
        {"type": "object", "properties": {}},
        system_info,
    )
