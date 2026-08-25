# backend/voice/tts.py
# TTS 引擎（P2）：edge-tts 合成（免费自然中文音色）+ pygame 本地播报。
#
# P0 阶段：语音合成在前端 useTTS 完成（HTMLAudioElement / speechSynthesis），
#          「播放」由前端承担，因为 P0 触发点是按键、有前端在旁。
# P2 阶段：后端用 edge-tts 合成 mp3、pygame 本地出声——免按键唤醒 / 定时提醒时
#          即使主窗口最小化也能播报，并支持打断（stop）。
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time

logger = logging.getLogger("My Workbench")

# 抑制 pygame 的社区提示横幅
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失
    EDGE_TTS_AVAILABLE = False


class TTSEngine:
    """本地 TTS 引擎：edge-tts 合成 + pygame 播放（支持打断）。"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%"):
        self._voice = voice
        self._rate = rate
        self._speaking = False
        self._gen = 0          # 代数计数器：stop 一次 +1，用于打断在途合成/播放
        self._pygame = None
        self._pygame_ok = True

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def synthesize(self, text: str, voice: str = "", rate: str = "") -> bytes:
        """用 edge-tts 合成文本，返回 mp3 字节。"""
        if not EDGE_TTS_AVAILABLE:
            raise RuntimeError("edge-tts 未安装")
        communicate = edge_tts.Communicate(text, voice or self._voice, rate=rate or self._rate)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    def _ensure_pygame(self):
        if self._pygame is not None:
            return self._pygame
        if not self._pygame_ok:
            return None
        try:
            import pygame
            pygame.mixer.init()
            self._pygame = pygame
            return pygame
        except Exception as e:
            logger.warning(f"[Voice] pygame 初始化失败，退回前端播报：{e}")
            self._pygame_ok = False
            return None

    def _play_blocking(self, audio: bytes, gen: int):
        """阻塞播放（供 asyncio.to_thread 调用；gen 用于判断是否已被打断）。"""
        pygame = self._ensure_pygame()
        if pygame is None:
            return
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(audio)
                tmp_path = f.name
            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while gen == self._gen and pygame.mixer.music.get_busy():
                time.sleep(0.05)
        except Exception as e:
            logger.warning(f"[Voice] 本地播报失败：{e}")
        finally:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    async def speak_async(self, text: str):
        """合成并本地播报（异步、可被 stop 打断）。失败时静默退回前端播报。"""
        text = (text or "").strip()
        if not text:
            return
        self.stop()  # 打断上一句
        gen = self._gen
        try:
            audio = await self.synthesize(text)
        except Exception as e:
            logger.warning(f"[Voice] edge-tts 合成失败：{e}")
            return
        if gen != self._gen:
            return  # 合成期间被 stop 打断，不再播报
        self._speaking = True
        try:
            await asyncio.to_thread(self._play_blocking, audio, gen)
        finally:
            if gen == self._gen:
                self._speaking = False

    def stop(self):
        """打断当前合成/播放。"""
        self._gen += 1
        self._speaking = False
        if self._pygame is not None:
            try:
                self._pygame.mixer.music.stop()
            except Exception:
                pass


# 进程内单例（懒加载，读取配置的 voice/rate）
_tts: "TTSEngine | None" = None


def get_tts() -> TTSEngine:
    global _tts
    if _tts is None:
        from config_loader import config
        _tts = TTSEngine(
            voice=getattr(config, "voice_assistant_edge_voice", "zh-CN-XiaoxiaoNeural"),
            rate=getattr(config, "voice_assistant_edge_rate", "+0%"),
        )
    return _tts


def reset_tts():
    """丢弃缓存的 TTS 单例，让下次 get_tts() 按新配置重建（P4 改音色/语速后生效）。"""
    global _tts
    if _tts is not None:
        try:
            _tts.stop()
        except Exception:
            pass
    _tts = None
