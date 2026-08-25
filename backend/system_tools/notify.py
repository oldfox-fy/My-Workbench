# backend/system_tools/notify.py
# 系统通知（P3）：发送 Windows 原生 Toast 通知（右下角弹窗）。
from __future__ import annotations

from typing import Any, Dict


async def send_notification(title: str, message: str) -> Dict[str, Any]:
    """发送一条 Windows 原生 Toast 通知（title 为标题，message 为正文）。"""
    from backend.utils.toast import send_toast_async

    title = (title or "").strip() or "通知"
    message = (message or "").strip()
    ok = await send_toast_async(title, message)
    if ok:
        return {"success": True, "content": "系统通知已发送"}
    return {"success": False, "content": "系统通知发送失败（非 Windows 或系统未开启通知）"}
