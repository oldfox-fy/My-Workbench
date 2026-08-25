# backend/system_tools/system_control.py
# 计算机操作（P3）：打开应用/文件/网址、音量控制、截图、剪贴板、电源操作、前台窗口。
#
# 说明：
#   - 音量：pycaw（绝对音量/静音，基于 Windows Core Audio）+ comtypes COM；
#           依赖缺失时回退 ctypes 模拟媒体键（仅静音/相对加减）。
#   - 电源操作（关机/重启/睡眠/锁屏/休眠）为敏感操作，已加入工具审批列表。
#   - 所有 Windows 专属函数都在 asyncio.to_thread 里跑，避免阻塞事件循环。
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 应用别名 → 可执行文件名（os.startfile 会走 ShellExecute，自动在 App Paths / PATH 里解析）
_APP_ALIASES = {
    "记事本": "notepad.exe",
    "计算器": "calc.exe",
    "画图": "mspaint.exe",
    "任务管理器": "taskmgr.exe",
    "资源管理器": "explorer.exe",
    "文件资源管理器": "explorer.exe",
    "控制面板": "control.exe",
    "命令提示符": "cmd.exe",
    "cmd": "cmd.exe",
    "终端": "wt.exe",
    "windows终端": "wt.exe",
    "浏览器": "msedge.exe",
    "edge": "msedge.exe",
    "chrome": "chrome.exe",
    "谷歌浏览器": "chrome.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "ppt": "powerpnt.exe",
    "powerpoint": "powerpnt.exe",
    "vscode": "code.exe",
    "vs code": "code.exe",
    "微信": "WeChat.exe",
    "qq": "QQ.exe",
    "网易云音乐": "cloudmusic.exe",
}

# 电源操作 → 命令行（敏感操作，走工具审批）
_POWER_COMMANDS = {
    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    "hibernate": ["rundll32.exe", "powrprof.dll,SetSuspendState", "1,1,0"],
    "shutdown": ["shutdown", "/s", "/t", "0"],
    "restart": ["shutdown", "/r", "/t", "0"],
}


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on", "是", "开")
    return bool(v) if v is not None else default


# ── 打开应用 / 文件 / 网址 ──

async def open_app(target: str) -> Dict[str, Any]:
    """打开一个应用（按别名/中文名）、本地文件/文件夹，或网址。"""
    target = (target or "").strip()
    if not target:
        return {"success": False, "content": "请指定要打开的应用、文件或网址"}

    # 网址 → 默认浏览器
    if re.match(r"^https?://", target, re.I) or target.startswith("www."):
        import webbrowser
        url = target if target.startswith("http") else "https://" + target
        try:
            await asyncio.to_thread(webbrowser.open, url)
            return {"success": True, "content": f"已在默认浏览器打开 {url}"}
        except Exception as e:
            return {"success": False, "content": f"打开网址失败：{e}"}

    # 别名 → 可执行文件名
    name = _APP_ALIASES.get(target.lower(), target)

    def _launch():
        # ShellExecute：能解析完整路径、注册表 App Paths、PATH 中的可执行文件，
        # 以及 .lnk 快捷方式，比 subprocess.Popen 对「应用名」更鲁棒。
        os.startfile(name)

    try:
        await asyncio.to_thread(_launch)
        return {"success": True, "content": f"已打开 {target}"}
    except Exception as e:
        return {"success": False, "content": f"找不到或无法打开「{target}」：{e}"}


# ── 音量控制 ──

def _get_endpoint_volume():
    """（阻塞）获取默认播放设备的 EndpointVolume 接口（新版 pycaw API）。"""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def _set_volume_level(level: int) -> None:
    """（阻塞）用 pycaw 设置绝对音量 0~100。依赖缺失/失败会向上抛异常。"""
    import comtypes
    level = max(0, min(100, int(level)))
    comtypes.CoInitialize()
    try:
        _get_endpoint_volume().SetMasterVolumeLevelScalar(level / 100.0, None)
    finally:
        comtypes.CoUninitialize()


def _set_mute(mute: bool) -> None:
    """（阻塞）用 pycaw 设置静音开关。"""
    import comtypes
    comtypes.CoInitialize()
    try:
        _get_endpoint_volume().SetMute(mute, None)
    finally:
        comtypes.CoUninitialize()


