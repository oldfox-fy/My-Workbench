# backend/voice/__init__.py
# 语音助手子系统（P0：按键触发全链路；P1：唤醒词 + VAD；P2：定时/提醒 + 本地引擎）
from .state import AssistantState, VoiceConnectionManager

__all__ = ["AssistantState", "VoiceConnectionManager"]
