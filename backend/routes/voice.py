# backend/routes/voice.py
"""
语音服务路由：STT（语音转文字）+ TTS（文字转语音）。

使用 OpenAI 兼容协议，支持任何兼容的 STT/TTS 端点：
  - STT: /v1/audio/transcriptions（whisper-1 或兼容模型）
  - TTS: /v1/audio/speech（tts-1 / tts-1-hd 或兼容模型）

前端调用方式：
  - STT: 录制音频 → FormData 上传 → POST /api/stt → 返回 {text: "..."}
  - TTS: 发送文本 → POST /api/tts → 返回 audio/mpeg 流 → <audio> 播放
"""
import io
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from openai import AsyncOpenAI

from config_loader import config

router = APIRouter(prefix="/api", tags=["voice"])


# ──────────────────── STT 语音转文字 ────────────────────

@router.post("/stt")
async def speech_to_text(
    file: UploadFile = File(...),
    language: Optional[str] = Form("zh"),
):
    """
    将音频文件转为文字。

    Args:
        file: 音频文件（支持 mp3, wav, webm, ogg 等）
        language: 语言代码（默认 "zh"）

    Returns:
        {"text": "识别出的文字", "language": "zh"}
    """
    if not config.voice_enabled:
        raise HTTPException(status_code=503, detail="语音服务未启用")

    client = _get_voice_client(stt=True)

    try:
        audio_bytes = await file.read()
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = file.filename or "audio.webm"

        result = await client.audio.transcriptions.create(
            model=config.voice_stt_model,
            file=audio_file,
            language=language,
        )
        return {"text": result.text, "language": language}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"语音识别失败：{str(e)}")


# ──────────────────── TTS 文字转语音 ────────────────────

class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: Optional[float] = 1.0


@router.post("/tts")
async def text_to_speech(
    body: TTSRequest,
):
    """
    将文字转为语音。

    Args:
        text: 要朗读的文字（最大 4096 字符）
        voice: 音色（alloy/echo/fable/onyx/nova/shimmer），默认使用 app_config 配置
        speed: 语速（0.25 ~ 4.0）

    Returns:
        audio/mpeg 流
    """
    if not config.voice_enabled:
        raise HTTPException(status_code=503, detail="语音服务未启用")

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    client = _get_voice_client(stt=False)

    try:
        voice = body.voice or config.voice_tts_voice
        text = body.text[:4096]  # OpenAI TTS 单次最大 4096 字符

        result = await client.audio.speech.create(
            model=config.voice_tts_model,
            voice=voice,
            input=text,
            speed=body.speed or 1.0,
            response_format="mp3",
        )

        # 将音频流返回给前端
        return StreamingResponse(
            io.BytesIO(result.content),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"语音合成失败：{str(e)}")


# ──────────────────── 辅助 ────────────────────

def _get_voice_client(stt: bool = True):
    """
    获取语音 API 客户端。

    优先使用 app_config.yaml 中 voice 节配置的独立 API（stt_* / tts_*），
    若未配置则回退到当前聊天 LLM 的 API 配置。
    这解决了 DeepSeek 等不含语音 API 的模型需要单独配置语音端点的问题。
    """
    # 1. 先尝试 voice 独立配置
    if stt:
        base = config.voice_stt_base_url
        key = config.voice_stt_api_key
    else:
        base = config.voice_tts_base_url
        key = config.voice_tts_api_key

    if base and key:
        return AsyncOpenAI(api_key=key, base_url=base)

    # 2. 回退到聊天 LLM 的配置（适用于 OpenAI 等同时支持语音的提供商）
    try:
        from backend.services.llm_service import LLMService
        svc = LLMService.instance
        if svc:
            llm_base = str(getattr(svc.client, "base_url", ""))
            llm_key = getattr(svc.client, "api_key", "")
            if llm_key:
                return AsyncOpenAI(api_key=llm_key, base_url=llm_base)
    except Exception:
        pass

    raise HTTPException(
        status_code=400,
        detail="语音服务未配置 API。请在 app_config.yaml 的 voice 节中设置 stt_base_url/stt_api_key，"
               "或确保当前聊天模型支持语音 API（如 OpenAI）。"
    )


@router.get("/voice-config")
async def get_voice_config():
    """获取当前语音配置（供前端判断是否启用语音功能）。"""
    return {
        "enabled": config.voice_enabled,
        "stt_model": config.voice_stt_model,
        "tts_model": config.voice_tts_model,
        "tts_voice": config.voice_tts_voice,
    }
