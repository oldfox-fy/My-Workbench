# backend/voice/state.py
# 语音助手的状态枚举 + 向前端主动推送的连接管理器（进程内广播）。
#
# 现状：项目里 /ws/chat 是「每连接独立、只回发自己」，没有广播通道（见 ws_chat.py）。
# 语音助手需要主动把「状态变化 / 提醒触发」推给所有前端（主窗口 + 悬浮球），
# 所以这里实现一个极简 ConnectionManager，参考 llm_service.py 的进程内 dict 模式。
from enum import Enum
from typing import Set

from fastapi import WebSocket


class AssistantState(str, Enum):
    IDLE = "idle"           # 空闲（监听/待命）
    LISTENING = "listening" # 正在收音
    THINKING = "thinking"   # ASR + 意图路由中
    ACTING = "acting"       # Agent 工具执行中
    SPEAKING = "speaking"   # TTS 播报中
    ERROR = "error"         # 出错


class VoiceConnectionManager:
    """维护所有已连接的 /ws/assistant 客户端，支持广播 JSON 消息。"""

    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, payload: dict):
        """向所有连接推送，失效连接自动剔除。"""
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# 进程内单例（与 app.state 双轨：模块级便于 assistant 内部调用）
connection_manager = VoiceConnectionManager()
