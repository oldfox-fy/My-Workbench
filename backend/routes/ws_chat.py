# backend/routes/ws_chat.py
"""
WebSocket 聊天端点：双向通信，支持即时取消、工具审批回传。

协议（JSON）：
  客户端 → 服务端:
    {type:"chat", messages, enable_tools, profile_id, llm_config, ...}
    {type:"cancel"}
    {type:"approval_reply", call_id, approved}

  服务端 → 客户端:
    {type:"chunk", content}
    {type:"reasoning", content, time}
    {type:"tool_preview", call_id, name}
    {type:"tool_status", call_id, status}
    {type:"tool_approval", call_id, tool_name, args_preview}
    {type:"done", usage}
    {type:"error", message}
"""
import json
import asyncio
from fastapi import WebSocket, WebSocketDisconnect

from backend.services.llm_service import LLMService, set_approval_result
from backend.services.tools import get_local_tools, get_mcp_tools
from backend.services.model_router import (
    detect_input_role, get_model_by_role, _looks_vision_capable,
)
from backend.database import get_db
from backend.utils.base import resource_path, get_current_time
from config_loader import config as app_config
import backend


# ──────────── 工具审批：WebSocket 专用通道 ────────────
_ws_approval_events: dict = {}  # call_id → asyncio.Event


def set_ws_approval(call_id: str, approved: bool):
    """WebSocket 审批回传处理。"""
    set_approval_result(call_id, approved)  # 复用 SSE 审批机制
    ev = _ws_approval_events.pop(call_id, None)
    if ev:
        ev.set()


async def _stream_to_ws(ws: WebSocket, generator, cancel_event: asyncio.Event):
    """将 LLMService.generate_response 的 SSE 输出转为 WebSocket JSON 消息。"""
    approval_regex_pattern = None  # lazy import

    async for chunk in generator:
        if cancel_event.is_set():
            await ws.send_json({"type": "done", "cancelled": True})
            return

        if not chunk:
            continue

        text = chunk.strip() if isinstance(chunk, str) else chunk

        # 工具审批
        if text.startswith("<!--tool_approval:"):
            parts = text.replace("<!--tool_approval:", "").replace("-->", "").split(":", 2)
            if len(parts) >= 3:
                call_id, tool_name, args_preview = parts[0], parts[1], parts[2]
                await ws.send_json({"type": "tool_approval", "call_id": call_id,
                                    "tool_name": tool_name, "args_preview": args_preview})
            continue

        # 工具预览
        if text.startswith("<!--tool_preview:start:"):
            parts = text.replace("<!--tool_preview:start:", "").replace("-->", "").split(":", 1)
            if len(parts) >= 2:
                await ws.send_json({"type": "tool_preview", "call_id": parts[0], "name": parts[1]})
            continue

        # 工具状态
        if text.startswith("<!--tool_status:"):
            parts = text.replace("<!--tool_status:", "").replace("-->", "").split(":", 1)
            if len(parts) >= 2:
                await ws.send_json({"type": "tool_status", "call_id": parts[0], "status": parts[1]})
            continue

        # 工具调用块边界
        if text.startswith("<!--tool_calls:"):
            await ws.send_json({"type": "tool_block", "action": "start" if ":start" in text else "end"})
            continue

        # 推理标记
        if text.startswith("<!--reasoning:"):
            continue  # 推理内容已在 chunk 中直接传递

        # Token 用量
        if text.startswith("<!--token_usage:"):
            try:
                usage_json = text.replace("<!--token_usage:", "").replace("-->", "")
                usage = json.loads(usage_json)
                await ws.send_json({"type": "done", "usage": usage})
            except Exception:
                await ws.send_json({"type": "done"})
            return

        # 计划事件
        if text.startswith("<!--plan:create:"):
            try:
                json_str = text.replace("<!--plan:create:", "").replace("-->", "")
                await ws.send_json({"type": "plan_event", "action": "create", "data": json.loads(json_str)})
            except Exception:
                pass
            continue
        if text.startswith("<!--plan:update:"):
            try:
                json_str = text.replace("<!--plan:update:", "").replace("-->", "")
                await ws.send_json({"type": "plan_event", "action": "update", "data": json.loads(json_str)})
            except Exception:
                pass
            continue

        # Span 追踪事件
        if text.startswith("<!--span:start:"):
            parts = text.replace("<!--span:start:", "").replace("-->", "").split(":", 2)
            if len(parts) >= 3:
                await ws.send_json({"type": "span_event", "action": "start", "span_id": parts[0], "span_type": parts[1], "name": parts[2]})
            continue
        if text.startswith("<!--span:end:"):
            parts = text.replace("<!--span:end:", "").replace("-->", "").split(":", 2)
            if len(parts) >= 3:
                await ws.send_json({"type": "span_event", "action": "end", "span_id": parts[0], "status": parts[1], "duration_ms": parts[2]})
            continue

        # 普通文本
        if text and not text.startswith("<!--"):
            await ws.send_json({"type": "chunk", "content": text})

    # 流正常结束
    await ws.send_json({"type": "done"})


