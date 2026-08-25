# backend/voice/vad.py
# 语音端点检测（判断「用户说完了」）。
#
# 实现：能量端点检测（RMS + 静音超时 + 最小语音时长），零依赖、CPU 极轻。
#
# 说明：原计划用 silero-vad（ONNX），但其 PyPI 包强依赖 torch（体积大、安装重），
#      对「唤醒后检测用户是否说完」这一场景，能量端点检测已足够可靠。
#      接口与状态机解耦，后续如需更高精度可在不破坏主循环的前提下替换为 silero-vad ONNX。
from __future__ import annotations

import numpy as np


class EnergyVAD:
    """基于 RMS 的端点检测器。

    用法：唤醒后反复调用 process(pcm_chunk)，当返回 True 表示「用户已说完」，
    可把已缓存的音频交给 STT。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        speech_threshold: float = 0.008,   # RMS 阈值（满幅归一化，约 -42 dBFS）
        silence_seconds: float = 1.2,      # 静音多久判定结束
        min_speech_seconds: float = 0.15,  # 最短有效语音（过滤误触）
    ):
        self.sample_rate = sample_rate
        self.speech_threshold = speech_threshold
        self.silence_samples = int(silence_seconds * sample_rate)
        self.min_speech_samples = int(min_speech_seconds * sample_rate)
        self.reset()

    def reset(self):
        self._speech_seen = 0
        self._silent_run = 0
        self._ended = False

    @property
    def ended(self) -> bool:
        return self._ended

    def is_speech(self, pcm: bytes) -> bool:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            return False
        rms = float(np.sqrt(np.mean(arr ** 2)))
        return rms > self.speech_threshold

    def process(self, pcm: bytes) -> bool:
        """送入一段 PCM（int16 单声道），返回是否已到语音结束点。"""
        n_samples = len(pcm) // 2
        if self.is_speech(pcm):
            self._speech_seen += n_samples
            self._silent_run = 0
        else:
            if self._speech_seen > 0:
                self._silent_run += n_samples
            if self._speech_seen >= self.min_speech_samples and self._silent_run >= self.silence_samples:
                self._ended = True
        return self._ended
