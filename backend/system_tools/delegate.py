# backend/system_tools/delegate.py
"""
子智能体委派工具：将独立子任务委派给轻量级子智能体并行处理。

支持三种模式：
  - single（默认）：单个子 Agent
  - sequential：多个 Agent 链式执行（前一个输出作为后一个的上下文）
  - parallel：多个 Agent 并行执行同一任务，结果合并

每个 Agent 可通过 agents 参数定义角色（role_name、goal、backstory）和工具集。
也可通过 template_id 使用预设的 Crew 模板。

安全约束：
  - 子智能体最多 3 轮工具调用
  - 子智能体默认仅允许只读工具
  - 子智能体禁止递归调用 system_delegate_task
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from backend.services.crew import run_single_agent, run_sequential, run_parallel, merge_results

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
    agents: Optional[List[Dict]] = None,
    collaboration: str = "single",
    template_id: Optional[int] = None,
    mcp_manager=None,
    skill_registry=None,
    llm_service=None,
    params: Optional[Dict] = None,
    trace_manager=None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    将子任务委派给子智能体执行。

    Args:
        task: 子任务描述（需自包含）
        tools: 允许子智能体使用的工具列表，省略时仅允许只读工具
        agents: 子 Agent 角色定义数组 [{role_name, goal, backstory, tools}]
        collaboration: 协作模式 — "single"｜"sequential"｜"parallel"
        template_id: 预设 Crew 模板 ID（与 agents 互斥）
        mcp_manager: MCP 客户端管理器（由 execute_tool 注入）
        skill_registry: 技能注册表（由 execute_tool 注入）
        llm_service: LLM 服务实例（由 execute_tool 注入）
        params: LLM 参数（由 execute_tool 注入）
        trace_manager: 追踪管理器（由 execute_tool 注入，可选）

    Returns:
        {success, result, agents_results, collaboration, total_tool_calls}
    """
    import logging as _logging
    _log = _logging.getLogger("My Workbench")

    if not llm_service:
        return {"success": False, "error": "子智能体委派需要 LLM 服务实例。"}
    if not task or not task.strip():
        return {"success": False, "error": "task 参数不能为空。"}

    _log.info(f"[DELEGATE] ========== delegate_task START ==========")
    _log.info(f"[DELEGATE] task={task[:100]}... collaboration={collaboration}")

    # 加载模板
    if template_id is not None:
        template = await _load_template(template_id)
        if template:
            agents = template.get("agents", [])
            collaboration = template.get("mode", "sequential")

    # 构建工具列表
    from backend.services.tools import get_local_tools
    local_tools = get_local_tools()

    if tools is not None:
        allowed = set(tools)
    else:
        allowed = set(DEFAULT_SUBAGENT_TOOLS)
    allowed.discard("system_delegate_task")

    mcp_tools = []
    if mcp_manager:
        mcp_tools = await mcp_manager.get_all_tools()

    all_base = list(local_tools) + list(mcp_tools)

    # 不同模式的超时限制（防止子智能体无限挂死）
    _TIMEOUT_SINGLE = 120.0     # 单 Agent 最多 120 秒（研究任务需网络请求）
    _TIMEOUT_SEQUENTIAL = 240.0 # 链式最多 240 秒（每个约 120s）
    _TIMEOUT_PARALLEL = 120.0   # 并行最多 120 秒（同时跑）

    try:
        if collaboration == "single" and (not agents or len(agents) <= 1):
            role = agents[0] if agents else None
            agent_tools = [t for t in all_base if t.get("function", {}).get("name") in allowed]
            if role and role.get("tools"):
                agent_tools = [t for t in all_base if t.get("function", {}).get("name") in set(role["tools"])]
            _log.info(f"[DELEGATE] mode=single, agent_tools={len(agent_tools)}, entering _run_single...")
            result = await asyncio.wait_for(
                _run_single(role, task, llm_service, agent_tools, mcp_manager,
                            skill_registry, params, trace_manager),
                timeout=_TIMEOUT_SINGLE,
            )
            _log.info(f"[DELEGATE] _run_single returned: success={result.get('success')}")
            return result

        elif collaboration == "sequential" and agents:
            result = await asyncio.wait_for(
                _run_sequential_wrapped(
                    task, agents, llm_service, all_base,
                    mcp_manager, skill_registry, params, trace_manager, local_tools,
                ),
                timeout=_TIMEOUT_SEQUENTIAL,
            )
            return result

        elif collaboration == "parallel" and agents:
            result = await asyncio.wait_for(
                _run_parallel_wrapped(
                    task, agents, llm_service, all_base,
                    mcp_manager, skill_registry, params, trace_manager, local_tools,
                ),
                timeout=_TIMEOUT_PARALLEL,
            )
            return result

        else:
            return {"success": False, "error": f"不支持的协作模式: {collaboration}"}

    except asyncio.TimeoutError:
        _log.warning(f"[DELEGATE] TIMEOUT after {_TIMEOUT_SINGLE}s!")
        return {
            "success": False,
            "error": f"子智能体委派超时，任务可能过于复杂。请尝试拆分任务或减少 Agent 数量。",
            "result": "",
        }
    except Exception as e:
        _log.error(f"[DELEGATE] EXCEPTION: {type(e).__name__}: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"子智能体执行异常：{str(e)}",
            "result": "",
        }

