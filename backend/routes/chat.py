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
from backend.services.model_router import (
    detect_input_role, get_model_by_role, get_default_model,
    should_switch_for_images, _looks_vision_capable,
)
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

# ── 命令执行意图检测：按需自动注入 system_run_command ──
# 避免该工具始终挂载导致每次请求都携带额外 token，同时确保用户需要时可用。
_COMMAND_INTENT_KEYWORDS = [
    # 中文信号词
    "运行", "执行", "编译", "构建", "安装", "生成",
    "跑一下", "跑脚本", "跑个", "写个脚本", "命令行", "终端",
    # 英文信号词
    "run", "execute", "compile", "build", "install",
    "generate", "node ", "python ", "pip ", "npm ",
    "npx ", "yarn ", "pnpm ", "cargo ", "go ",
    # 产物生成类（通常需要跑脚本产出文件）
    "生成ppt", "ppt", "pptx", "生成报告", "导出",
    "下载文件", "生成图表", "生成图片",
]


def _detect_command_intent(messages: list) -> bool:
    """扫描最近几条用户消息，判断是否需要命令执行能力。"""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return False
    # 只检查最近的用户消息（避免历史对话干扰）
    recent = user_msgs[-1]
    content = recent.get("content", "")
    if isinstance(content, list):
        # 多模态消息：提取文本部分
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return False
    text = content.lower()
    for kw in _COMMAND_INTENT_KEYWORDS:
        kw_lower = kw.lower().rstrip()
        if len(kw_lower) <= 3:
            # 短关键词用词边界匹配，防止 "ppt" 误匹配 "prompt"、"run" 误匹配 "runtime"
            if re.search(rf'\b{re.escape(kw_lower)}\b', text):
                return True
        else:
            if kw_lower in text:
                return True
    return False

