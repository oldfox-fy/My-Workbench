# backend/services/llm_service.py
import uuid
import json
import time
import asyncio
from fastapi import Request
from openai import AsyncOpenAI, APIError
from typing import List, Dict, AsyncGenerator, Optional
import httpx
from backend.services.tools import get_all_tools, execute_tool
from backend.db.tool_calls import create_tool_call, update_tool_call, update_tool_call_arguments
from config_loader import config as app_config
from backend.utils.base import normalize_base_url

# AsyncOpenAI 默认 HTTP 超时：连接 10s，读取 120s，写入 10s，连接池 10s
_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

# 工具审批：模块级状态（同一进程内共享）
_pending_approvals: Dict[str, asyncio.Event] = {}
_approval_results: Dict[str, bool] = {}
_approval_answers: Dict[str, str] = {}  # system_ask_user 的用户文本回复

def set_approval_result(call_id: str, approved: bool, answer: str = None):
    """由 POST /api/tool-calls/approval 端点调用，设置审批结果并唤醒等待的协程。"""
    _approval_results[call_id] = approved
    if answer:
        _approval_answers[call_id] = answer
    event = _pending_approvals.pop(call_id, None)
    if event:
        event.set()


# 可重试的 HTTP 状态码（限流 + 服务端临时故障）
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# 默认重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # 秒


def _is_retryable(error: APIError) -> bool:
    """判断 API 错误是否可通过重试恢复"""
    status = getattr(error, 'status_code', None) or getattr(error, 'http_status', None)
    return status in _RETRYABLE_STATUSES


def _is_sensitive_tool(func_name: str) -> bool:
    """判断工具是否需要用户审批（敏感工具不应在流式阶段 eager-launch）"""
    sensitive = getattr(app_config, 'tool_approval_sensitive', set())
    return func_name in sensitive


def _cancel_pending(pending_executions: dict) -> None:
    """取消所有未完成的背景工具任务，防止资源泄露。"""
    for task in pending_executions.values():
        if not task.done():
            task.cancel()


