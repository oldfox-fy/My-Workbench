# backend/voice/settings.py
# 语音助手设置（P4）：JSON overlay 持久化 + 运行时生效。
#
# 设计：
#   - app_config.yaml 在打包后是只读的（位于 _MEIPASS 内），用户改动无法写回；
#     因此把语音相关的「可调项」另存到 data_dir/voice_settings.json，启动时
#     overlay 到 config.voice_assistant_* 属性，运行时改设置也直接改这些属性。
#   - 字段与 config.voice_assistant_* 一一对应；类型转换在此收敛，避免前端
#     传错类型导致助手启动失败。
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("My Workbench")

# 可调字段：JSON key -> (config 属性名, 类型)。str 之外的字段在写入时做类型转换。
_FIELDS = {
    "enabled": ("voice_assistant_enabled", bool),
    "wake_mode": ("voice_assistant_wake_mode", str),
    "wake_word": ("voice_assistant_wake_word", str),
    "whisper_model": ("voice_assistant_whisper_model", str),
    "whisper_mirror": ("voice_assistant_whisper_mirror", str),
    "silence_timeout": ("voice_assistant_silence_timeout", float),
    "max_listen_seconds": ("voice_assistant_max_listen_seconds", int),
    "stt_engine": ("voice_assistant_stt_engine", str),
    "tts_engine": ("voice_assistant_tts_engine", str),
    "edge_voice": ("voice_assistant_edge_voice", str),
    "edge_rate": ("voice_assistant_edge_rate", str),
}


def _coerce(key: str, value: Any) -> Any:
    """按字段声明类型把外部传入的值收敛为正确类型。"""
    _, typ = _FIELDS[key]
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "是", "开")
        return bool(value)
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    return str(value)


class VoiceSettingsStore:
    """语音设置的持久化存储：JSON overlay + 运行时应用到 config。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._overlay: Dict[str, Any] = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._overlay = {k: v for k, v in data.items() if k in _FIELDS}
        except Exception as e:
            logger.warning(f"[Voice] 读取语音设置失败，使用默认值：{e}")
            self._overlay = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._overlay, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[Voice] 保存语音设置失败：{e}")

    def effective(self) -> Dict[str, Any]:
        """返回当前生效的全部字段（config 现值，已含 overlay 覆盖）。"""
        from config_loader import config
        return {key: getattr(config, attr) for key, (attr, _) in _FIELDS.items()}

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """合并更新 → 持久化 overlay → 应用到 config 属性，返回生效后的全部字段。"""
        from config_loader import config
        with self._lock:
            for key, value in updates.items():
                if key not in _FIELDS:
                    continue
                self._overlay[key] = _coerce(key, value)
            self._save()
            for key, value in self._overlay.items():
                attr, _ = _FIELDS[key]
                setattr(config, attr, value)
        return self.effective()

    def apply_to_config(self):
        """启动时把 overlay 应用到 config（在语音助手 start 之前调用一次）。"""
        from config_loader import config
        with self._lock:
            for key, value in self._overlay.items():
                attr, _ = _FIELDS[key]
                setattr(config, attr, value)


# 进程内单例
_store: Optional[VoiceSettingsStore] = None


def get_settings() -> VoiceSettingsStore:
    global _store
    if _store is None:
        from config_loader import config
        path = os.path.join(str(config.data_dir), "voice_settings.json")
        _store = VoiceSettingsStore(path)
    return _store


def apply_voice_settings():
    """启动时调用：把持久化的语音设置 overlay 到 config。"""
    get_settings().apply_to_config()
