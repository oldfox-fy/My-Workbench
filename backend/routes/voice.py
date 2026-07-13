# backend/routes/voice.py
"""
语音服务路由：STT（语音转文字）+ TTS（文字转语音）。

支持两种配置来源（优先级从高到低）：
  1. 请求参数中的 model / base_url / api_key（前端 useVoice 传入 audio 角色模型配置）
  2. app_config.yaml 中 voice 节的独立配置（stt_*/tts_*）
  3. 当前聊天 LLM 的 API 配置（fallback）
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
    model: Optional[str] = Form(None),
    base_url: Optional[str] = Form(""),
    api_key: Optional[str] = Form(""),
):
    """
    将音频文件转为文字。

    Args:
        file: 音频文件（支持 mp3, wav, webm, ogg 等）
        language: 语言代码（默认 "zh"）
        model: 模型名称（可选，优先级高于配置文件）
        base_url: API 地址（可选）
        api_key: API Key（可选）

    Returns:
        {"text": "识别出的文字", "language": "zh"}
    """
    if not config.voice_enabled:
        raise HTTPException(status_code=503, detail="语音服务未启用")

    client, stt_model = _get_voice_client(
        stt=True,
        override_model=model or None,
        override_base=base_url or None,
        override_key=api_key or None,
    )

    try:
        audio_bytes = await file.read()
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = file.filename or "audio.webm"

        result = await client.audio.transcriptions.create(
            model=stt_model,
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
    model: Optional[str] = None        # 覆盖配置的模型
    base_url: Optional[str] = ""       # 覆盖 API 地址
    api_key: Optional[str] = ""        # 覆盖 API Key


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
        model: 模型名称（可选，优先级高于配置文件）
        base_url: API 地址（可选）
        api_key: API Key（可选）

    Returns:
        audio/mpeg 流
    """
    if not config.voice_enabled:
        raise HTTPException(status_code=503, detail="语音服务未启用")

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    client, tts_model = _get_voice_client(
        stt=False,
        override_model=body.model or None,
        override_base=body.base_url or None,
        override_key=body.api_key or None,
    )

    try:
        voice = body.voice or config.voice_tts_voice
        text = body.text[:4096]  # OpenAI TTS 单次最大 4096 字符

        result = await client.audio.speech.create(
            model=tts_model,
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

def _get_voice_client(
    stt: bool = True,
    override_model: Optional[str] = None,
    override_base: Optional[str] = None,
    override_key: Optional[str] = None,
):
    """
    获取语音 API 客户端和模型名。

    优先级：
      1. override_* 参数（来自前端 audio 角色模型配置）
      2. app_config.yaml 中 voice 节的独立配置（stt_* / tts_*）
      3. 当前聊天 LLM 的 API 配置（fallback）
    """
    base = ""
    key = ""
    model = ""

    # 1. 请求级别的覆盖参数
    if override_model:
        model = override_model
    if override_base:
        base = override_base
    if override_key:
        key = override_key

    # 如果请求参数已完整，直接使用
    if base and key and model:
        return AsyncOpenAI(api_key=key, base_url=base), model

    # 2. voice 独立配置
    if stt:
        if not base:
            base = config.voice_stt_base_url
        if not key:
            key = config.voice_stt_api_key
        if not model:
            model = config.voice_stt_model
    else:
        if not base:
            base = config.voice_tts_base_url
        if not key:
            key = config.voice_tts_api_key
        if not model:
            model = config.voice_tts_model

    if base and key:
        return AsyncOpenAI(api_key=key, base_url=base), model

    # 3. 回退到聊天 LLM 的配置
    try:
        from backend.services.llm_service import LLMService
        from backend.utils.base import normalize_base_url
        svc = LLMService.instance
        if svc:
            llm_base = str(getattr(svc.client, "base_url", "")).rstrip("/")
            llm_key = str(getattr(svc.client, "api_key", "") or "")
            # 从 LLM client 获取 base_url 的对象属性
            llm_base_url = normalize_base_url(llm_base)
            if llm_key:
                return AsyncOpenAI(api_key=llm_key, base_url=llm_base_url), model
    except Exception:
        pass

    raise HTTPException(
        status_code=400,
        detail="语音服务未配置 API。请在 app_config.yaml 的 voice 节中设置 stt_base_url/stt_api_key，"
               "或配置一个 role=audio 的语音模型，或确保当前聊天模型支持语音 API（如 OpenAI）。"
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
