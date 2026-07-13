# backend/routes/chat.py
import re
import json
import traceback
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from backend.services.llm_service import LLMService
from backend.services.tools import get_local_tools, get_mcp_tools, get_all_tools
from backend.services.context_compressor import compress_messages
from backend.services.session_memory import search_relevant_memories, index_assistant_message
from backend.database import get_db
from backend.utils.base import resource_path, get_current_time, get_local_ip
from config_loader import config
import backend
import asyncio


router = APIRouter(prefix="/api", tags=["chat"])

BASE_SYSTEM_PROMPT = ""

full_path = resource_path("system_prompt.md")
with open(full_path, 'r', encoding="utf-8") as f:
    BASE_SYSTEM_PROMPT = f.read()

BASE_SYSTEM_PROMPT = BASE_SYSTEM_PROMPT.replace("{{uploads_dir}}", str(config.uploads_dir))


disabled_tools = ['system_write_file', 'system_patch_file', 'system_create_project_tree', 'system_read_file_list', 'system_run_command']
default_tools = ['system_get_weather', 'system_read_file', 'system_kb_list', 'system_kb_read', 'system_kb_search']

class ModelConfig(BaseModel):
    type: str
    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    thinking: str = 'enabled'


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    enable_tools: bool = False
    llm_config: Optional[ModelConfig] = None
    profile_id: Optional[int] = None
    message_id: Optional[int] = None


async def get_mcp_manager(request: Request):
    return request.app.state.mcp_manager


async def get_skill_registry(request: Request):
    return getattr(request.app.state, "skill_registry", None)


