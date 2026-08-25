# backend/system_tools/reminder.py
# 提醒工具（LLM 慢通道）：当快通道 parse_reminder 解析不了时，LLM 用结构化参数落库。
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict


def _parse_at(s: str, now: float):
    """把时间字符串解析为 epoch 秒。支持「YYYY-MM-DD HH:MM」「HH:MM」等；失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    # 仅时间（HH:MM / HH:MM:SS）→ 默认今天，已过则顺延明天
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt).time()
            dt = datetime.now().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if dt.timestamp() <= now:
                dt += timedelta(days=1)
            return dt.timestamp()
        except ValueError:
            continue
    return None


async def set_reminder(content: str, at: str = "", after_seconds: int = 0,
                       recurring_seconds: int = 0) -> Dict[str, Any]:
    """设置一条提醒。时间用 at（具体时间）或 after_seconds（相对秒数）之一指定；
    recurring_seconds>0 表示循环提醒。"""
    from backend.voice.reminders import get_store

    content = (content or "").strip()
    if not content:
        return {"success": False, "content": "提醒内容不能为空"}

    now = time.time()
    recurring_seconds = int(recurring_seconds or 0)

    if at:
        due_ts = _parse_at(at, now)
        if due_ts is None:
            return {"success": False, "content": f"无法解析时间「{at}」，请用 YYYY-MM-DD HH:MM 或 HH:MM"}
    elif int(after_seconds or 0) > 0:
        due_ts = now + int(after_seconds)
    elif recurring_seconds > 0:
        due_ts = now + recurring_seconds
    else:
        return {"success": False, "content": "请指定提醒时间（at 或 after_seconds）"}

    reminder = await asyncio.to_thread(get_store().add, content, due_ts, recurring_seconds)
    return {
        "success": True,
        "id": reminder["id"],
        "content": reminder["content"],
        "due_ts": reminder["due_ts"],
        "recurring_seconds": reminder["recurring_seconds"],
    }


async def list_reminders() -> Dict[str, Any]:
    """列出所有未触发的提醒。"""
    from backend.voice.reminders import get_store

    items = await asyncio.to_thread(get_store().list)
    out = []
    for it in items:
        out.append({
            "id": it["id"],
            "content": it["content"],
            "due": datetime.fromtimestamp(it["due_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "recurring_seconds": it["recurring_seconds"],
        })
    return {"success": True, "count": len(out), "reminders": out}


async def cancel_reminder(reminder_id: int) -> Dict[str, Any]:
    """取消指定 id 的提醒。"""
    from backend.voice.reminders import get_store

    ok = await asyncio.to_thread(get_store().cancel, int(reminder_id))
    return {"success": ok, "content": "已取消该提醒" if ok else "未找到该提醒（可能已触发或 id 有误）"}
