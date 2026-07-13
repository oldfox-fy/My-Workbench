# backend/services/crew.py
"""
Crew 编排引擎：多 Agent 角色协作。

支持模式：
  - single：单个子 Agent（兼容旧版 delegate）
  - sequential：链式执行，前一个 Agent 输出作为后一个的上下文
  - parallel：并行扇出，所有 Agent 同时执行，结果合并

每个 Agent 可定义 role_name、goal、backstory 和专用工具集。
"""
import json
import asyncio
from typing import Any, Dict, List, Optional


async def run_single_agent(
    task: str,
    llm_service,
    tools: List[Dict],
    role: Optional[Dict] = None,
    mcp_manager=None,
    skill_registry=None,
    params: Optional[Dict] = None,
    trace_manager=None,
) -> Dict[str, Any]:
    """执行单个子 Agent 任务。"""
    system_prompt = _build_agent_prompt(role) if role else "你是一个高效的子任务执行者。请仅使用给定的工具完成任务，完成后直接返回结果，不要询问用户。保持输出简洁。"
    sub_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    sub_span = None
    if trace_manager:
        agent_name = role.get("role_name", "子Agent") if role else "子Agent"
        sub_span = trace_manager.start_sub_agent(agent_name)

    response_parts = []
    tool_calls_count = 0
    sub_params = (params or {}).copy()

    try:
        async for chunk in llm_service.generate_response(
            messages=sub_messages,
            enable_tools=True,
            tools=tools,
            request=None,
            mcp_manager=mcp_manager,
            skill_registry=skill_registry,
            params=sub_params,
            message_id=None,
            max_steps=3,
            excluded_tools={"system_delegate_task"},
        ):
            if chunk.startswith("<!--"):
                if chunk.startswith("<!--tool_status:") and ":success-->" in chunk:
                    tool_calls_count += 1
                continue
            response_parts.append(chunk)

        result_text = "".join(response_parts).strip()

        if sub_span and trace_manager:
            trace_manager.end_span(sub_span.id, "success", result_text[:300])

        return {
            "success": True,
            "result": result_text,
            "tool_calls_count": tool_calls_count,
            "role_name": role.get("role_name", "") if role else "",
        }
    except Exception as e:
        if sub_span and trace_manager:
            trace_manager.end_span(sub_span.id, "error", "", str(e))
        return {
            "success": False,
            "error": str(e),
            "result": "",
            "tool_calls_count": tool_calls_count,
            "role_name": role.get("role_name", "") if role else "",
        }


async def run_sequential(
    task: str,
    agents: List[Dict],
    llm_service,
    tools: List[Dict],
    mcp_manager=None,
    skill_registry=None,
    params: Optional[Dict] = None,
    trace_manager=None,
    all_local_tools: List[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    链式执行：Agent1 → Agent2 → Agent3。
    每个 Agent 接收上一个 Agent 的输出作为额外上下文。
    """
    results = []
    previous_result = ""

    for i, agent_def in enumerate(agents):
        # 构建当前 Agent 的上下文
        agent_task = task
        if previous_result and i > 0:
            agent_task = f"{task}\n\n【前一步的结果】\n{previous_result[:2000]}"

        # 构建工具列表
        agent_tools = _build_agent_tools(agent_def, tools, all_local_tools)

        result = await run_single_agent(
            agent_task, llm_service, agent_tools,
            role=agent_def, mcp_manager=mcp_manager,
            skill_registry=skill_registry, params=params,
            trace_manager=trace_manager,
        )
        results.append(result)

        if result.get("success") and result.get("result"):
            previous_result = result["result"]

    return results


async def run_parallel(
    task: str,
    agents: List[Dict],
    llm_service,
    tools: List[Dict],
    mcp_manager=None,
    skill_registry=None,
    params: Optional[Dict] = None,
    trace_manager=None,
    all_local_tools: List[Dict] = None,
) -> List[Dict[str, Any]]:
    """并行扇出：所有 Agent 同时执行，结果合并。"""
    async def _run_one(agent_def):
        agent_tools = _build_agent_tools(agent_def, tools, all_local_tools)
        return await run_single_agent(
            task, llm_service, agent_tools,
            role=agent_def, mcp_manager=mcp_manager,
            skill_registry=skill_registry, params=params,
            trace_manager=trace_manager,
        )

    coros = [_run_one(a) for a in agents]
    return await asyncio.gather(*coros)


# ── 工具函数 ──

def _build_agent_prompt(role: Optional[Dict]) -> str:
    """根据角色定义构建子 Agent 的系统提示词。"""
    if not role:
        return "你是一个高效的子任务执行者。请仅使用给定的工具完成任务，完成后直接返回结果。保持输出简洁。"
    name = role.get("role_name", "助手")
    goal = role.get("goal", "高效完成任务")
    backstory = role.get("backstory", "")
    parts = [f"你是 **{name}**。", f"目标：{goal}"]
    if backstory:
        parts.append(f"背景：{backstory}")
    parts.append("规则：只使用分配的工具；完成后直接返回结果；用中文回复。")
    return "\n".join(parts)


def _build_agent_tools(
    agent_def: Dict,
    base_tools: List[Dict],
    all_local_tools: List[Dict] = None,
) -> List[Dict]:
    """根据 Agent 定义的工具白名单构建工具列表。"""
    allowed = set(agent_def.get("tools", []))
    if not allowed and all_local_tools:
        # 默认只读工具
        default_readonly = {
            "system_read_file", "system_kb_list", "system_kb_read",
            "system_kb_search", "system_get_weather", "system_read_file_list",
        }
        allowed = default_readonly
    allowed.discard("system_delegate_task")

    result = [t for t in base_tools if t.get("function", {}).get("name") in allowed]
    return result


def merge_results(results: List[Dict[str, Any]], mode: str) -> str:
    """合并多个 Agent 的结果为单一文本。"""
    if mode == "single":
        r = results[0] if results else {}
        return r.get("result", "") if r.get("success") else f"错误: {r.get('error', '')}"

    parts = []
    for i, r in enumerate(results):
        role = r.get("role_name", f"Agent {i+1}")
        if r.get("success"):
            parts.append(f"### {role}\n{r.get('result', '')}")
        else:
            parts.append(f"### {role}\n❌ 错误: {r.get('error', '')}")
    return "\n\n".join(parts)