class LLMService:
    instance: Optional["LLMService"] = None

    def __init__(self,
                 model_type: str,
                 model_name: str,
                 api_key: str = "",
                 base_url: str = None,
                 thinking: str = 'enabled',
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 base_delay: float = DEFAULT_BASE_DELAY,
                 fallback_config: Optional[Dict] = None,
                 role: str = 'default'):
        self.model_type = model_type
        self.model_name = model_name
        self.role = role  # 模型角色：image_gen → 走 images.generate 而非 chat.completions
        self.thinking = thinking
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.fallback_config = fallback_config
        self.client = AsyncOpenAI(
            api_key=api_key or None,
            base_url=normalize_base_url(base_url or ""),
            timeout=_CLIENT_TIMEOUT,
        )
        # 备用客户端（惰性创建，仅在触发降级时初始化）
        self._fallback_client = None

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        enable_tools: bool = False,
        tools: Optional[List[Dict]] = None,
        request: Optional[Request] = None,
        mcp_manager=None,
        params: Dict = None,
        message_id: Optional[int] = None,
        skill_registry=None,
        max_steps: int = 25,
        excluded_tools: Optional[set] = None,
        skip_approval: bool = False,
        auto_approve: bool = False,
    ) -> AsyncGenerator[str, None]:
        params = params or {}
        _sub_agent = skip_approval  # 子智能体模式下跳过审批
        _auto_approve = auto_approve  # 子智能体模式下自动同意审批

        # ---------- Span 追踪 ----------
        from backend.services.tracer import TraceManager, persist_trace
        chat_id = ""
        if request:
            chat_id = request.path_params.get("chat_id", "") if hasattr(request, "path_params") else ""
        trace = TraceManager(chat_id=chat_id, message_id=message_id or 0)
        # ---------- 图像生成分支 ----------
        # 使用模型角色判断（而非名字里是否有 "image"），因为主流生图模型
        # 如 dall-e-3、stable-diffusion、FLUX、Kolors 名字里都不含 "image"。
        if self.role == "image_gen":
            prompt = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    prompt = msg.get("content", "")
                    break

            if not prompt:
                yield "❌ 未找到有效的用户提示词，无法生成图像。"
                return

            size = params.get("size", "1024x768")
            quality = params.get("quality", "standard")
            n = params.get("n", 1)

            try:
                response = await self.client.images.generate(
                    model=self.model_name,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    n=n,
                )

                if not response.data or len(response.data) == 0:
                    yield "❌ 图像生成服务未返回有效结果。"
                    return

                img_data = response.data[0]

                if hasattr(img_data, 'url') and img_data.url:
                    image_url = img_data.url
                    yield f"![生成的图片]({image_url})"
                else:
                    if hasattr(img_data, 'b64_json') and img_data.b64_json:
                        yield "⚠️ 图像生成服务仅返回 base64 数据，无法提供直接链接。"
                    else:
                        yield "❌ 图像生成服务未返回图片 URL 或 base64 数据。"
                return

            except APIError as e:
                # 提供更精准的错误提示
                msg = e.message
                hint = ""
                if "20012" in msg or "does not exist" in msg.lower():
                    hint = (
                        f"\n\n💡 **模型名 `{self.model_name}` 在当前服务商不支持图像生成。**\n"
                        "请到 设置 → 模型 → 图像生成角色 中更换为正确的生图模型名。\n"
                        "例如硅基流动可用：`stabilityai/stable-diffusion-3-5-large`"
                    )
                yield f"❌ 图像生成 API 错误：{msg}{hint}"
            except Exception as e:
                yield f"❌ 图像生成失败：{str(e)}"
            return

        # ---------- 原有文本生成 + 工具调用分支 ----------
        current_messages = messages.copy()

        if tools is None and enable_tools:
            tools = await get_all_tools(mcp_manager, skill_registry)

        # 过滤掉排除的工具（子智能体防递归委派等场景）
        if tools and excluded_tools:
            tools = [t for t in tools if t.get("function", {}).get("name") not in excluded_tools]

        reasoning_start_time = None

        # 全步骤累计 token（计费总量）
        total_usage_all_steps = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "completion_tokens_details": {}
        }

        # 最后一步的 token 和生成耗时（用于展示最终回答速度）
        last_step_usage = None
        last_step_generation_time = 0.0

        MAX_STEPS = max_steps
        # 连续失败上限与 max_steps 联动：每步可能并行调用多个工具，
        # 因此失败容忍度应至少等于步数上限，避免子智能体（max_steps=3）
        # 在第一步并行工具全失败时就被 force_final 终止。
        MAX_CONSECUTIVE_FAILURES = max(3, max_steps * 2)
        consecutive_failures = 0
        force_final = False

        for step in range(MAX_STEPS):
            if request and await request.is_disconnected():
                break

            # 开始 Step Span
            step_span = trace.start_step(step + 1)

            tool_calls_by_index = {}
            reasoning_buffer = ""
            in_reasoning = False

            step_usage_record = None
            step_generation_time = 0.0

            kwargs = {
                "model": self.model_name,
                "messages": current_messages,
                "stream": True,
                "temperature": params.get('temperature', 1.0),
                "top_p": params.get('top_p', 0.95),
                "frequency_penalty": params.get('frequency_penalty', 0.0),
                "presence_penalty": params.get('presence_penalty', 0.0),
                "max_tokens": params.get('max_tokens', 16384),
                "stream_options": {"include_usage": True},
            }

            extra_body = {
                "top_k": params.get('top_k', 20),
                "chat_template_kwargs": {
                    "add_generation_prompt": True,
                }
            }

            if self.thinking == "enabled":
                extra_body["enable_thinking"] = True
                extra_body["preserve_thinking"] = True
                extra_body["chat_template_kwargs"]["enable_thinking"] = True
                extra_body["chat_template_kwargs"]["preserve_thinking"] = True

            kwargs["extra_body"] = extra_body

            # 连续失败上限或达到最大步数，都强制让模型直接总结
            if force_final or step == MAX_STEPS - 1:
                if force_final:
                    yield "\n⚠️ 工具连续调用失败次数过多，正在基于已收集信息生成最终总结...\n"
                else:
                    yield "\n⚠️ 工具调用次数已达上限，正在基于已收集信息生成最终总结...\n"

                current_messages.append({
                    "role": "user",
                    "content": ("【系统指令】你的工具调用已达限制或连续多次失败。"
                                "请立即放弃尝试调用工具，根据上面已经收集到的上下文信息，"
                                "直接以纯文本形式回答我的问题并进行最终总结。"
                                "禁止输出任何 XML/JSON 格式的工具调用标记。")
                })
                kwargs["messages"] = current_messages
                # 移除本轮可能残留的 tools 参数，确保 API 不尝试生成工具调用
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                tools = None
                force_final = False
            elif tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            # ---------- API 调用（含重试与降级） ----------
            response = None
            api_error = None
            client = self.client  # 当前使用的客户端（可能已在之前的步骤中降级）
            _api_t0 = time.time()
            import logging as _logging
            _log = _logging.getLogger("My Workbench")
            _log.info(f"[LLM] API call step={step+1}/{MAX_STEPS}, model={kwargs.get('model','?')[:30]}...")

            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.chat.completions.create(**kwargs)
                    api_error = None
                    break
                except APIError as e:
                    api_error = e
                    if attempt < self.max_retries and _is_retryable(e):
                        delay = self.base_delay * (2 ** attempt)  # 指数退避: 1s, 2s, 4s
                        yield (f"\n⚠️ API 调用失败（{e.message}），"
                               f"{delay:.0f}s 后重试（{attempt + 1}/{self.max_retries}）...\n")
                        await asyncio.sleep(delay)
                    elif self.fallback_config and client is not self._fallback_client:
                        # 主模型不可用，尝试切换到备用模型
                        yield (f"\n🔄 主模型暂时不可用（{e.message}），"
                               f"正在切换到备用模型：{self.fallback_config.get('model_name', '')}...\n")
                        if self._fallback_client is None:
                            self._fallback_client = AsyncOpenAI(
                                api_key=self.fallback_config.get('api_key') or None,
                                base_url=normalize_base_url(self.fallback_config.get('base_url') or ''),
                                timeout=_CLIENT_TIMEOUT,
                            )
                        client = self._fallback_client
                        kwargs["model"] = self.fallback_config.get("model_name", self.model_name)
                        # 备用模型重试一次
                        try:
                            response = await client.chat.completions.create(**kwargs)
                            api_error = None
                            yield "✅ 备用模型连接成功，继续处理...\n"
                            break
                        except APIError as e2:
                            api_error = e2
                            yield f"\n❌ 备用模型同样失败：{e2.message}\n"
                            break
                    else:
                        if not self.fallback_config:
                            yield f"\n❌ 模型服务错误（已重试 {attempt} 次）：{e.message}"
                        break
                except Exception as e:
                    # 网络层异常（httpx 连接/超时/协议错误）纳入重试
                    if isinstance(e, (httpx.ConnectError, httpx.ReadTimeout,
                                       httpx.RemoteProtocolError, httpx.WriteError,
                                       httpx.PoolTimeout, httpx.TimeoutException,
                                       httpx.NetworkError)):
                        if attempt < self.max_retries - 1:
                            delay = self.base_delay * (2 ** attempt)
                            yield (f"\n⏳ 网络异常（{type(e).__name__}），"
                                   f"{delay:.0f}s 后重试（{attempt + 1}/{self.max_retries}）...\n")
                            await asyncio.sleep(delay)
                            continue
                    api_error = APIError(message=str(e))
                    yield f"\n❌ 模型调用异常：{str(e)}\n"
                    break

            if api_error:
                _log.warning(f"[LLM] API call FAILED step={step+1} in {time.time()-_api_t0:.1f}s: {api_error.message[:100]}")
                break

            _log.info(f"[LLM] API call DONE step={step+1} in {time.time()-_api_t0:.1f}s, streaming...")
            first_token_time = None
            tool_preview_active = {}
            tool_calls_started = False
            # 流式工具调用：追踪已启动后台执行的工具
            pending_executions: dict = {}   # idx → asyncio.Task
            last_active_idx = None

            async for chunk in response:
                if request and await request.is_disconnected():
                    break

                # ---------- 流式工具执行辅助函数 ----------
                # 定义在此处以捕获 step 作用域变量（message_id, mcp_manager, skill_registry）
                async def _execute_one_tool(idx, tc, preview_info):
                    """执行单个工具调用，返回结果元组。"""
                    local_call_id = preview_info.get('call_id', '')
                    real_tool_call_id = tc.get("id")
                    func_name = tc["function"]["name"] or "未知工具"
                    raw_args = tc["function"]["arguments"]

                    # ── 执行级工具黑名单：被排除的工具直接拒绝 ──
                    # 只过滤 tools 列表不够（部分模型会从训练记忆"回忆"工具并生成调用），
                    # 必须在执行前二次拦截，彻底禁止子智能体递归委派。
                    if excluded_tools and func_name in excluded_tools:
                        return {
                            "idx": idx,
                            "local_call_id": local_call_id,
                            "real_tool_call_id": real_tool_call_id,
                            "func_name": func_name,
                            "failed": True,
                            "tool_content": json.dumps({
                                "success": False,
                                "error": f"子智能体禁止使用 {func_name}。请用已授权的工具（system_web_fetch / system_read_file / system_write_file 等）直接完成任务，不要再次委派。"
                            }, ensure_ascii=False),
                            "parse_error": None,
                        }

                    # 创建 tool_call Span（用于数据库持久化）
                    tool_span = trace.start_tool_call(func_name, raw_args[:300])

                    parse_error = None
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as e:
                        parse_error = f"JSON 解析失败: {e}\n原始参数: {raw_args[:200]}"
                        args = {"raw": raw_args, "parse_error": str(e)}

                    if message_id:
                        try:
                            await update_tool_call_arguments(local_call_id, args)
                        except Exception as e:
                            print(f"[DB] Failed to update arguments: {e}")

                    start_time = time.time()
                    failed = False
                    import logging as _logging
                    _log = _logging.getLogger("My Workbench")
                    _log.info(f"[LLM] tool START: {func_name}")
                    try:
                        result = await asyncio.wait_for(
                            execute_tool(func_name, args, mcp_manager, skill_registry, llm_service=self, params=params),
                            timeout=120.0,
                        )
                        if isinstance(result, str):
                            try:
                                result_obj = json.loads(result)
                                if isinstance(result_obj, dict) and result_obj.get("success") is False:
                                    failed = True
                            except json.JSONDecodeError:
                                if result.startswith("工具执行出错:"):
                                    failed = True
                        elif isinstance(result, dict) and result.get("success") is False:
                            failed = True
                    except Exception as e:
                        error_msg = str(e)
                        if len(error_msg) > 1000:
                            error_msg = error_msg[:1000] + "...(错误信息过长已截断)"
                        result = f"工具执行出错: {error_msg}"
                        failed = True

                    exec_time_ms = int((time.time() - start_time) * 1000)

                    if message_id:
                        try:
                            result_str = str(result)[:20000]
                            await update_tool_call(
                                call_id=local_call_id,
                                result=result_str,
                                status="error" if failed else "success",
                                execution_time=exec_time_ms,
                                error_message=result if failed else None
                            )
                        except Exception as e:
                            print(f"[DB] Failed to update result: {e}")

                    if isinstance(result, dict):
                        tool_content = json.dumps(result, ensure_ascii=False)
                    else:
                        tool_content = str(result)

                    # 结束 tool_call Span
                    _log.info(f"[LLM] tool DONE: {func_name} in {exec_time_ms}ms, failed={failed}")
                    trace.end_span(tool_span.id, "error" if failed else "success",
                                   tool_content[:300], result if failed else None)

                    return {
                        "idx": idx,
                        "local_call_id": local_call_id,
                        "real_tool_call_id": real_tool_call_id,
                        "func_name": func_name,
                        "failed": failed,
                        "tool_content": tool_content,
                        "parse_error": parse_error,
                    }
                if request and await request.is_disconnected():
                    break

                # ---------- usage 收集 ----------
                if hasattr(chunk, 'usage') and chunk.usage:
                    step_usage = chunk.usage
                    try:
                        su = step_usage.model_dump()
                    except AttributeError:
                        su = dict(step_usage)

                    total_usage_all_steps["prompt_tokens"] += su.get("prompt_tokens", 0) or 0
                    total_usage_all_steps["completion_tokens"] += su.get("completion_tokens", 0) or 0
                    total_usage_all_steps["total_tokens"] += su.get("total_tokens", 0) or 0

                    details = su.get("completion_tokens_details") or {}
                    for k, v in details.items():
                        total_usage_all_steps["completion_tokens_details"][k] = \
                            total_usage_all_steps["completion_tokens_details"].get(k, 0) + (v or 0)

                    step_usage_record = {
                        "prompt_tokens": su.get("prompt_tokens", 0) or 0,
                        "completion_tokens": su.get("completion_tokens", 0) or 0,
                        "total_tokens": su.get("total_tokens", 0) or 0,
                        "completion_tokens_details": details
                    }

                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if first_token_time is None:
                        if (getattr(delta, 'reasoning_content', None) or
                            getattr(delta, 'content', None) or
                            getattr(delta, 'tool_calls', None)):
                            first_token_time = time.time()
                else:
                    continue

                delta = chunk.choices[0].delta
                reasoning = getattr(delta, 'reasoning_content', None)
                tool_calls_data = getattr(delta, 'tool_calls', None)
                delta_content = getattr(delta, 'content', None)

                if reasoning:
                    if not in_reasoning:
                        in_reasoning = True
                        reasoning_start_time = time.time()
                        yield "<!--reasoning:start-->"
                    reasoning_buffer += reasoning
                    yield reasoning
                    continue

                if in_reasoning and (delta_content or tool_calls_data):
                    reasoning_time = time.time() - reasoning_start_time
                    yield f"<!--reasoning:end:{reasoning_time:.2f}-->"
                    in_reasoning = False

                if tool_calls_data:
                    if not tool_calls_started:
                        tool_calls_started = True
                        yield "\n<!--tool_calls:start-->"

                    for tc_delta in tool_calls_data:
                        idx = getattr(tc_delta, 'index', None)
                        if idx is None:
                            idx = tc_delta.id if tc_delta.id else str(uuid.uuid4())

                        if idx not in tool_preview_active and tc_delta.function and tc_delta.function.name:
                            call_id = str(uuid.uuid4())
                            func_name = tc_delta.function.name
                            tool_preview_active[idx] = {
                                'call_id': call_id,
                                'name': func_name,
                                'db_created': False,
                                'preview_sent': True
                            }
                            if idx in tool_calls_by_index:
                                tool_calls_by_index[idx]['call_id'] = call_id
                            else:
                                tool_calls_by_index[idx] = {
                                    "id": None, "type": "function", "function": {"name": "", "arguments": ""},
                                    "call_id": call_id
                                }

                            yield f"<!--tool_preview:start:{call_id}:{func_name}-->"

                            # 创建数据库记录
                            if message_id:
                                try:
                                    await create_tool_call(
                                        message_id=message_id,
                                        call_id=call_id,
                                        tool_name=func_name
                                    )
                                    tool_preview_active[idx]['db_created'] = True
                                except Exception as e:
                                    print(f"[DB] Failed to create tool call record: {e}")

                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": getattr(tc_delta, 'id', None),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                                "call_id": None
                            }
                        target = tool_calls_by_index[idx]
                        if tc_delta.id:
                            target["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name and not target["function"]["name"]:
                                target["function"]["name"] = tc_delta.function.name
                            arg_delta = tc_delta.function.arguments or ""
                            target["function"]["arguments"] += arg_delta

                        # 流式工具执行：检测到新 idx → 前一个 idx 参数已完整 → 后台启动
                        # ⚠️ 敏感工具不在此阶段 eager-launch，需等待后续审批流程
                        if idx != last_active_idx:
                            if last_active_idx is not None and last_active_idx not in pending_executions:
                                prev_tc = tool_calls_by_index.get(last_active_idx)
                                if prev_tc and prev_tc["function"]["arguments"].strip():
                                    if not _is_sensitive_tool(prev_tc["function"]["name"]):
                                        pending_executions[last_active_idx] = asyncio.create_task(
                                            _execute_one_tool(last_active_idx, prev_tc, tool_preview_active.get(last_active_idx, {})))
                            last_active_idx = idx

                elif delta_content:
                    yield delta_content

            # 记录本步骤生成时间
            if first_token_time is not None:
                step_generation_time = time.time() - first_token_time
                last_step_generation_time = step_generation_time

            if in_reasoning:
                reasoning_time = time.time() - reasoning_start_time
                yield f"<!--reasoning:end:{reasoning_time:.2f}-->"

            if step_usage_record:
                last_step_usage = step_usage_record

            # ---------- 构建工具调用字典 (用于 assistant_msg) ----------
            tool_calls = {}
            for idx, tc in tool_calls_by_index.items():
                if tc.get("id"):
                    tool_calls[tc["id"]] = tc
                else:
                    print(f"[ERROR] 工具 {tc['function']['name']} 缺失 ID，可能流式解析异常，跳过")
                    continue

            if tool_calls and request and await request.is_disconnected():
                _cancel_pending(pending_executions)
                break

            if not tool_calls:
                _cancel_pending(pending_executions)
                break

            # ---------- 构建 valid_calls (用于执行工具，保留 idx) ----------
            valid_calls = {}
            for idx, tc in tool_calls_by_index.items():
                if tc.get("id") and tc["function"]["name"].strip():
                    valid_calls[idx] = tc
                else:
                    yield "\n⚠️ 检测到无效工具调用（名称空白），已忽略。\n"

            if not valid_calls:
                break

            if not tool_calls_started:
                yield "\n<!--tool_calls:start-->"

            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": list(tool_calls.values())
            }
            current_messages.append(assistant_msg)

            # ---------- 并行执行工具（结合流式后台任务） ----------
            # 快照 tool_preview_active
            preview_snapshot = {
                idx: dict(info)
                for idx, info in tool_preview_active.items()
                if idx in valid_calls
            }

            # 流结束后：最后一个 tool_call 的参数也已完成 → 后台启动
            # ⚠️ 敏感工具不在此 eager-launch，等审批流处理
            if last_active_idx is not None and last_active_idx not in pending_executions:
                last_tc = tool_calls_by_index.get(last_active_idx)
                if last_tc and last_tc["function"]["arguments"].strip():
                    if not _is_sensitive_tool(last_tc["function"]["name"]):
                        pending_executions[last_active_idx] = asyncio.create_task(
                            _execute_one_tool(last_active_idx, last_tc, preview_snapshot.get(last_active_idx, {}))
                        )

            # ---------- 工具审批流 ----------
            # 子智能体模式：跳过审批（无人可响应审批请求，event.wait() 会永久阻塞）
            if _sub_agent:
                approval_enabled = False
            else:
                approval_enabled = getattr(app_config, 'tool_approval_enabled', True)
            sensitive_set = set(getattr(app_config, 'tool_approval_sensitive', set()))

            # 收集需要审批的敏感工具
            approval_needed = []
            for idx, tc in valid_calls.items():
                if idx in pending_executions:
                    continue  # 已在流式阶段启动，不中断
                func_name = tc["function"]["name"]
                if approval_enabled and func_name in sensitive_set:
                    local_call_id = preview_snapshot.get(idx, {}).get('call_id', '')
                    raw_args = tc["function"]["arguments"]
                    args_preview = raw_args[:500] if raw_args else "{}"
                    # base64 编码参数，避免特殊字符破坏 marker 格式
                    import base64
                    encoded_args = base64.b64encode(args_preview.encode()).decode()
                    approval_needed.append((idx, local_call_id, func_name, encoded_args))

            # 发出审批请求，等待前端响应
            rejected_call_ids = set()
            if approval_needed and (request or _auto_approve):
                for idx, local_call_id, func_name, args_preview in approval_needed:
                    event = asyncio.Event()
                    _pending_approvals[local_call_id] = event
                    yield f"<!--tool_approval:{local_call_id}:{func_name}:{args_preview}-->"

                # 等待所有审批结果（最长 60s 超时，超时自动拒绝）
                for idx, local_call_id, func_name, args_preview in approval_needed:
                    if _auto_approve:
                        # 子智能体模式：自动同意所有敏感工具，不等待用户
                        _approval_results[local_call_id] = True
                        yield f"<!--tool_status:{local_call_id}:approved_auto-->"
                    else:
                        event = _pending_approvals.get(local_call_id)
                        if event:
                            try:
                                await asyncio.wait_for(event.wait(), timeout=60.0)
                            except asyncio.TimeoutError:
                                _approval_results[local_call_id] = False
                    if not _approval_results.get(local_call_id, False):
                        rejected_call_ids.add(local_call_id)
                        yield f"<!--tool_status:{local_call_id}:rejected-->"

            # 清理审批状态
            for local_call_id in [a[1] for a in approval_needed]:
                _pending_approvals.pop(local_call_id, None)
                _approval_results.pop(local_call_id, None)

            # 对未在流式阶段启动的工具，现在启动（跳过被拒绝的）
            fresh_coros = []
            fresh_indices = []
            for idx, tc in valid_calls.items():
                if idx not in pending_executions:
                    local_call_id = preview_snapshot.get(idx, {}).get('call_id', '')
                    func_name = tc["function"]["name"]
                    if local_call_id in rejected_call_ids:
                        # 被拒绝的工具：构造一个跳过结果
                        async def _rejected(idx=idx, tc=tc, pi=preview_snapshot.get(idx, {})):
                            return {
                                "idx": idx,
                                "local_call_id": pi.get('call_id', ''),
                                "real_tool_call_id": tc.get("id"),
                                "func_name": tc["function"]["name"],
                                "failed": True,
                                "tool_content": "用户拒绝了此工具调用。",
                                "parse_error": None,
                            }
                        fresh_coros.append(_rejected())
                    elif func_name == "system_ask_user" and local_call_id in _approval_answers:
                        # ask_user 获得了用户文本回复：跳过实际执行，直接返回答案
                        user_answer = _approval_answers.pop(local_call_id)
                        async def _answer_result(idx=idx, tc=tc, pi=preview_snapshot.get(idx, {})):
                            return {
                                "idx": idx,
                                "local_call_id": pi.get('call_id', ''),
                                "real_tool_call_id": tc.get("id"),
                                "func_name": tc["function"]["name"],
                                "failed": False,
                                "tool_content": json.dumps({"success": True, "answer": user_answer}, ensure_ascii=False),
                                "parse_error": None,
                            }
                        fresh_coros.append(_answer_result())
                    else:
                        fresh_indices.append(idx)
                        fresh_coros.append(_execute_one_tool(idx, tc, preview_snapshot.get(idx, {})))

            # 等待后台任务 + 执行新工具，合并所有结果
            exec_results = [await task for task in pending_executions.values()]
            if fresh_coros:
                exec_results += await asyncio.gather(*fresh_coros)

            # 按 idx 排序后顺序处理结果（保持 tool_call_id 顺序一致性）
            exec_results.sort(key=lambda r: r["idx"])

            for r in exec_results:
                idx = r["idx"]
                local_call_id = r["local_call_id"]
                func_name = r["func_name"]

                # 发送补发的 tool_preview:start（如果不是流式阶段已发送）
                preview_info = preview_snapshot.get(idx, {})
                if not preview_info.get('preview_sent', False):
                    yield f"<!--tool_preview:start:{local_call_id}:{func_name}-->"

                # 参数解析错误的提示
                if r["parse_error"]:
                    yield f"\n❌ 工具 `{r['func_name']}` 参数错误：{r['parse_error']}\n"

                # 更新连续失败计数 + 发送状态标记
                if r["failed"]:
                    consecutive_failures += 1
                    yield f"<!--tool_status:{local_call_id}:error-->"
                else:
                    consecutive_failures = 0
                    yield f"<!--tool_status:{local_call_id}:success-->"

                yield f"<!--tool_preview:end:{local_call_id}-->"

                # 追加 tool 消息到上下文
                final_id_for_context = r["real_tool_call_id"] if r["real_tool_call_id"] else local_call_id
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": final_id_for_context,
                    "content": r["tool_content"]
                })

            # 清理已处理的 tool_preview_active
            for r in exec_results:
                tool_preview_active.pop(r["idx"], None)

            # 刷新计划事件（system_todo 工具产生的 plan 标记）
            from backend.system_tools.todo import pop_plan_events
            for plan_marker in pop_plan_events():
                yield plan_marker

            # 刷新 PPT 预览事件（system_generate_pptx 工具产生的 ppt_preview 标记）
            from backend.system_tools.ppt_generator import pop_ppt_preview_events
            for ppt_marker in pop_ppt_preview_events():
                yield ppt_marker

            yield "<!--tool_calls:end-->"

            # 结束 Step Span
            trace.end_span(step_span.id, "success")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                force_final = True

        # ---------- 最终 token 统计输出 ----------
        # 持久化追踪数据到数据库（TracePanel 通过 API 读取）
        asyncio.create_task(persist_trace(trace))

        if last_step_usage and last_step_usage["completion_tokens"] > 0:
            tokens = last_step_usage["completion_tokens"]
            gen_time = last_step_generation_time
            if tokens < 20 or gen_time < 0.1:
                speed_str = "⚡瞬间完成"
            else:
                speed = tokens / gen_time if gen_time > 0 else 0.0
                speed_str = f"{speed:.2f} token/s"
            token_info = {
                "final_answer_usage": last_step_usage,
                "total_usage_all_steps": total_usage_all_steps,
                "speed": speed_str
            }
            yield f"\n<!--token_usage:{json.dumps(token_info)}-->"