async def _handle_chat(ws: WebSocket, data: dict, cancel_event: asyncio.Event,
                   skill_registry=None, mcp_manager=None):
    """处理 chat 消息：构建 LLMService → 流式返回结果。"""
    try:
        llm_cfg = data.get("llm_config", {})
        messages = data.get("messages", [])
        enable_tools = data.get("enable_tools", False)
        auto_switch = data.get("auto_switch", False)

        # ── 智能模型路由 ──
        if auto_switch and llm_cfg:
            detected_role = detect_input_role(messages, enable_tools)

            current_model = llm_cfg.get("model_name", "")
            need_switch = True
            if detected_role == "vision" and _looks_vision_capable(current_model):
                need_switch = False
            elif detected_role == "default":
                need_switch = False
            elif detected_role == "image_gen":
                need_switch = True  # 生图总是需要切到专门的生图模型

            if need_switch:
                routed_model = await get_model_by_role(detected_role)
                if routed_model and routed_model.get("modelName") != current_model:
                    role_labels = {"vision": "视觉", "audio": "语音", "reasoning": "推理", "fast": "快速", "image_gen": "生图"}
                    label = role_labels.get(detected_role, detected_role)
                    await ws.send_json({"type": "chunk", "content": f"\n🔄 智能切换：检测到{label}需求 → `{routed_model['modelName']}`\n"})
                    llm_cfg = {
                        "type": routed_model["type"],
                        "model_name": routed_model["modelName"],
                        "base_url": routed_model.get("baseUrl"),
                        "api_key": routed_model.get("apiKey", ""),
                        "thinking": llm_cfg.get("thinking", "enabled"),
                        "role": routed_model.get("role", "default"),
                    }
                elif detected_role != "default":
                    role_labels = {"vision": "视觉", "audio": "语音", "reasoning": "推理", "image_gen": "生图"}
                    label = role_labels.get(detected_role, detected_role)
                    await ws.send_json({"type": "chunk", "content": f"\n💡 检测到{label}输入，未配置对应角色模型，继续使用当前模型。\n"})

        if llm_cfg:
            model_role = llm_cfg.get("role", "default") or "default"
            # 前端可能不发送 role，从数据库兜底查找
            if model_role == "default":
                from backend.services.model_router import lookup_model_role
                model_role = await lookup_model_role(
                    llm_cfg.get("model_name", ""),
                    llm_cfg.get("base_url", ""),
                )
            service = LLMService(
                model_type=llm_cfg.get("type", "online"),
                model_name=llm_cfg.get("model_name", ""),
                base_url=llm_cfg.get("base_url"),
                api_key=llm_cfg.get("api_key"),
                thinking=llm_cfg.get("thinking", "enabled"),
                max_retries=getattr(app_config, "max_retries", 3),
                base_delay=getattr(app_config, "base_delay", 1.0),
                fallback_config=getattr(app_config, "fallback_config", None),
                role=model_role,
            )
        else:
            service = LLMService.instance
            if not service:
                await ws.send_json({"type": "error", "message": "请先配置模型"})
                return

        profile_id = data.get("profile_id")
        params = data.get("params", {})
        profile_prompt_value = ""
        profile_skill_prompt = ""

        # 构建工具（对齐 SSE 端点的 profile 过滤逻辑）
        tools = None
        if enable_tools:
            from backend.services.tools import get_all_tools, get_local_tools as _get_ws_local, get_mcp_tools as _get_ws_mcp
            from backend.routes.chat import default_tools, disabled_tools, _detect_command_intent

            if profile_id == 0:
                # 全能助手：所有工具 + 所有 MCP + 所有技能
                tools = await get_all_tools()
                if skill_registry:
                    expanded = skill_registry.expand_for_all_prompt_skills()
                    if expanded.get("instructions"):
                        profile_skill_prompt = "\n\n".join(expanded["instructions"])
            elif profile_id is not None:
                # 普通角色：按白名单过滤
                db = await get_db()
                cursor = await db.execute(
                    "SELECT tools, profile_prompt, temperature, top_p, top_k, frequency_penalty, presence_penalty, skills FROM profiles WHERE id = ?",
                    (profile_id,)
                )
                row = await cursor.fetchone()
                await db.close()

                if row:
                    allowed_tools = json.loads(row[0] or "[]")
                    profile_prompt_value = row[1] or ""
                    params = {
                        "temperature": row[2] if row[2] is not None else 1.0,
                        "top_p": row[3] if row[3] is not None else 1.0,
                        "top_k": row[4] if row[4] is not None else 40,
                        "frequency_penalty": row[5] if row[5] is not None else 0.0,
                        "presence_penalty": row[6] if row[6] is not None else 0.0,
                    }
                    profile_skills = json.loads(row[7] or "[]") if len(row) > 7 and row[7] else []

                    local_tools = _get_ws_local()
                    tools = [t for t in local_tools if t["function"]["name"] in default_tools]

                    # 智能注入 system_run_command
                    if _detect_command_intent(messages):
                        runner_tool = [t for t in local_tools if t["function"]["name"] == "system_run_command"]
                        existing_names = {t["function"]["name"] for t in tools}
                        for t in runner_tool:
                            if t["function"]["name"] not in existing_names:
                                tools.append(t)

                    # 展开技能
                    allowed_code_tool_names = []
                    if skill_registry and profile_skills:
                        expanded = skill_registry.expand_for_profile(profile_skills)
                        if expanded["instructions"]:
                            profile_skill_prompt = "\n\n".join(expanded["instructions"])
                        allowed_tools = list(set(allowed_tools) | expanded["allowed_tools"])
                        allowed_code_tool_names = expanded["code_tool_names"]

                    # 筛选 disabled + MCP 工具
                    mcp_tools = await _get_ws_mcp(mcp_manager)
                    enable_tools_list = [t for t in local_tools if t["function"]["name"] in disabled_tools]
                    enable_tools_list.extend(mcp_tools)
                    use_tools = [t for t in enable_tools_list if t["function"]["name"] in allowed_tools]
                    tools.extend(use_tools)

                    # 追加 code 型技能定义
                    if skill_registry and allowed_code_tool_names:
                        code_defs = skill_registry.code_tool_definitions()
                        tools.extend([d for d in code_defs if d["function"]["name"] in allowed_code_tool_names])
                else:
                    tools = await get_all_tools()
            else:
                # 无 profile_id：仅 default_tools
                local_tools = _get_ws_local()
                tools = [t for t in local_tools if t["function"]["name"] in default_tools]
                if _detect_command_intent(messages):
                    runner_tool = [t for t in local_tools if t["function"]["name"] == "system_run_command"]
                    existing_names = {t["function"]["name"] for t in tools}
                    for t in runner_tool:
                        if t["function"]["name"] not in existing_names:
                            tools.append(t)

            if tools:
                tools.sort(key=lambda t: t.get("function", {}).get("name", ""))

        # 系统提示词
        system_prompt = ""
        try:
            spath = resource_path("system_prompt.md")
            with open(spath, encoding="utf-8") as f:
                system_prompt = f.read()
        except Exception:
            pass
        system_prompt = system_prompt.replace("{{uploads_dir}}", str(app_config.uploads_dir))
        system_prompt = system_prompt.replace("{{workspace_path}}", backend.workspace_path)
        system_prompt = system_prompt.replace("{{kb_path}}", getattr(backend, "kb_path", "") or backend.workspace_path)
        system_prompt = system_prompt.replace("{{time_now}}", get_current_time())

        # 注入角色提示词
        if profile_prompt_value:
            system_prompt = system_prompt + "\n\n### 角色扮演\n\n" + profile_prompt_value
        if profile_skill_prompt:
            system_prompt = system_prompt + "\n\n### 已启用技能\n\n" + profile_skill_prompt

        messages = [m for m in messages if m.get("role") != "system"]
        messages.insert(0, {"role": "system", "content": system_prompt})

        # 流式生成
        ws_message_id = data.get("message_id")
        gen = service.generate_response(
            messages=messages,
            enable_tools=enable_tools,
            tools=tools,
            params=params,
            message_id=ws_message_id,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
        )
        await _stream_to_ws(ws, gen, cancel_event)

    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})


