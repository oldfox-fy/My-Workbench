# backend/system_tools/delegate.py
"""
子智能体委派工具：将独立子任务委派给轻量级子智能体并行处理。

子智能体拥有独立上下文窗口和受限工具集，完成后将结果汇总回主智能体。
适用于可并行分解的任务，如"把这三个文件分别翻译成英文"。

安全约束：
  - 子智能体最多 3 轮工具调用（防止过度递归）
  - 子智能体默认仅允许只读工具
  - 子智能体禁止再调用 system_delegate_task（防无限递归）
"""
import asyncio
import json
from typing import Any, Dict, List, Optional


# 子智能体默认允许的只读工具
DEFAULT_SUBAGENT_TOOLS = {
    "system_read_file",
    "system_kb_list",
    "system_kb_read",
    "system_kb_search",
    "system_get_weather",
    "system_read_file_list",
}


async def delegate_task(
    task: str,
    tools: Optional[List[str]] = None,
    mcp_manager=None,
    skill_registry=None,
    llm_service=None,
    params: Optional[Dict] = None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    将子任务委派给子智能体执行。

    Args:
        task: 子任务描述（需自包含，子智能体看不到主对话历史）
        tools: 允许子智能体使用的工具列表。省略时仅允许只读工具。
        mcp_manager: MCP 客户端管理器（由 execute_tool 注入）
        skill_registry: 技能注册表（由 execute_tool 注入）
        llm_service: LLM 服务实例（由 execute_tool 注入）
        params: LLM 参数（由 execute_tool 注入）

    Returns:
        {"success": bool, "result": str, "tool_calls_count": int, "error": str}
    """
    if not llm_service:
        return {"success": False, "error": "子智能体委派需要 LLM 服务实例，但未能获取。"}

    if not task or not task.strip():
        return {"success": False, "error": "task 参数不能为空。"}

    # 确定允许的工具集
    if tools is not None:
        allowed = set(tools)
    else:
        allowed = set(DEFAULT_SUBAGENT_TOOLS)

    # 禁止子智能体递归委派
    allowed.discard("system_delegate_task")

    try:
        # 构建子智能体工具列表（延迟导入避免循环依赖）
        from backend.services.tools import get_local_tools
        local_tools = get_local_tools()
        sub_tools = [t for t in local_tools if t["function"]["name"] in allowed]

        # 追加 MCP 工具（仅白名单内的）
        if mcp_manager:
            mcp_tools = await mcp_manager.get_all_tools()
            sub_tools.extend([t for t in mcp_tools if t["function"]["name"] in allowed])

        # 构建子智能体消息
        system_prompt = (
            "你是一个高效的子任务执行者。请仅使用给定的工具完成任务，"
            "完成后直接返回结果，不要询问用户。保持输出简洁。"
        )
        sub_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        # 收集子智能体响应
        response_parts = []
        tool_calls_count = 0
        sub_params = (params or {}).copy()

        async for chunk in llm_service.generate_response(
            messages=sub_messages,
            enable_tools=True,
            tools=sub_tools,
            request=None,  # 子智能体无客户端连接
            mcp_manager=mcp_manager,
            skill_registry=skill_registry,
            params=sub_params,
            message_id=None,  # 子智能体不记录到 DB
            max_steps=3,
            excluded_tools={"system_delegate_task"},
        ):
            # 过滤掉 SSE 标记，收集纯文本
            if chunk.startswith("<!--"):
                if chunk.startswith("<!--tool_status:") and ":success-->" in chunk:
                    tool_calls_count += 1
                continue
            response_parts.append(chunk)

        result_text = "".join(response_parts).strip()

        return {
            "success": True,
            "result": result_text,
            "tool_calls_count": tool_calls_count,
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"子智能体执行异常：{str(e)}",
            "result": "",
            "tool_calls_count": 0,
        }
