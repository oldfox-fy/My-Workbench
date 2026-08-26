# backend/voice/assistant.py
# 语音助手核心编排：状态机 + 意图路由 + 复用 LLM Agent。
#
# P0：按键触发（前端录音 → /api/assistant/ask → 文本回答 → 前端 TTS 播报）。
# P1：免按键唤醒（openWakeWord 常驻监听 → 能量端点检测 → STT → 路由 → 播报）。
#
# 复用关系（不重写现有逻辑）：
#   - ASR/TTS 复用 backend/routes/voice.py 的 _get_voice_client（OpenAI 兼容）
#   - Agent 推理复用 backend/services/llm_service.py 的 generate_response
#   - 工具集复用 backend/services/tools.py 的 get_all_tools
#   - 模型解析复用 backend/services/model_router.py 的 get_default_model
from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from typing import List, Optional

from .state import AssistantState, connection_manager

logger = logging.getLogger("My Workbench")

# 纯唤醒词（未跟指令）时，先语音回应的问候语，随后进入聆听等待下一句指令。
_WAKE_GREETING = "您好，有什么可以帮您？"

# 语音播报时需过滤的过程性输出前缀（工具执行状态 / 重试 / 失败提示等），只保留最终答案。
_PROCESS_MARKERS = ("⚠️", "🔄", "✅", "❌", "⏳", "⏰", "🎬", "📋", "🖼️")