# ──────────── WebSocket 端点 ────────────

async def ws_chat_endpoint(websocket: WebSocket):
    """主 WebSocket 端点。"""
    await websocket.accept()
    cancel_event = asyncio.Event()
    chat_task = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            if msg_type == "chat":
                # 取消前一个生成（如果有）
                if chat_task and not chat_task.done():
                    cancel_event.set()
                    chat_task.cancel()
                    try:
                        await chat_task
                    except asyncio.CancelledError:
                        pass
                cancel_event.clear()
                chat_task = asyncio.create_task(
                    _handle_chat(websocket, data, cancel_event,
                                 skill_registry=getattr(websocket.app.state, "skill_registry", None),
                                 mcp_manager=getattr(websocket.app.state, "mcp_manager", None)))

            elif msg_type == "cancel":
                cancel_event.set()
                if chat_task and not chat_task.done():
                    chat_task.cancel()
                await websocket.send_json({"type": "done", "cancelled": True})

            elif msg_type == "approval_reply":
                call_id = data.get("call_id", "")
                approved = data.get("approved", False)
                set_ws_approval(call_id, approved)
                await websocket.send_json({"type": "approval_ack", "call_id": call_id})

            else:
                await websocket.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        pass
    finally:
        cancel_event.set()
        if chat_task and not chat_task.done():
            chat_task.cancel()
