# backend/routes/assistant.py
# 语音助手 HTTP + WebSocket 端点。
#
# P0：
#   - POST /api/assistant/ask   （音频或文本 → 识别/路由 → 文本回答，供前端 TTS 播报）
#   - GET  /api/assistant/state （状态查询，供前端轮询/徽标）
#   - WS   /ws/assistant        （状态推送通道，P1 唤醒后主窗口/悬浮球订阅）
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger("My Workbench")

from backend.routes.voice import _get_voice_client
from config_loader import config

from ..voice.assistant import get_assistant
from ..voice.state import connection_manager

router = APIRouter(prefix="/api", tags=["assistant"])


# ── 请求/响应模型 ──

class AssistantAskResponse(BaseModel):
    transcript: str
    answer: str
    state: str
    spoken_locally: bool = False


# ── 音频识别（复用 voice.py 的配置优先级：请求参数 > app_config > 聊天 LLM）──

async def _transcribe(file: UploadFile, language: str,
                      model: Optional[str], base_url: Optional[str], api_key: Optional[str]) -> str:
    import io
    if not config.voice_enabled:
        return ""
    client, stt_model = _get_voice_client(
        stt=True,
        override_model=model or None,
        override_base=base_url or None,
        override_key=api_key or None,
    )
    audio_bytes = await file.read()
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = file.filename or "audio.webm"
    result = await client.audio.transcriptions.create(
        model=stt_model,
        file=audio_file,
        language=language,
    )
    return result.text or ""


# ── 核心：一条语音指令的全链路 ──

@router.post("/assistant/ask", response_model=AssistantAskResponse)
async def assistant_ask(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    language: Optional[str] = Form("zh"),
    model: Optional[str] = Form(None),
    base_url: Optional[str] = Form(""),
    api_key: Optional[str] = Form(""),
):
    """语音助手入口：传入音频（或直接传文字）→ ASR → 意图路由 → Agent → 文本回答。

    前端拿到 answer 后，走现有 useTTS 播报（P2 才切后端本地出声）。
    """
    assistant = get_assistant()

    transcript = (text or "").strip()
    if not transcript and file is not None:
        try:
            transcript = await _transcribe(file, language or "zh", model, base_url, api_key)
        except Exception as e:
            return AssistantAskResponse(
                transcript="", answer=f"语音识别失败：{e}", state=assistant.state
            )

    transcript = transcript.strip()
    if not transcript:
        return AssistantAskResponse(transcript="", answer="我没听清，请再说一遍。", state=assistant.state)

    answer = await assistant.ask(transcript)
    spoken = await assistant.speak_answer(answer)
    return AssistantAskResponse(transcript=transcript, answer=answer, state=assistant.state, spoken_locally=spoken)


# ── 状态查询 ──

@router.get("/assistant/state")
async def assistant_state():
    assistant = get_assistant()
    from backend.voice.audio_capture import backend_mic_available
    return {
        "enabled": assistant.enabled,
        "state": assistant.state,
        "backend_mic_available": backend_mic_available(),
        "wake_available": assistant.wake_available,
    }


# ── 语音助手配置（P4：语音设置面板读写）──

class VoiceConfigUpdate(BaseModel):
    """语音助手可调项（全部可选，只更新传入的字段）。"""
    enabled: Optional[bool] = None
    wake_mode: Optional[str] = None
    wake_word: Optional[str] = None
    whisper_model: Optional[str] = None
    whisper_mirror: Optional[str] = None
    silence_timeout: Optional[float] = None
    max_listen_seconds: Optional[int] = None
    stt_engine: Optional[str] = None
    tts_engine: Optional[str] = None
    edge_voice: Optional[str] = None
    edge_rate: Optional[str] = None


def _assistant_config_payload():
    from backend.voice.settings import get_settings
    assistant = get_assistant()
    return {
        "config": get_settings().effective(),
        "enabled": assistant.enabled,
        "state": assistant.state,
        "wake_available": assistant.wake_available,
    }


@router.get("/assistant/config")
async def assistant_get_config():
    return _assistant_config_payload()


@router.put("/assistant/config")
async def assistant_put_config(payload: VoiceConfigUpdate):
    """写入语音设置：持久化 overlay → 应用 config → 重启助手 → 重置 TTS，实时生效。"""
    from backend.voice.settings import get_settings
    from backend.voice.tts import reset_tts

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if updates:
        get_settings().update(updates)
        assistant = get_assistant()
        try:
            await assistant.stop()
        except Exception as e:
            logger.warning(f"[Voice] 停止助手失败：{e}")
        assistant.tts = None
        reset_tts()  # 让新的 edge 音色/语速在下次播报生效
        try:
            await assistant.start()
        except Exception as e:
            logger.warning(f"[Voice] 重启助手失败：{e}")

    return _assistant_config_payload()


# ── WebSocket 状态推送（P1 唤醒后前端订阅）──
# 注：ws 端点在 main.py 中单独注册（app.websocket("/ws/assistant")），
#     这里只定义处理函数，不再挂到 router 上（避免与 /api 前缀冲突）。

async def ws_assistant(websocket: WebSocket):
    await connection_manager.connect(websocket)
    try:
        # 连接后立即推送一次当前状态，让悬浮球/主窗口在无状态变化时也能正确显示（如已暂停）
        assistant = get_assistant()
        await websocket.send_json({
            "type": "state",
            "state": assistant.state,
            "enabled": assistant.enabled,
            "wake_available": assistant.wake_available,
        })
        while True:
            # P0 阶段只做服务端推送，不处理客户端指令；接收消息仅为保活/防阻塞。
            await websocket.receive_text()
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
    except Exception:
        connection_manager.disconnect(websocket)