class VoiceAssistant:
    """语音助手单例。P0 提供 ask() 按键触发；P1 增加免按键唤醒主循环。"""

    def __init__(self):
        self._state = AssistantState.IDLE
        self.tts = None  # P2 阶段挂载 TTSEngine

        # P1 唤醒监听相关（在 start() 中初始化）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_engine = None
        self._vad = None
        self._mic = None
        self._listening = False          # 已唤醒、正在收音
        self._buffer: List[bytes] = []
        self._cooldown_blocks = 0        # 唤醒冷却（避免尾音误触）
        self._max_blocks = 0             # 单句最大块数
        self._stopping = False           # 停止标志（协调后台启动线程）

        # P1.5 中文唤醒词（whisper 模式）
        self._wake_mode = None           # None | "oww" | "whisper"
        self._whisper = None             # whisper 模式的中文唤醒检测器
        self._awaiting_command = False   # whisper 模式：纯唤醒词后等待下一句指令
        self._speech_active = False      # whisper 模式：正在收音的片段
        self._wake_word_text = ""        # 配置的唤醒词原文
        self._bootstrap_gen = 0          # 启动代数：改设置重启时丢弃过期 bootstrap 线程的写回

    # ── 状态 ──
    @property
    def state(self) -> str:
        return self._state.value

    @property
    def enabled(self) -> bool:
        """是否启用语音助手（含免按键唤醒）。读取配置，保证与设置面板一致。"""
        from config_loader import config
        return bool(getattr(config, "voice_assistant_enabled", True))

    @property
    def wake_available(self) -> bool:
        """免按键唤醒是否可用（已启用 + 麦克风可用 + 引擎加载成功）。"""
        if not self.enabled:
            return False
        if self._mic is None:
            return False
        if self._wake_mode == "whisper":
            return self._whisper is not None
        return self._wake_engine is not None

    async def _set_state(self, state: AssistantState):
        self._state = state
        await connection_manager.broadcast({
            "type": "state",
            "state": state.value,
            "enabled": self.enabled,
            "wake_available": self.wake_available,
        })

    def _schedule(self, coro):
        """把协程调度到主事件循环（在 sounddevice 线程里调用）。"""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── P2 本地引擎开关 ──
    def _use_local_stt(self) -> bool:
        from config_loader import config
        return (getattr(config, "voice_assistant_stt_engine", "local") or "local") == "local"

    def _use_local_tts(self) -> bool:
        from config_loader import config
        return (getattr(config, "voice_assistant_tts_engine", "edge") or "edge") == "edge"

    async def speak_answer(self, answer: str) -> bool:
        """edge 模式下后端本地播报；返回是否已本地播报（前端据此避免重复播报）。"""
        answer = (answer or "").strip()
        if not answer or not self._use_local_tts():
            return False
        if self.tts is None:
            from .tts import get_tts
            self.tts = get_tts()
        await self._set_state(AssistantState.SPEAKING)
        try:
            await self.tts.speak_async(answer)
        finally:
            await self._set_state(AssistantState.IDLE)
        return True

    async def stop_speaking(self) -> None:
        """打断当前播报（悬浮球播报中点击调用）。"""
        from .tts import get_tts
        try:
            get_tts().stop()
        except Exception as e:
            logger.warning(f"[Voice] 停止播报失败：{e}")
        # speak_answer 的 finally 会把 SPEAKING 改回 IDLE；这里兜底处理并发时序
        if self._state == AssistantState.SPEAKING:
            await self._set_state(AssistantState.IDLE)

    async def wake_manually(self) -> bool:
        """按键/悬浮球手动唤醒：进入聆听，等下一句指令直接对话。

        复用 whisper 模式的 _awaiting_command / oww 模式的 _listening，
        使「点击唤醒」与「说唤醒词后等指令」走同一套后续处理链路。
        """
        if not self.enabled:
            return False
        # 打断在途播报，避免把问候/回答的尾音当新输入
        from .tts import get_tts
        try:
            get_tts().stop()
        except Exception:
            pass
        self._cooldown_blocks = 0
        self._buffer = []
        self._speech_active = False
        if self._vad is not None:
            try:
                self._vad.reset()
            except Exception:
                pass
        if self._wake_mode == "oww":
            self._listening = True
        else:
            self._awaiting_command = True
        await self._set_state(AssistantState.LISTENING)
        return True

    async def set_enabled(self, enabled: bool) -> None:
        """启用/停用语音助手（悬浮球右键菜单 / 设置面板共用）。实时重启生效。"""
        from .settings import get_settings
        from .tts import reset_tts
        get_settings().update({"enabled": enabled})
        try:
            await self.stop()
        except Exception as e:
            logger.warning(f"[Voice] 停止助手失败：{e}")
        self.tts = None
        reset_tts()  # 让新的音色/语速在下次播报生效
        try:
            await self.start()
        except Exception as e:
            logger.warning(f"[Voice] 重启助手失败：{e}")

    # ── 主入口（按键触发 / 唤醒触发共用）──
    async def ask(self, text: str) -> str:
        """处理一句已识别的用户指令，返回可播报的回答文本。"""
        text = (text or "").strip()
        if not text:
            return "我没听清，请再说一遍。"

        await self._set_state(AssistantState.THINKING)
        try:
            # 快通道（P0 恒为 None，P2 接定时/音量等本地规则）
            from .intent_router import try_fast_route
            fast = await try_fast_route(text)
            if fast is not None:
                return fast.answer

            # 慢通道：完整 LLM Agent
            await self._set_state(AssistantState.ACTING)
            answer = await self._agent_ask(text)
            return answer or "抱歉，我没能给出回答。"
        except Exception as e:
            logger.error(f"[Voice] 处理指令失败：{e}", exc_info=True)
            await self._set_state(AssistantState.ERROR)
            return f"出错了：{e}"
        finally:
            await self._set_state(AssistantState.IDLE)

    # ── 慢通道：LLM Agent ──
    async def _agent_ask(self, text: str) -> str:
        """用与文字对话一致的 Agent 能力回答用户（带工具、MCP、技能）。"""
        # 延迟导入，避免模块级循环依赖（chat 路由在 app 启动后才会 import voice）
        from backend.services.tools import get_all_tools
        from backend.services.model_router import get_default_model

        # 1. 解析模型：按 default 角色取（LLMService.instance 未被任何路径赋值，
        #    故每次按当前默认模型构建，保证与用户当前配置一致）
        from backend.services.llm_service import LLMService
        cfg = await get_default_model()
        if cfg is None:
            return "尚未配置模型，请先在设置中添加一个默认模型。"
        service = LLMService(
            model_type=cfg.get("type", "online"),
            model_name=cfg.get("modelName", ""),
            base_url=cfg.get("baseUrl"),
            api_key=cfg.get("apiKey", ""),
            role=cfg.get("role", "default") or "default",
        )

        # 2. 工具集：本地 + MCP + code 技能（与「全能助手」一致）
        tools = await get_all_tools(self._mcp_manager(), self._skill_registry())

        # 3. 消息与系统提示词
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": text},
        ]

        # 4. 流式生成 → 拼出最终文本（过滤控制标记 + 思考过程，仅保留最终答案）
        chunks: list[str] = []
        in_reasoning = False
        async for chunk in service.generate_response(
            messages=messages,
            enable_tools=True,
            tools=tools or None,
            request=None,
            params={},
            skill_registry=self._skill_registry(),
            mcp_manager=self._mcp_manager(),
            max_steps=getattr(self, "_max_steps", 25),
        ):
            if not isinstance(chunk, str) or not chunk:
                continue
            stripped = chunk.strip()
            # 思考过程（reasoning_content）以 <!--reasoning:start--> / <!--reasoning:end--> 包裹，
            # 语音场景只播报最终结果，整段跳过，不读思考过程。
            if stripped.startswith("<!--reasoning:start-->"):
                in_reasoning = True
                continue
            if stripped.startswith("<!--reasoning:end"):
                in_reasoning = False
                continue
            if in_reasoning:
                continue
            if stripped.startswith("<!--"):
                continue  # 跳过 tool_approval / tool_status / token_usage / plan / ppt 等控制标记
            if stripped.startswith(_PROCESS_MARKERS):
                continue  # 跳过工具执行状态 / 重试 / 失败等过程提示
            chunks.append(stripped)

        return "".join(chunks).strip()

    def _system_prompt(self) -> str:
        from config_loader import config
        from backend.utils.base import resource_path, get_current_time
        import backend as _be
        try:
            with open(resource_path("system_prompt.md"), encoding="utf-8") as f:
                sp = f.read()
        except Exception:
            sp = ""
        sp = sp.replace("{{uploads_dir}}", str(config.uploads_dir))
        sp = sp.replace("{{workspace_path}}", getattr(_be, "workspace_path", "") or "")
        sp = sp.replace("{{kb_path}}", getattr(_be, "kb_path", "") or "")
        sp = sp.replace("{{time_now}}", get_current_time())
        # 语音场景追加：回答要口语化、简洁，便于朗读
        sp += "\n\n你正以语音方式与用户对话，请用口语化的中文简洁作答，避免冗长的列表和代码块。"
        return sp

    # ── 依赖句柄（由 main.py lifespan 注入）──
    def bind(self, mcp_manager=None, skill_registry=None):
        self._mcp = mcp_manager
        self._skills = skill_registry

    def _mcp_manager(self):
        return getattr(self, "_mcp", None)

    def _skill_registry(self):
        return getattr(self, "_skills", None)

    # ── P1：唤醒监听主循环 ──
    def _is_speaking(self) -> bool:
        """本地 TTS 是否正在播报（用于抑制播报回音，避免误唤醒）。"""
        tts = self.tts
        if tts is None:
            from .tts import get_tts
            tts = get_tts()
        return bool(getattr(tts, "speaking", False))

    def _on_audio(self, pcm: bytes):
        """sounddevice 回调（运行在其采集线程）。不做任何 asyncio，仅状态机 + 调度。"""
        if self._is_speaking():
            return  # 正在本地播报，忽略回音（edge-tts/pygame 外放会被麦克风重新采入）
        if self._wake_mode == "whisper":
            self._on_audio_whisper(pcm)
        else:
            self._on_audio_oww(pcm)

    def _on_audio_oww(self, pcm: bytes):
        """openWakeWord 模式：先检测唤醒词，命中后收音 → STT → ask。"""
        if self._listening:
            self._buffer.append(pcm)
            ended = self._vad.process(pcm)
            if ended or (self._max_blocks > 0 and len(self._buffer) >= self._max_blocks):
                audio = b"".join(self._buffer)
                self._listening = False
                self._buffer = []
                self._schedule(self._handle_utterance(audio))
        else:
            if self._cooldown_blocks > 0:
                self._cooldown_blocks -= 1
                return
            if self._wake_engine is not None and self._wake_engine.detect(pcm):
                self._listening = True
                self._buffer = []
                self._vad.reset()
                self._cooldown_blocks = 0
                self._schedule(self._set_state(AssistantState.LISTENING))

    def _on_audio_whisper(self, pcm: bytes):
        """whisper 模式：常驻用 VAD 切出「有人说话」的片段，交给本地转写匹配唤醒词。"""
        if self._cooldown_blocks > 0:
            self._cooldown_blocks -= 1
            return
        if not self._speech_active:
            if self._vad.is_speech(pcm):
                self._speech_active = True
                self._buffer = []
                self._vad.reset()
        if self._speech_active:
            self._buffer.append(pcm)
            ended = self._vad.process(pcm)
            if ended or (self._max_blocks > 0 and len(self._buffer) >= self._max_blocks):
                audio = b"".join(self._buffer)
                self._speech_active = False
                self._buffer = []
                self._schedule(self._handle_whisper_utterance(audio))

    async def _handle_utterance(self, audio: bytes):
        """（主事件循环）处理一段唤醒后采集到的语音。"""
        try:
            transcript = await self._stt_pcm(audio)
            if not transcript:
                await connection_manager.broadcast({"type": "no_speech"})
                return
            answer = await self.ask(transcript)
            spoken = await self.speak_answer(answer)
            await connection_manager.broadcast({
                "type": "assistant_answer",
                "transcript": transcript,
                "answer": answer,
                "spoken_locally": spoken,
            })
        except Exception as e:
            logger.error(f"[Voice] 唤醒处理失败：{e}", exc_info=True)
            await connection_manager.broadcast({"type": "assistant_error", "message": str(e)})
        finally:
            # 处理完成后进入冷却，避免把播报/尾音当新唤醒
            self._cooldown_blocks = 20  # 约 0.6s（按 30ms/块）

    async def _stt_pcm(self, audio: bytes) -> str:
        """把 int16 16kHz PCM 转写为文本。

        local 模式：faster-whisper 本地转写（免 key，可离线）；
        cloud 模式：封装为 WAV 走 OpenAI 兼容 STT。
        """
        from config_loader import config
        if self._use_local_stt():
            from .whisper_wake import transcribe_pcm_async
            return await transcribe_pcm_async(
                audio,
                model_size=config.voice_assistant_whisper_model,
                mirror=config.voice_assistant_whisper_mirror,
            )
        from backend.routes.voice import _get_voice_client
        if not config.voice_enabled:
            return ""
        client, stt_model = _get_voice_client(stt=True)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(audio)
        buf.seek(0)
        buf.name = "command.wav"
        result = await client.audio.transcriptions.create(model=stt_model, file=buf, language="zh")
        return (result.text or "").strip()

    async def _handle_whisper_utterance(self, audio: bytes):
        """（主事件循环）whisper 模式：本地转写一段语音，匹配唤醒词并执行指令。"""
        try:
            if self._awaiting_command:
                # 上一句是纯唤醒词，这一句直接视为指令
                command = await asyncio.to_thread(self._whisper.transcribe, audio)
                self._awaiting_command = False
                if not command:
                    await connection_manager.broadcast({"type": "no_speech"})
                    await self._set_state(AssistantState.IDLE)
                    return
                await self._process_command(command, command)
                return

            transcript = await asyncio.to_thread(self._whisper.transcribe, audio)
            if not transcript:
                return
            matched, command = self._whisper.match_wake(transcript, self._wake_word_text)
            if not matched:
                logger.info(f"[Voice] 未命中唤醒词（忽略）：{transcript!r}")
                return  # 不是唤醒词，忽略（环境音/无关对话）
            if command:
                await self._process_command(command, transcript)
            else:
                # 纯唤醒词：先语音回应问候语，再进入聆听，等下一句指令
                try:
                    await self.speak_answer(_WAKE_GREETING)
                except Exception as e:
                    # 问候播报失败（如离线）不阻断唤醒流程，仍进入聆听等指令
                    logger.warning(f"[Voice] 唤醒问候播报失败：{e}")
                self._awaiting_command = True
                await self._set_state(AssistantState.LISTENING)
        except Exception as e:
            logger.error(f"[Voice] 唤醒处理失败：{e}", exc_info=True)
            await connection_manager.broadcast({"type": "assistant_error", "message": str(e)})
        finally:
            self._cooldown_blocks = 20  # 约 0.6s 冷却，避免把尾音/播报当新输入

    async def _process_command(self, command: str, original_transcript: str):
        """把识别出的指令交给 agent，并广播结果（前端写入对话 + 播报）。"""
        answer = await self.ask(command)
        spoken = await self.speak_answer(answer)
        await connection_manager.broadcast({
            "type": "assistant_answer",
            "transcript": original_transcript,
            "answer": answer,
            "spoken_locally": spoken,
        })

    # ── 生命周期 ──
    async def start(self):
        """P0 只做按键；P1/P1.5 在依赖/麦克风就绪时启动免按键唤醒监听。

        模型下载/加载 + 麦克风开启放到后台线程，避免首次下载阻塞事件循环、
        进而拖慢 /api/wait-ready 的就绪信号。加载完成后 _on_audio 才开始做
        唤醒检测；在此之前恒为按键模式。
        """
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._bootstrap_gen += 1
        gen = self._bootstrap_gen
        from config_loader import config
        # 单句最大块数（30ms/块）
        self._max_blocks = int(config.voice_assistant_max_listen_seconds / 0.03)
        self._wake_word_text = config.voice_assistant_wake_word or ""
        if not config.voice_assistant_enabled:
            logger.info("[Voice] 语音助手已暂停（启用开关关闭，仅按键模式）")
            await self._set_state(AssistantState.PAUSED)
            return

        # 启用：先广播「待命」，唤醒监听真正就绪后仍保持该状态
        await self._set_state(AssistantState.IDLE)
        mode = (config.voice_assistant_wake_mode or "whisper").lower()
        threading.Thread(target=self._bootstrap, args=(mode, gen), name="voice-wake-bootstrap", daemon=True).start()

    def _bootstrap(self, mode: str, gen: int):
        """后台线程：按模式加载唤醒引擎 + 开麦。失败则退回按键模式。"""
        from config_loader import config
        try:
            from .vad import EnergyVAD
            from .audio_capture import MicStream

            vad = EnergyVAD(silence_seconds=config.voice_assistant_silence_timeout)
            if mode == "oww":
                from .wake_word import OpenWakeWordEngine
                detector = OpenWakeWordEngine(
                    model_name=config.voice_assistant_wake_word,
                    model_path=getattr(config, "voice_assistant_wake_model_path", "") or "",
                    threshold=config.voice_assistant_wake_threshold,
                    mirror=getattr(config, "voice_assistant_wake_model_mirror", "") or "",
                )
            else:
                from .whisper_wake import WhisperWakeDetector
                detector = WhisperWakeDetector(
                    model_size=config.voice_assistant_whisper_model,
                    mirror=config.voice_assistant_whisper_mirror,
                )

            mic = MicStream(on_audio=self._on_audio)
            mic.start()

            if self._stopping or gen != self._bootstrap_gen:
                # 启动期间应用已关闭 / 设置已被再次修改，立即回收刚开起的资源
                mic.stop()
                detector.close()
                return
            # 顺序：先 vad/mic/detector，最后置 _wake_mode——_on_audio 以 _wake_mode
            # 非空作为「可开始唤醒检测」的闸门，避免回调线程读到半初始化状态。
            self._vad = vad
            self._mic = mic
            if mode == "oww":
                self._wake_engine = detector
            else:
                self._whisper = detector
            self._wake_mode = mode
            logger.info(f"[Voice] 免按键唤醒已启动（mode={mode}）")
        except Exception as e:
            logger.warning(f"[Voice] 免按键唤醒启动失败，退回按键模式：{e}")

    async def stop(self):
        self._stopping = True
        if self._mic is not None:
            self._mic.stop()
            self._mic = None
        if self._wake_engine is not None:
            try:
                self._wake_engine.close()
            except Exception:
                pass
            self._wake_engine = None
        if self._whisper is not None:
            try:
                self._whisper.close()
            except Exception:
                pass
            self._whisper = None
        self._wake_mode = None
        self._listening = False
        self._speech_active = False
        self._awaiting_command = False
        await self._set_state(AssistantState.IDLE)
        logger.info("[Voice] 语音助手已停止")


# 进程内单例（懒加载；main.py lifespan 会 bind 依赖并 start）
_assistant: Optional[VoiceAssistant] = None


def get_assistant() -> VoiceAssistant:
    global _assistant
    if _assistant is None:
        _assistant = VoiceAssistant()
    return _assistant
