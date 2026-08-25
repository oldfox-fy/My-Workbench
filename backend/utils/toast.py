# backend/utils/toast.py
# 发送 Windows 原生 Toast 通知（无第三方依赖，走 PowerShell + WinRT）。
#
# 背景：本应用是 PyWebView 桌面程序，非 UWP/打包应用，Windows 的 Toast 需要一个
# 已注册的 AUMID。这里借用 PowerShell 自身在「开始菜单」里的 AppId
# （{1AC14E77-...}\WindowsPowerShell\v1.0\powershell.exe）来弹 toast，
# 无需 pip 安装任何包、也无需生成 .lnk 快捷方式。
#
# 用途：P3 系统通知（send_notification 工具）+ P2 定时提醒到点后的视觉弹窗。
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from xml.sax.saxutils import escape

logger = logging.getLogger("My Workbench")

# PowerShell 在「开始菜单」中的 AUMID（ToastNotificationManager.CreateToastNotifier 用）
_PS_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

# 占位符模板：标题/正文/AppId 用 .replace 注入，避免与 PowerShell 的 $、{} 冲突。
_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast duration="short">
  <visual>
    <binding template="ToastText02">
      <text id="1">__TITLE__</text>
      <text id="2">__MESSAGE__</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default" />
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APP_ID__').Show($toast)
"""


def _clean(text: str) -> str:
    """清洗文本：去掉换行/控制字符、压缩空白，并做 XML 转义（避免注入破坏 toast 结构）。"""
    out = (text or "").replace("\r", " ").replace("\n", " ")
    out = " ".join(out.split())
    return escape(out)


def send_toast(title: str, message: str, timeout: int = 5) -> bool:
    """发送一条 Windows 10/11 原生 toast，返回是否成功。

    非 Windows、无 PowerShell、系统关闭了通知等场景不会抛异常，仅记日志并返回 False，
    调用方（提醒/工具）据此降级（提醒仍会走 WS 广播 + 语音播报）。
    """
    if sys.platform != "win32":
        logger.info("[Toast] 非 Windows 平台，跳过原生通知")
        return False

    title = _clean(title) or "提醒"
    message = _clean(message)

    ps = (
        _PS_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__MESSAGE__", message)
        .replace("__APP_ID__", _PS_APP_ID)
    )

    try:
        flags = subprocess.CREATE_NO_WINDOW  # 隐藏 PowerShell 控制台窗口
    except AttributeError:
        flags = 0
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=timeout, creationflags=flags,
        )
        return proc.returncode == 0
    except Exception as e:
        logger.warning(f"[Toast] 发送原生通知失败：{e}")
        return False


async def send_toast_async(title: str, message: str, timeout: int = 5) -> bool:
    """send_toast 的异步封装（供 asyncio 事件循环内调用，避免阻塞）。"""
    return await asyncio.to_thread(send_toast, title, message, timeout)
