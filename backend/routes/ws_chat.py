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

        # 普通文本
        if text and not text.startswith("<!--"):
            await ws.send_json({"type": "chunk", "content": text})

    # 流正常结束
    await ws.send_json({"type": "done"})


async def _handle_chat(ws: WebSocket, data: dict, cancel_event: asyncio.Event):
    """处理 chat 消息：构建 LLMService → 流式返回结果。"""
    try:
        llm_cfg = data.get("llm_config", {})
        if llm_cfg:
            service = LLMService(
                model_type=llm_cfg.get("type", "online"),
                model_name=llm_cfg.get("model_name", ""),
                base_url=llm_cfg.get("base_url"),
                api_key=llm_cfg.get("api_key"),
                thinking=llm_cfg.get("thinking", "enabled"),
                max_retries=getattr(app_config, "max_retries", 3),
                base_delay=getattr(app_config, "base_delay", 1.0),
                fallback_config=getattr(app_config, "fallback_config", None),
            )
        else:
            service = LLMService.instance
            if not service:
                await ws.send_json({"type": "error", "message": "请先配置模型"})
                return

        messages = data.get("messages", [])
        enable_tools = data.get("enable_tools", False)
        profile_id = data.get("profile_id")
        params = data.get("params", {})

        # 构建工具（简化版，与 SSE 端点保持一致的核心逻辑）
        tools = None
        if enable_tools:
            from backend.services.tools import get_all_tools
            tools = await get_all_tools()
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

        messages = [m for m in messages if m.get("role") != "system"]
        messages.insert(0, {"role": "system", "content": system_prompt})

        # 流式生成
        gen = service.generate_response(
            messages=messages,
            enable_tools=enable_tools,
            tools=tools,
            params=params,
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
                chat_task = asyncio.create_task(_handle_chat(websocket, data, cancel_event))

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