class ModelConfig(BaseModel):
    type: str
    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    thinking: str = 'enabled'
    role: str = 'default'  # 模型角色：用于 LLMService 区分 API 路由（image_gen → images.generate）


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    enable_tools: bool = False
    llm_config: Optional[ModelConfig] = None
    profile_id: Optional[int] = None
    message_id: Optional[int] = None
    auto_switch: bool = False  # 是否启用智能模型切换


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

        # ── 智能模型路由 ──
        routed_role = None  # 记录被路由到的角色
        route_notice = None  # 诊断消息（将插入到流开头）
        if request.auto_switch and request.llm_config:
            detected_role = detect_input_role(request.messages, request.enable_tools)

            # 如果当前模型已支持所需能力，不切换
            current_model = request.llm_config.model_name or ""
            need_switch = True
            if detected_role == "vision" and _looks_vision_capable(current_model):
                need_switch = False
            elif detected_role == "default":
                need_switch = False  # 无需特殊能力，不切换
            elif detected_role == "image_gen":
                need_switch = True  # 生图总是需要切到专门的生图模型

            if need_switch:
                routed_model = await get_model_by_role(detected_role)
                if routed_model and routed_model.get("modelName") != current_model:
                    routed_role = detected_role
                    old_name = request.llm_config.model_name
                    request.llm_config.model_name = routed_model["modelName"]
                    request.llm_config.type = routed_model["type"]
                    request.llm_config.base_url = routed_model.get("baseUrl")
                    request.llm_config.api_key = routed_model.get("apiKey", "")
                    request.llm_config.role = routed_model.get("role", "default")  # 传递角色给 LLMService
                    role_labels = {"vision": "视觉", "audio": "语音", "reasoning": "推理", "fast": "快速", "image_gen": "生图"}
                    label = role_labels.get(detected_role, detected_role)
                    route_notice = f"\n🔄 智能切换：检测到{label}需求，从 `{old_name}` 切换到 `{routed_model['modelName']}`\n"
                elif detected_role != "default":
                    role_labels = {"vision": "视觉", "audio": "语音", "reasoning": "推理", "image_gen": "生图"}
                    label = role_labels.get(detected_role, detected_role)
                    route_notice = (f"\n💡 检测到{label}输入，但未配置对应角色模型。"
                                    f"请在设置中为模型添加「{label}」角色后即可自动切换。"
                                    f"当前继续使用 `{current_model}` 处理。\n")

        if request.llm_config:
            llm_cfg = request.llm_config
            model_role = getattr(llm_cfg, 'role', 'default') or 'default'
            # 前端可能不发送 role（用户手动选模型而非自动切换），从数据库兜底查找
            if model_role == 'default':
                from backend.services.model_router import lookup_model_role
                model_role = await lookup_model_role(
                    llm_cfg.model_name or "",
                    llm_cfg.base_url or "",
                )
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
                    role=model_role,
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
                    role=model_role,
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

        # ── 智能按需注入 system_run_command ──
        # 即使用户未开启角色/未选角色，只要对话中检测到命令执行意图，
        # 就将 system_run_command 注入当前请求的工具列表。避免该工具
        # 始终挂载造成的 token 浪费，同时确保「生成 PPT / 运行脚本」
        # 等场景下 LLM 能直接调用。
        if _detect_command_intent(messages):
            runner_tool = [t for t in local_tools if t["function"]["name"] == "system_run_command"]
            # 去重：避免已通过角色白名单添加后重复
            existing_names = {t["function"]["name"] for t in tools}
            for t in runner_tool:
                if t["function"]["name"] not in existing_names:
                    tools.append(t)

        # 如果携带了 profile_id，获取角色信息
        if request.enable_tools and request.profile_id is not None:
            # ── 全能助手 (profile_id=0)：全部工具 + 全部 MCP + 全部技能，不过滤 ──
            if request.profile_id == 0:
                # 所有本地工具（包含 disabled 白名单中的高级工具）
                tools = local_tools.copy()
                # 所有 MCP 工具
                mcp_tools = await get_mcp_tools(mcp_manager)
                for t in mcp_tools:
                    if t["function"]["name"] not in {x["function"]["name"] for x in tools}:
                        tools.append(t)
                # 所有 code 型技能定义
                if skill_registry:
                    code_defs = skill_registry.code_tool_definitions()
                    for d in code_defs:
                        if d["function"]["name"] not in {x["function"]["name"] for x in tools}:
                            tools.append(d)
                # 注入所有 prompt 型技能指令
                if skill_registry:
                    expanded = skill_registry.expand_for_all_prompt_skills()
                    if expanded.get("instructions"):
                        profile_skill_prompt = "\n\n".join(expanded["instructions"])
            else:
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

        # 工具列表按名称排序，确保每次请求的工具定义顺序一致（Prompt Cache 友好）
        tools.sort(key=lambda t: t.get("function", {}).get("name", ""))

        messages = [m for m in messages if m["role"] != "system"]

        # ── 系统提示词组装（静态前缀 → 动态后缀，最大化 Prompt Cache 命中） ──
        system_prompt = BASE_SYSTEM_PROMPT.replace("{{workspace_path}}", backend.workspace_path)
        system_prompt = system_prompt.replace("{{kb_path}}", getattr(backend, "kb_path", "") or backend.workspace_path)
        # profile_prompt（固定角色 → 静态内容，放在时间前面以利缓存）
        if profile_prompt:
            system_prompt = f"{system_prompt}\n\n### 角色扮演\n\n{profile_prompt}"
        # 技能指令（同上，同角色固定）
        if profile_skill_prompt:
            system_prompt = f"{system_prompt}\n\n### 已启用技能\n\n{profile_skill_prompt}"

        # ── 动态后缀（放在 system prompt 最末尾，不影响前面的静态缓存前缀） ──
        dynamic_suffix_parts = [f"当前时间：{get_current_time()}"]
        dynamic_suffix_parts.append(f"工作区路径：{backend.workspace_path}")

        # 注入相关历史记忆（跨对话语义检索，随查询变化）
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
                        "\n\n### 相关历史记忆\n"
                        "以下是你在其他对话中曾讨论过的相关内容，可在回复时结合这些信息理解用户背景：\n"
                        + "\n".join(memory_lines)
                    )
                    dynamic_suffix_parts.append(memory_section)

        # 将动态部分追加到 system prompt 末尾
        system_prompt = system_prompt + "\n\n---\n" + "\n".join(dynamic_suffix_parts)

        messages.insert(0, {"role": "system", "content": system_prompt})

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
            if route_notice:
                yield route_notice

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
async def get_tools_info(
    mcp_manager=Depends(get_mcp_manager),
    skill_registry=Depends(get_skill_registry),
):
    all_tools = await get_all_tools(mcp_manager, skill_registry)
    tool_json = {}
    for tool in all_tools:
        meta = tool.get("function", {}).get("meta", {})
        tool_json[tool["function"]["name"]] = {
            'title': tool["function"]["title"],
            'description': tool["function"]["description"],
            'is_skill': bool(meta.get("skill", False)),
            'isolated': bool(meta.get("isolated", False)),
        }
    return tool_json

    
@router.get("/system-info")
async def get_system_info():
    return {
        "workspace_dir": backend.workspace_path,
        "upload_dir": config.uploads_dir,
        "local_ip": get_local_ip(),
    }


# ──────────── 自动更新检查 ────────────
import time as _time
import os as _os

_update_cache = {"ts": 0, "data": None}

@router.get("/check-update")
async def check_update():
    """检查 GitHub Release 是否有新版本。缓存 1 小时。"""
    global _update_cache
    now = _time.time()
    if _update_cache["data"] and now - _update_cache["ts"] < 3600:
        return _update_cache["data"]

    current_version = "0.0.0"
    version_file = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "version.txt")
    try:
        with open(version_file, "r") as f:
            current_version = f.read().strip()
    except Exception:
        pass

    result = {"has_update": False, "current_version": current_version, "latest_version": "", "download_url": ""}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/repos/lumneo/LumNeo/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "MyWorkbench"},
            )
            if resp.status_code == 200:
                release = resp.json()
                latest = release.get("tag_name", "").lstrip("v")
                if latest and latest != current_version:
                    result["has_update"] = True
                    result["latest_version"] = latest
                    # 找 exe/zip 下载链接
                    for asset in release.get("assets", []):
                        name = asset.get("name", "")
                        if name.endswith((".exe", ".zip")):
                            result["download_url"] = asset.get("browser_download_url", "")
                            break
                    if not result["download_url"]:
                        result["download_url"] = release.get("html_url", "")
    except Exception:
        pass  # 网络不可达时静默失败

    _update_cache = {"ts": now, "data": result}
    return result