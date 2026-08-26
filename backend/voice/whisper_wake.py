# backend/voice/whisper_wake.py
# 中文唤醒词 + 本地 ASR：共享一个 faster-whisper 模型实例。
#
# 背景：openWakeWord 官方仅支持英文（生成训练数据的 TTS 均为英文数据集），
#       中文唤醒词无法用其内置/自训模型直接得到。这里改走本地 ASR 路线：
#       能量 VAD 切出「有人说话」的片段 → faster-whisper 本地转写 → 匹配唤醒短语。
#       命中后，唤醒短语之后的文字即用户指令，直接交给 agent 处理。
#
# P2 复用：本模块同时提供「唤醒短语匹配」与「通用转写（本地 STT）」两个入口，
#       二者共享同一个 faster-whisper 模型（进程内缓存），避免重复加载占用内存。
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Tuple

import numpy as np

logger = logging.getLogger("My Workbench")

try:
    from faster_whisper import WhisperModel as _FW
    FASTER_WHISPER_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失
    FASTER_WHISPER_AVAILABLE = False

# 进程内共享模型缓存：唤醒词检测与本地 STT 复用同一实例（键：模型配置）
_MODEL_CACHE: dict = {}


def get_whisper_model(model_size: str = "tiny", device: str = "cpu",
                      compute_type: str = "int8", mirror: str = ""):
    """惰性加载并缓存 faster-whisper 模型（进程内共享，线程安全由调用方保证）。"""
    if not FASTER_WHISPER_AVAILABLE:
        raise RuntimeError("faster-whisper 未安装，无法启用本地 ASR")
    key = (model_size, device, compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    # 禁用 xet 下载：xet 的 CAS 服务器（cas-server.xethub.hf.co）不受镜像代理，
    # 走镜像时会 401，改用标准 HTTP 下载即可正常拉取模型。
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    if mirror:
        # 显式覆盖：用户配置了镜像时优先走镜像，而非环境里残留的 HF_ENDPOINT
        os.environ["HF_ENDPOINT"] = mirror.rstrip("/")
    model = _FW(model_size, device=device, compute_type=compute_type)
    _MODEL_CACHE[key] = model
    logger.info(f"[Voice] faster-whisper 模型已加载：{model_size}")
    return model


def _transcribe_model(model, pcm: bytes) -> str:
    """用指定模型把一段 int16 16kHz PCM 转写成中文文本（空/过短返回空串）。"""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if arr.size < 800:  # 至少约 0.05s 才送入模型
        return ""
    segments, _info = model.transcribe(
        arr,
        language="zh",
        beam_size=1,  # 贪心解码，速度快
        vad_filter=False,  # 已由上层能量 VAD 切句，避免重复 VAD 截断短句
        condition_on_previous_text=False,  # 短唤醒词/指令场景，避免前后文导致幻觉
    )
    return "".join(seg.text for seg in segments).strip()


def transcribe_pcm(pcm: bytes, model_size: str = "tiny", mirror: str = "") -> str:
    """通用本地转写（同步、阻塞）。供后台线程或 executor 调用，不要在事件循环内直接调。"""
    model = get_whisper_model(model_size=model_size, mirror=mirror)
    return _transcribe_model(model, pcm)


async def transcribe_pcm_async(pcm: bytes, model_size: str = "tiny", mirror: str = "") -> str:
    """通用本地转写（异步）：放到线程池执行，避免阻塞事件循环。"""
    return await asyncio.to_thread(transcribe_pcm, pcm, model_size, mirror)


class WhisperWakeDetector:
    """基于本地 faster-whisper 的中文唤醒词检测器（复用共享模型）。"""

    def __init__(self, model_size: str = "tiny", device: str = "cpu",
                 compute_type: str = "int8", mirror: str = ""):
        self._model = get_whisper_model(model_size, device, compute_type, mirror)

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """同步转写一段语音（阻塞；在事件循环内请用 transcribe_pcm_async）。"""
        return _transcribe_model(self._model, pcm)

    @staticmethod
    def normalize(text: str) -> str:
        """归一化：小写 + 去标点/空格，便于中文短语精确匹配。"""
        text = (text or "").strip().lower()
        text = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】\-_…~]", "", text)
        return text

    @staticmethod
    def _pinyin(s: str) -> str:
        """中文转无调拼音串，用于同音字容错；pypinyin 不可用时返回原串。"""
        try:
            from pypinyin import lazy_pinyin
            return "".join(lazy_pinyin(s))
        except Exception:
            return s

    @classmethod
    def _fuzzy_equal(cls, a: str, b: str) -> bool:
        """近似相等：先比拼音（同音字），再退化编辑距离（同形近字）。"""
        if a == b:
            return True
        if cls._pinyin(a) == cls._pinyin(b):
            return True
        try:
            from difflib import SequenceMatcher
            return SequenceMatcher(None, a, b).ratio() >= 0.8
        except Exception:
            return False

    @classmethod
    def _wake_variants(cls, wake_word: str) -> list:
        """生成待匹配的唤醒词变体（按长度降序）：完整唤醒词 + 去掉问候前缀后的核心词。

        tiny 模型常把问候语「你好」误转成「鸣好」（ní→míng），整句前缀因此对不上；
        但核心词（如「海之芯」）才是区分度所在，去掉问候前缀单独匹配即可容忍该误差。
        """
        w = cls.normalize(wake_word)
        if not w:
            return []
        variants = {w}
        core = re.sub(r"^(你好|嗨|哈喽|哈啰|hello|hi|hey|喂)\s*[,，]?\s*", "", w, flags=re.I)
        if core and core != w:
            variants.add(core)
        return sorted(variants, key=len, reverse=True)

    @classmethod
    def match_wake(cls, transcript: str, wake_word: str) -> Tuple[bool, str]:
        """匹配唤醒词，返回 (是否命中, 唤醒词之后的指令文本)。

        例：transcript="你好，海之芯，帮我查一下天气"、wake_word="你好，海之芯"
            → (True, "帮我查一下天气")

        匹配策略（对每个变体：完整词 → 核心词）：
          1) 精确包含（句首或句中均可）；
          2) 逐字符滑动窗口 + 拼音/近似模糊匹配，容忍 ASR 同音/近音误差，
             且不要求唤醒词位于句首（ASR 可能在前方混入「鸣好」等杂质）。
        """
        t = cls.normalize(transcript)
        if not t:
            return False, ""
        for w in cls._wake_variants(wake_word):
            # 1) 精确包含
            idx = t.find(w)
            if idx >= 0:
                return True, t[idx + len(w):].strip()
            # 2) 逐字符滑动窗口模糊匹配
            wlen = len(w)
            if wlen > len(t):
                continue
            for i in range(len(t) - wlen + 1):
                if cls._fuzzy_equal(t[i:i + wlen], w):
                    return True, t[i + wlen:].strip()
        return False, ""

    def close(self):
        # faster-whisper 无显式 close；释放引用即可（共享模型由缓存持有）
        pass