@router.post("/chat")
async def chat(
    request: ChatRequest,
    fastapi_request: Request,
    mcp_manager=Depends(get_mcp_manager),
    skill_registry=Depends(get_skill_registry),
):
    try:
        # 1. 创建 LLM 服务实例
        # 从全局配置读取容错参数
        max_retries = getattr(config, 'max_retries', 3)
        base_delay = getattr(config, 'base_delay', 1.0)
        fallback_cfg = getattr(config, 'fallback_config', None)

        if request.llm_config:
            llm_cfg = request.llm_config
            if llm_cfg.type == "local":
                service = LLMService(
                    model_type="local",
                    model_name=llm_cfg.model_name,
                    base_url=llm_cfg.base_url,
                    api_key=llm_cfg.api_key,
                    thinking=llm_cfg.thinking,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    fallback_config=fallback_cfg,
                )
            else:
                if not llm_cfg.api_key:
                    raise HTTPException(status_code=400, detail="线上模型必须提供 API Key")
                service = LLMService(
                    model_type="online",
                    model_name=llm_cfg.model_name,
                    base_url=llm_cfg.base_url,
                    api_key=llm_cfg.api_key,
                    thinking=llm_cfg.thinking,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    fallback_config=fallback_cfg,
                )
        else:
            service = LLMService.instance
            if not service:
                raise HTTPException(status_code=400, detail="请先选择或配置模型")

        # 2. 处理消息和角色
        messages = request.messages.copy()
        local_tools = get_local_tools()
        tools = [t for t in local_tools if t["function"]["name"] in default_tools]
        profile_prompt = ""
        profile_skill_prompt = ""
        params = {}

        # 如果携带了 profile_id，获取角色信息
        if request.enable_tools and request.profile_id is not None:
            db = await get_db()
            cursor = await db.execute(
                "SELECT tools, profile_prompt, temperature, top_p, top_k, frequency_penalty, presence_penalty, skills FROM profiles WHERE id = ?",
                (request.profile_id,)
            )
            row = await cursor.fetchone()
            await db.close()

            if row:
                allowed_tools = json.loads(row[0] or "[]")
                profile_prompt = row[1] or ""
                params = {
                    "temperature": row[2] if row[2] is not None else 1.0,
                    "top_p": row[3] if row[3] is not None else 1.0,
                    "top_k": row[4] if row[4] is not None else 40,
                    "frequency_penalty": row[5] if row[5] is not None else 0.0,
                    "presence_penalty": row[6] if row[6] is not None else 0.0,
                }
                profile_skills = json.loads(row[7] or "[]") if len(row) > 7 and row[7] else []

                # 展开角色勾选的技能：prompt 技能 → 注入指令 + 追加工具白名单；code 技能 → 可调用 function
                allowed_code_tool_names = []
                if skill_registry and profile_skills:
                    expanded = skill_registry.expand_for_profile(profile_skills)
                    if expanded["instructions"]:
                        profile_skill_prompt = "\n\n".join(expanded["instructions"])
                    allowed_tools = list(set(allowed_tools) | expanded["allowed_tools"])
                    allowed_code_tool_names = expanded["code_tool_names"]

                # 筛选工具
                mcp_tools = await get_mcp_tools(mcp_manager) if request.enable_tools else []
                enable_tools = [t for t in local_tools if t["function"]["name"] in disabled_tools]
                enable_tools.extend(mcp_tools)
                use_tools = [t for t in enable_tools if t["function"]["name"] in allowed_tools]
                tools.extend(use_tools)

                # 追加角色可用的 code 型技能定义
                if skill_registry and allowed_code_tool_names:
                    code_defs = skill_registry.code_tool_definitions()
                    tools.extend([d for d in code_defs if d["function"]["name"] in allowed_code_tool_names])

        messages = [m for m in messages if m["role"] != "system"]

        system_prompt = BASE_SYSTEM_PROMPT.replace("{{workspace_path}}", backend.workspace_path)
        system_prompt = system_prompt.replace("{{kb_path}}", getattr(backend, "kb_path", "") or backend.workspace_path)
        system_prompt = system_prompt.replace("{{time_now}}", get_current_time())
        # profile_prompt 加入到 system_prompt 中
        if profile_prompt:
            system_prompt = f"{system_prompt}\n\n ### 角色扮演 \n\n{profile_prompt}"
        # 技能指令（prompt 型技能）加入到 system_prompt 中
        if profile_skill_prompt:
            system_prompt = f"{system_prompt}\n\n ### 已启用技能 \n\n{profile_skill_prompt}"

        # 注入相关历史记忆（跨对话语义检索）
        if request.enable_tools:
            last_user_msg = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    last_user_msg = m.get("content", "")
                    break
            if last_user_msg:
                memories = await search_relevant_memories(last_user_msg, k=3)
                if memories:
                    memory_lines = []
                    for mem in memories:
                        time_str = mem.get("created_at", "")[:10] if mem.get("created_at") else "未知时间"
                        snippet = mem["content"][:120].replace("\n", " ")
                        memory_lines.append(f"- [{time_str}] {snippet}...")
                    memory_section = (
                        "\n\n ### 相关历史记忆 \n"
                        "以下是你在其他对话中曾讨论过的相关内容，可在回复时结合这些信息理解用户背景：\n"
                        + "\n".join(memory_lines)
                    )
                    system_prompt += memory_section

        messages.insert(0, {"role": "system", "content": f"{system_prompt}\n\n{backend.workspace_path}"})

        REASONING_BLOCK = re.compile(
            r'<!--reasoning:start-->.*?<!--reasoning:end:\d+\.?\d*-->',
            re.DOTALL
        )
        MISC_MARKERS = re.compile(
            r'<!--(?:token_usage|reasoning):[^>]*-->'
        )

        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                content = REASONING_BLOCK.sub('', content)
                content = MISC_MARKERS.sub('', content)
                msg["content"] = content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part["text"]
                        text = REASONING_BLOCK.sub('', text)
                        text = MISC_MARKERS.sub('', text)
                        part["text"] = text

        # 3. 上下文压缩（长对话自动压缩中间部分，防止 token 溢出）
        if request.enable_tools and len(messages) > 12:
            messages = compress_messages(messages, max_tokens=8000, keep_recent=10)

        # 4. 流式响应（含自动会话记忆索引）
        async def stream_with_memory():
            """流式响应的同时收集纯文本，结束后自动索引到会话记忆。"""
            collected_text = []
            async for chunk in service.generate_response(
                messages=messages,
                enable_tools=request.enable_tools,
                tools=tools,
                request=fastapi_request,
                mcp_manager=mcp_manager,
                params=params,
                message_id=request.message_id,
                skill_registry=skill_registry,
            ):
                yield chunk
                # 收集非标记文本用于索引
                if chunk and not chunk.startswith("<!--"):
                    collected_text.append(chunk)

            # 流结束后：后台索引当前 AI 回复
            if request.enable_tools and collected_text:
                full_text = "".join(collected_text).strip()
                # 查找当前对话的 chat_id（从消息中推断）
                chat_id = None
                for m in messages:
                    # chat_id 不在消息中，我们用 message_id 查找
                    pass
                if request.message_id and full_text:
                    # 通过 message_id 查找 chat_id
                    try:
                        db = await get_db()
                        cursor = await db.execute(
                            "SELECT chat_id FROM messages WHERE id = ?",
                            (request.message_id,)
                        )
                        row = await cursor.fetchone()
                        await db.close()
                        if row:
                            asyncio.create_task(index_assistant_message(
                                chat_id=row[0],
                                message_id=request.message_id,
                                content=full_text,
                            ))
                    except Exception:
                        pass  # 索引失败不影响主流程

        return StreamingResponse(
            stream_with_memory(),
            media_type="text/event-stream"
        )
    except Exception as e:
        error_trace = traceback.format_exc()
        raise HTTPException(
            status_code=500, 
            detail=f"打包环境运行崩溃，详细堆栈如下:\n{error_trace}"
        )


@router.get("/tools")
async def get_tools(mcp_manager=Depends(get_mcp_manager)):
    local_tools = get_local_tools()
    enable_tools = [t for t in local_tools if t["function"]["name"] in disabled_tools]
    mcp_tools = await get_mcp_tools(mcp_manager)
    enable_tools.extend(mcp_tools)
    return {"tools": enable_tools}

@router.get("/tools-info")
async def get_tools_info(mcp_manager=Depends(get_mcp_manager)):
    all_tools = await get_all_tools(mcp_manager)
    tool_json = {}
    for tool in all_tools:
        tool_json[tool["function"]["name"]] = {
            'title': tool["function"]["title"],
            'description': tool["function"]["description"],
        }
    return tool_json

    
@router.get("/system-info")
async def get_system_info():
    return {
        "workspace_dir": backend.workspace_path,
        "upload_dir": config.uploads_dir,
        "local_ip": get_local_ip(),
    }