async def set_volume(level: int) -> Dict[str, Any]:
    """把系统主音量设置为 0~100 的绝对值。"""
    try:
        await asyncio.to_thread(_set_volume_level, int(level))
        return {"success": True, "content": f"音量已设为 {max(0, min(100, int(level)))}%"}
    except Exception as e:
        return {"success": False, "content": f"设置音量失败（可能缺少音频设备或 pycaw 依赖）：{e}"}


async def mute_volume(mute: bool = True) -> Dict[str, Any]:
    """静音（mute=true）或取消静音（mute=false）。"""
    mute = _as_bool(mute, True)
    try:
        await asyncio.to_thread(_set_mute, mute)
        return {"success": True, "content": "已静音" if mute else "已取消静音"}
    except Exception as e:
        return {"success": False, "content": f"切换静音失败：{e}"}


# ── 截图 ──

async def screenshot(save_path: str = "") -> Dict[str, Any]:
    """截取整个屏幕并保存为 PNG，返回文件路径与尺寸。save_path 留空则自动存到生成目录。"""
    from PIL import ImageGrab
    from config_loader import config

    try:
        img = await asyncio.to_thread(ImageGrab.grab)
    except Exception as e:
        return {"success": False, "content": f"截图失败（全屏截图需在桌面会话中运行）：{e}"}

    if save_path and str(save_path).strip():
        out = str(save_path).strip()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = str(Path(config.generate_dir) / f"screenshot_{ts}.png")

    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        await asyncio.to_thread(img.save, out, "PNG")
    except Exception as e:
        return {"success": False, "content": f"截图保存失败：{e}"}

    return {
        "success": True,
        "path": out,
        "width": img.width,
        "height": img.height,
        "content": f"截图已保存到 {out}（{img.width}x{img.height}）",
    }


# ── 剪贴板 ──
# 用 pyperclip（自带重试）：Windows 剪贴板是共享资源，其他进程常短暂锁住它，
# 直接用 win32clipboard 易在连续写读时报 1418（clipboard not open）。pyperclip 已内置处理。

def _clipboard_get() -> str:
    import pyperclip
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def _clipboard_set(text: str) -> None:
    import pyperclip
    pyperclip.copy(text)


async def clipboard_read() -> Dict[str, Any]:
    """读取剪贴板中的文本内容。"""
    try:
        text = await asyncio.to_thread(_clipboard_get)
        return {"success": True, "content": text or "（剪贴板为空或不是文本）"}
    except Exception as e:
        return {"success": False, "content": f"读取剪贴板失败（可能正被其他程序占用）：{e}"}


async def clipboard_write(text: str) -> Dict[str, Any]:
    """把文本写入剪贴板。"""
    text = "" if text is None else str(text)
    try:
        await asyncio.to_thread(_clipboard_set, text)
        return {"success": True, "content": "已写入剪贴板"}
    except Exception as e:
        return {"success": False, "content": f"写入剪贴板失败：{e}"}


# ── 电源操作（敏感）──

async def power_action(action: str) -> Dict[str, Any]:
    """执行电源操作：lock（锁屏）/ sleep（睡眠）/ hibernate（休眠）/ shutdown（关机）/ restart（重启）。"""
    action = (action or "").strip().lower()
    cmd = _POWER_COMMANDS.get(action)
    if cmd is None:
        allowed = "、".join(_POWER_COMMANDS.keys())
        return {"success": False, "content": f"不支持的电源操作「{action}」，可选：{allowed}"}
    try:
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=30)
        label = {"lock": "已锁屏", "sleep": "正在睡眠", "hibernate": "正在休眠",
                 "shutdown": "正在关机", "restart": "正在重启"}[action]
        return {"success": True, "content": label}
    except Exception as e:
        return {"success": False, "content": f"电源操作失败：{e}"}


# ── 前台窗口 ──

def _active_window() -> Dict[str, Any]:
    import win32gui
    hwnd = win32gui.GetForegroundWindow()
    return {
        "title": win32gui.GetWindowText(hwnd),
        "class": win32gui.GetClassName(hwnd),
        "hwnd": hwnd,
    }


async def get_active_window() -> Dict[str, Any]:
    """获取当前前台（活动）窗口的标题与类名。"""
    try:
        info = await asyncio.to_thread(_active_window)
        info["success"] = True
        info["content"] = f"当前窗口：{info['title'] or '（无标题）'}"
        return info
    except Exception as e:
        return {"success": False, "content": f"获取前台窗口失败：{e}"}