async def _run_single(role, task, llm_service, agent_tools, mcp_manager,
                      skill_registry, params, trace_manager):
    """包装 run_single_agent，使其可被 asyncio.wait_for 取消。"""
    result = await run_single_agent(
        task, llm_service, agent_tools,
        role=role, mcp_manager=mcp_manager,
        skill_registry=skill_registry, params=params,
        trace_manager=trace_manager,
    )
    return {
        "success": result["success"],
        "result": result.get("result", ""),
        "error": result.get("error", ""),
        "agents_results": [result],
        "collaboration": "single",
        "total_tool_calls": result.get("tool_calls_count", 0),
    }


async def _run_sequential_wrapped(task, agents, llm_service, all_base,
                                   mcp_manager, skill_registry, params,
                                   trace_manager, local_tools):
    """包装 run_sequential，使其可被 asyncio.wait_for 取消。"""
    results = await run_sequential(
        task, agents, llm_service, all_base,
        mcp_manager=mcp_manager, skill_registry=skill_registry,
        params=params, trace_manager=trace_manager,
        all_local_tools=local_tools,
    )
    merged = merge_results(results, "sequential")
    total_tc = sum(r.get("tool_calls_count", 0) for r in results)
    return {
        "success": True,
        "result": merged,
        "agents_results": results,
        "collaboration": "sequential",
        "total_tool_calls": total_tc,
    }


async def _run_parallel_wrapped(task, agents, llm_service, all_base,
                                 mcp_manager, skill_registry, params,
                                 trace_manager, local_tools):
    """包装 run_parallel，使其可被 asyncio.wait_for 取消。"""
    results = await run_parallel(
        task, agents, llm_service, all_base,
        mcp_manager=mcp_manager, skill_registry=skill_registry,
        params=params, trace_manager=trace_manager,
        all_local_tools=local_tools,
    )
    merged = merge_results(list(results), "parallel")
    total_tc = sum(r.get("tool_calls_count", 0) for r in results)
    return {
        "success": True,
        "result": merged,
        "agents_results": list(results),
        "collaboration": "parallel",
        "total_tool_calls": total_tc,
    }


async def _load_template(template_id: int) -> Optional[Dict]:
    """从数据库加载 Crew 模板。"""
    try:
        from backend.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT name, title, mode, config FROM crew_templates WHERE id = ?",
                (template_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            config = json.loads(row[3]) if isinstance(row[3], str) else row[3]
            return {
                "name": row[0], "title": row[1], "mode": row[2],
                "agents": config.get("agents", []) if isinstance(config, dict) else [],
            }
        finally:
            await db.close()
    except Exception:
        return None
