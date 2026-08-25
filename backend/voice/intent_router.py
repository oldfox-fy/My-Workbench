# backend/voice/intent_router.py
# 语音意图「快/慢」双通道路由。
#
# 快速通道：高频、参数固定的本地指令，本地规则匹配 + 直接调函数，
#           避免 LLM 首 token 延迟拖垮语音体验。
# 慢速通道：其余自然语言需求，走完整 LLM Agent（复用 llm_service）。
#
# 已接入快通道：
#   - P2：定时/提醒（中文时间短语解析 → ReminderStore）
#   - P3：打开应用/文件/网址、截图、静音/取消静音、音量绝对值
#          （关机/重启等敏感电源操作仍走慢通道，受工具审批保护）
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("My Workbench")


@dataclass
class FastIntentResult:
    """快通道命中结果：直接给出一句可播报的回答文本。"""
    answer: str


def _fmt_interval(secs: int) -> str:
    if secs % 3600 == 0:
        return f"每 {secs // 3600} 小时"
    if secs % 60 == 0:
        return f"每 {secs // 60} 分钟"
    return f"每 {secs} 秒"


# ── 快通道匹配器 ──

# 打开/启动/运行/开启 X（前缀型）
_OPEN_PREFIX_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我|给)?\s*(?:打开|启动|运行|开启|打开一下|开一下|打开一个)"
    r"\s*(?:一个|个|一下|下)?\s*(.+?)[，。！？、,.!?~…\s]*$"
)
# 把 X 打开（后置型）
_OPEN_BA_RE = re.compile(
    r"^(?:请|麻烦|帮我)?\s*把\s*(.+?)\s*(?:打开|启动|运行|开启)\s*[，。！？、,.!?~…\s]*$"
)
# 截图
_SHOT_RE = re.compile(r"截图|截屏|截个图|截一张图|保存截图|屏幕截图")
# 静音 / 取消静音（先查「取消」，避免"取消静音"被"静音"误命中）
_MUTE_RE = re.compile(r"静音|消音|关(?:掉|闭)?声音|关(?:掉|闭)?音量|静个音")
_UNMUTE_RE = re.compile(r"取消静音|解除静音|恢复声音|打开声音|开启声音")
# 音量绝对值：音量/声音 调到/设为 X（支持百分比与中文数字）
_VOLUME_SET_RE = re.compile(
    r"(?:音量|声音).*?(?:调到|设为|设置成|设成|开到)\s*(?:百分之)?\s*([0-9]{1,3}|[零〇一二两三四五六七八九十]+)"
)


def _strip(text: str) -> str:
    return (text or "").strip(" ，。！？、,.!?~…·")


def _extract_open_target(text: str) -> Optional[str]:
    """从「打开 X」「把 X 打开」等句式里提取目标（应用/文件/网址），未命中返回 None。"""
    m = _OPEN_PREFIX_RE.match(text)
    if m:
        return _strip(m.group(1))
    m = _OPEN_BA_RE.match(text)
    if m:
        return _strip(m.group(1))
    return None


async def try_fast_route(text: str) -> Optional[FastIntentResult]:
    """尝试走本地快速通道。返回 None 表示未命中，需要交给 LLM Agent。"""
    text = (text or "").strip()
    if not text:
        return None

    from .reminders import parse_reminder, get_store, _parse_number

    # 1. 定时/提醒
    draft = parse_reminder(text)
    if draft is not None:
        reminder = await asyncio.to_thread(
            get_store().add,
            draft["content"], draft["due_ts"], draft["recurring_seconds"],
        )
        if reminder["recurring_seconds"] > 0:
            answer = f"好的，已设置{_fmt_interval(reminder['recurring_seconds'])}提醒你：{reminder['content']}"
        else:
            when = datetime.fromtimestamp(reminder["due_ts"]).strftime("%m月%d日 %H:%M")
            answer = f"好的，{when} 我会提醒你：{reminder['content']}"
        return FastIntentResult(answer=answer)

    # 2. 打开应用 / 文件 / 网址（仅命中已知目标才抢答，避免「打开音乐」这类被误判）
    target = _extract_open_target(text)
    if target:
        from backend.system_tools.system_control import open_app, _APP_ALIASES
        is_known = (
            target.lower() in _APP_ALIASES
            or bool(re.match(r"^(?:https?://|www\.)", target, re.I))
            or os.path.exists(target)
        )
        if is_known:
            result = await open_app(target)
            return FastIntentResult(answer=result["content"])

    # 3. 截图
    if _SHOT_RE.search(text):
        from backend.system_tools.system_control import screenshot
        result = await screenshot()
        if result["success"]:
            return FastIntentResult(answer="好的，已截图保存到生成目录")
        return FastIntentResult(answer=result["content"])

    # 4. 静音 / 取消静音
    if _UNMUTE_RE.search(text):
        from backend.system_tools.system_control import mute_volume
        result = await mute_volume(False)
        return FastIntentResult(answer=result["content"])
    if _MUTE_RE.search(text):
        from backend.system_tools.system_control import mute_volume
        result = await mute_volume(True)
        return FastIntentResult(answer=result["content"])

    # 5. 音量绝对值（音量/声音 调到 X）
    m = _VOLUME_SET_RE.search(text)
    if m:
        num = _parse_number(m.group(1))
        if num is not None and 0 <= num <= 100:
            from backend.system_tools.system_control import set_volume
            result = await set_volume(num)
            return FastIntentResult(answer=result["content"])

    return None
