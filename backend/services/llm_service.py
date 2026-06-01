# backend/services/llm_service.py
import uuid
import json
import time
import base64
from fastapi import Request
from openai import AsyncOpenAI, APIError
from typing import List, Dict, AsyncGenerator, Optional
from backend.services.tools import get_all_tools, execute_tool


class LLMService:
    instance: Optional["LLMService"] = None

    def __init__(self, 
                 model_type: str, 
                 model_name: str, 
                 api_key: str = "", 
                 base_url: str = None, 
                 thinking: str = 'enabled'):
        self.model_type = model_type
        self.model_name = model_name
        self.thinking = thinking
        self.client = AsyncOpenAI(api_key=api_key or None, base_url=base_url)

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        enable_tools: bool = False,
        tools: Optional[List[Dict]] = None,
        request: Optional[Request] = None,
        mcp_manager = None,
        params: Dict = None,
    ) -> AsyncGenerator[str, None]:
        params = params or {}
        
        # ---------- 图像生成分支 ----------
        if "image" in self.model_name.lower():
            # 提取用户消息作为提示词
            prompt = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    prompt = msg.get("content", "")
                    break

            if not prompt:
                yield "❌ 未找到有效的用户提示词，无法生成图像。"
                return

            # 图像生成参数
            size = params.get("size", "1024x768")
            quality = params.get("quality", "standard")
            n = params.get("n", 1)

            try:
                # 调用图像生成 API，不设置 response_format
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
                
                # 优先使用 url 字段（标准 OpenAI 图像 API 默认返回）
                if hasattr(img_data, 'url') and img_data.url:
                    image_url = img_data.url
                    # 以 Markdown 图片格式输出，前端可直接渲染
                    yield f"![生成的图片]({image_url})"
                else:
                    # 降级处理：如果确实没有 url，尝试 b64_json（但不符合用户要求，仅作提示）
                    if hasattr(img_data, 'b64_json') and img_data.b64_json:
                        yield "⚠️ 图像生成服务仅返回 base64 数据，无法提供直接链接。"
                    else:
                        yield "❌ 图像生成服务未返回图片 URL 或 base64 数据。"
                return

            except APIError as e:
                yield f"❌ 图像生成 API 错误：{e.message}"
            except Exception as e:
                yield f"❌ 图像生成失败：{str(e)}"
            return   # 结束，不再执行后续文本生成逻辑
        
        # ---------- 原有文本生成 + 工具调用分支 ----------
        current_messages = messages.copy()

        if tools is None and enable_tools:
            tools = await get_all_tools(mcp_manager)

        reasoning_start_time = None
        final_usage = None
        
        pure_generation_time = 0.0

        MAX_STEPS = 6
        
        for step in range(MAX_STEPS):
            tool_call_started = False
            tool_status_id = ""
            if request and await request.is_disconnected():
                break

            tool_calls = {}
            reasoning_buffer = ""
            in_reasoning = False

            kwargs = {
                "model": self.model_name,
                "messages": current_messages,
                "stream": True,
                "temperature": params.get('temperature', 1.0),
                "top_p": params.get('top_p', 0.95),
                "frequency_penalty": params.get('frequency_penalty', 0.0),
                "presence_penalty": params.get('presence_penalty', 0.0),
                "stream_options": {"include_usage": True},
                "extra_body": {
                    "top_k": params.get('top_k', 20),
                    "chat_template_kwargs": {"enable_thinking": self.thinking == "enabled", "preserve_thinking": True},
                    "enable_thinking": self.thinking == "enabled",
                    "preserve_thinking": True
                }
            }
            
            if step == MAX_STEPS - 1:
                yield "\n⚠️ 工具调用次数已达上限，正在基于已收集信息生成最终总结...\n"
                current_messages.append({
                    "role": "user",
                    "content": "【系统指令】你的工具调用次数已达最大限制。请立即放弃尝试调用工具，根据上面已经收集到的上下文信息，直接回答我的问题并进行最终总结。"
                })
                kwargs["messages"] = current_messages
            elif tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            try:
                response = await self.client.chat.completions.create(**kwargs)

            except APIError as e:
                yield f"\n❌ 模型服务错误：{e.message}"
                break

            first_token_time = None

            async for chunk in response:
                if request and await request.is_disconnected():
                    break

                if chunk.choices:
                    d = chunk.choices[0].delta
                    if first_token_time is None:
                        first_token_time = time.time()

                if not chunk.choices:
                    if hasattr(chunk, 'usage') and chunk.usage:
                        final_usage = chunk.usage
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
                    if not tool_call_started:
                        tool_status_id = f"tool-{uuid.uuid4().hex[:6]}"
                        yield f"\n<!--status:show:{tool_status_id}-->🔧 准备执行操作..."
                        tool_call_started = True
                    for tc_delta in tool_calls_data:
                        if not tc_delta.id and tool_calls:
                            last_id = list(tool_calls.keys())[-1]
                            target = tool_calls[last_id]
                            if tc_delta.function:
                                target["function"]["name"] += (tc_delta.function.name or "")
                                target["function"]["arguments"] += (tc_delta.function.arguments or "")
                            continue

                        tc_id = tc_delta.id
                        if tc_id not in tool_calls:
                            tool_calls[tc_id] = {
                                "id": tc_id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            }
                        if tc_delta.function:
                            tool_calls[tc_id]["function"]["name"] += (tc_delta.function.name or "")
                            tool_calls[tc_id]["function"]["arguments"] += (tc_delta.function.arguments or "")

                    # 每收到任何工具调用片段，立刻把当前 tool_calls 的快照发给前端
                    if tool_calls:  # 如果有工具调用数据就发送
                        snapshot = {}
                        for tid, tc in tool_calls.items():
                            snapshot[tid] = {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"]
                            }
                        yield f"<!--tool_preview:{json.dumps(snapshot)}-->"
                elif delta_content:
                    yield delta_content

            if first_token_time is not None:
                pure_generation_time += (time.time() - first_token_time)

            if in_reasoning:
                reasoning_time = time.time() - reasoning_start_time
                yield f"<!--reasoning:end:{reasoning_time:.2f}-->"

            if tool_calls:
                if request and await request.is_disconnected():
                    break

            if not tool_calls:
                break

            valid_calls = {}
            for tid, tc in tool_calls.items():
                if tc["function"]["name"].strip():
                    valid_calls[tid] = tc
                else:
                    yield "\n⚠️ 检测到无效工具调用（名称空白），已忽略。\n"

            if not valid_calls:
                break
            else:
                if tool_status_id:
                    yield f"<!--status:hide:{tool_status_id}-->"

            yield "\n<!--tool_calls:start-->🔧 检测到工具调用..."

            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": list(valid_calls.values())
            }
            current_messages.append(assistant_msg)

            for tc in valid_calls.values():
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                try:
                    result = await execute_tool(func_name, args, mcp_manager)
                except Exception as e:
                    error_msg = str(e)
                    if len(error_msg) > 1000:
                        error_msg = error_msg[:1000] + "...(错误信息过长已截断)"
                    result = f"工具执行出错: {error_msg}"
                result_str = str(result)
                json_str = json.dumps({'name': func_name, 'arguments': args})
                b64_str = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
                yield f"<!--tool_call:{b64_str}-->"
                yield f"<!--tool_result:{json.dumps({'name': func_name, 'result': result_str})}-->"

                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str
                })
            yield "<!--tool_calls:end-->"
            
        if final_usage:
            try:
                usage_dict = final_usage.model_dump()
            except AttributeError:
                usage_dict = dict(final_usage)

            completion_tokens = usage_dict.get('completion_tokens', 0) or 0
            details = usage_dict.get('completion_tokens_details') or {}
            reasoning_tokens = details.get('reasoning_tokens', 0) or 0

            if pure_generation_time > 0:
                speed = completion_tokens / pure_generation_time
            else:
                speed = 0.0

            usage_dict['speed'] = f"{speed:.2f} token/s"
            usage_dict['total_tokens'] = completion_tokens
            usage_dict['reasoning_tokens'] = reasoning_tokens
            yield f"\n<!--token_usage:{json.dumps(usage_dict)}-->"