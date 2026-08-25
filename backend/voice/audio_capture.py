# backend/voice/audio_capture.py
# 后端麦克风采集（常驻流）。
#
# P1：sounddevice 连续采集，回调把 int16 单声道 16kHz PCM 分块交给唤醒词/VAD。
#     采集运行在 sounddevice 自身线程；跨线程回主事件循环由 assistant 用
#     asyncio.run_coroutine_threadsafe 完成。
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("My Workbench")

try:
    import sounddevice as _sd  # noqa: F401
    SOUNDDEVICE_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失
    SOUNDDEVICE_AVAILABLE = False


def backend_mic_available() -> bool:
    """后端能否直接访问麦克风（免按键唤醒的前提）。"""
    if not SOUNDDEVICE_AVAILABLE:
        return False
    try:
        _sd.query_devices()
        return True
    except Exception as e:
        logger.warning(f"[Voice] 后端麦克风不可用：{e}")
        return False


class MicStream:
    """常驻麦克风流：把音频分块回调给 on_audio(bytes)。

    约定：16kHz、单声道、int16（RawInputStream）。
    """

    def __init__(self, on_audio: Callable[[bytes], None], sample_rate: int = 16000, block_ms: int = 30):
        self.on_audio = on_audio
        self.sample_rate = sample_rate
        self.blocksize = int(sample_rate * block_ms / 1000)  # 每块样本数（如 480）
        self._stream: Optional[object] = None

    @property
    def available(self) -> bool:
        return backend_mic_available()

    def start(self):
        if not SOUNDDEVICE_AVAILABLE:
            raise RuntimeError("sounddevice 未安装，无法后端采集麦克风")
        self._stream = _sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        # indata 为 shape (frames, 1) 的 int16 numpy 数组，转原始字节回调
        try:
            self.on_audio(bytes(indata))
        except Exception as e:  # 回调内异常不能让采集崩溃
            logger.error(f"[Voice] 音频回调异常：{e}")

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"[Voice] 关闭麦克风流出错：{e}")
            self._stream = None
