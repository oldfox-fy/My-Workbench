# backend/voice/wake_word.py
# 唤醒词引擎抽象 + openWakeWord 实现（ONNX，本地免费）。
#
# 注意：openWakeWord 内置预训练模型均为英文（alexa/hey_jarvis/hey_mycroft/...），
#       无内置中文「小爱同学」。因此：
#         - 默认按配置的唤醒词尝试匹配内置模型名；
#         - 匹配不到时回退到英文 "alexa_v0.1" 以先验证整条链路；
#         - 支持通过 app_config 的 wake_model_path 指定自定义中文 ONNX 模型
#           （可用 openWakeWord 训练脚本自训「小爱同学」，无需改代码）。
#
# 另一要点：openWakeWord 的预训练模型不随 pip 包分发，首次使用需从 GitHub 下载
#（含 melspectrogram / embedding 特征模型）。这里在加载前幂等补齐（已存在则跳过）。
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("My Workbench")

try:
    import openwakeword as _oww
    OPENWAKEWORD_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失
    OPENWAKEWORD_AVAILABLE = False


class WakeWordEngine:
    """唤醒词引擎接口。"""

    def detect(self, pcm: bytes, sample_rate: int = 16000) -> bool:
        raise NotImplementedError

    def close(self):
        pass


class OpenWakeWordEngine(WakeWordEngine):
    """基于 openWakeWord 的唤醒词引擎（16kHz int16 单声道 PCM 输入）。"""

    def __init__(self, model_name: str = "", model_path: str = "", threshold: float = 0.5,
                 mirror: str = "", timeout: int = 30):
        if not OPENWAKEWORD_AVAILABLE:
            raise RuntimeError("openwakeword 未安装，无法启用唤醒词")
        self._threshold = threshold
        self._load_path = self._resolve_model(model_name, model_path)
        # 预训练模型不随包分发，加载前幂等下载（含特征模型）。网络不可用则抛异常，
        # 由 assistant.start 捕获后退回按键模式，不影响主流程。
        self._ensure_models(self._load_path, mirror=mirror, timeout=timeout)
        self._model = _oww.Model(wakeword_models=[self._load_path], inference_framework="onnx")
        logger.info(f"[Voice] 唤醒词引擎已加载模型：{self._load_path}")

    @staticmethod
    def _resolve_model(model_name: str, model_path: str) -> str:
        # 1. 自定义模型文件优先
        if model_path and os.path.exists(model_path):
            return model_path

        # 2. 尝试匹配内置模型名（前缀/包含匹配，如 "alexa" → "alexa_v0.1"）
        pretrained = _oww.get_pretrained_model_paths("onnx")
        builtin = {os.path.splitext(os.path.basename(p))[0]: p for p in pretrained}
        key = (model_name or "").strip().lower().replace(" ", "_")
        for name in builtin:
            if key and key in name.lower():
                return name

        # 3. 回退英文模型，先跑通链路
        fallback = next((n for n in builtin if n.startswith("alexa")), None) or list(builtin)[0]
        logger.warning(
            f"[Voice] 未找到唤醒词「{model_name}」对应的内置模型，回退到英文「{fallback}」。"
            f"如需中文唤醒词，请自训 ONNX 模型并在 app_config.yaml 的 voice_assistant.wake_model_path 指定。"
        )
        return fallback

    @staticmethod
    def _ensure_models(load_path: str, mirror: str = "", timeout: int = 30):
        """确保唤醒词模型 + 特征模型已存在于本地（幂等、带超时重试、可选镜像）。

        openWakeWord 官方 download_models 无超时、无镜像，在网络差的环境会长时间挂起，
        故这里自行实现下载：写入 .part 临时文件 + 成功后原子替换，失败可安全重试。
        """
        target_dir = os.path.join(os.path.dirname(_oww.__file__), "resources", "models")
        os.makedirs(target_dir, exist_ok=True)

        # 需要下载的文件 URL：特征模型 + VAD + 目标唤醒词模型
        urls = []
        for mdl in _oww.FEATURE_MODELS.values():
            urls.append(mdl["download_url"])
            urls.append(mdl["download_url"].replace(".tflite", ".onnx"))
        for mdl in getattr(_oww, "VAD_MODELS", {}).values():
            urls.append(mdl["download_url"])  # silero_vad 仅 onnx
        if not os.path.exists(load_path):
            # 内置模型名（如 alexa_v0.1）→ 下载该模型（tflite + onnx）
            for mdl in _oww.MODELS.values():
                if load_path in mdl["download_url"].split("/")[-1]:
                    urls.append(mdl["download_url"])
                    urls.append(mdl["download_url"].replace(".tflite", ".onnx"))
                    break

        for u in urls:
            dest = os.path.join(target_dir, u.split("/")[-1])
            if os.path.exists(dest):
                continue
            OpenWakeWordEngine._download_with_retry(
                (mirror.rstrip("/") + "/" + u) if mirror else u,
                dest, timeout=timeout,
            )

    @staticmethod
    def _download_with_retry(url: str, dest: str, timeout: int = 30, retries: int = 3):
        """带超时与重试的下载（写入 .part 临时文件，成功后原子替换）。"""
        import time
        import requests
        last = None
        for attempt in range(retries):
            try:
                with requests.get(url, stream=True, timeout=timeout) as r:
                    r.raise_for_status()
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            if chunk:
                                f.write(chunk)
                os.replace(tmp, dest)
                return
            except Exception as e:  # noqa: BLE001 - 网络异常统一重试
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"下载模型失败（{url}）：{last}")

    def detect(self, pcm: bytes, sample_rate: int = 16000) -> bool:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size < 1600:  # 至少约 0.1s 才送入模型
            return False
        preds = self._model.predict(arr)
        if not preds:
            return False
        score = float(max(preds.values()))
        return score >= self._threshold

    def close(self):
        try:
            if getattr(self, "_model", None) is not None:
                self._model.reset()
        except Exception:
            pass
