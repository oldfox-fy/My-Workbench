# backend/services/tools.py
import json
import importlib
import yaml
from typing import Dict
from backend.utils.base import resource_path


def load_tools_from_config(config_path: str):
    full_path = resource_path(config_path)
    with open(full_path, 'r', encoding="utf-8") as f:
        config = yaml.safe_load(f)

    tools_definition = []
    available_tools = {}

    for tool_cfg in config['tools']:
        # 构建工具定义
        tools_definition.append({
            "type": "function",
            "function": {
                "name": tool_cfg['name'],
                "title": tool_cfg['title'],
                "description": tool_cfg['description'],
                "parameters": tool_cfg['parameters'],
                "meta": tool_cfg.get('meta', {})
            }
        })
        # 动态导入函数
        module = importlib.import_module(tool_cfg['module'])
        func = getattr(module, tool_cfg['function_name'])
        available_tools[tool_cfg['name']] = func

    return tools_definition, available_tools


TOOLS_DEFINITION, AVAILABLE_TOOLS = load_tools_from_config("tools_config.yaml")

def get_local_tools():
    """获取本地工具"""
    return TOOLS_DEFINITION.copy()

async def get_all_tools(mcp_manager=None, skill_registry=None):
    """获取所有工具（本地 + MCP + code 型技能），按名称排序以利 prompt cache"""
    tools = TOOLS_DEFINITION.copy()
    if mcp_manager:
        mcp_tools = await mcp_manager.get_all_tools()
        tools.extend(mcp_tools)
    if skill_registry:
        tools.extend(skill_registry.code_tool_definitions())
    # 过滤掉非字典项，按名称排序保证缓存一致性
    clean_tools = [t for t in tools if isinstance(t, dict)]
    clean_tools.sort(key=lambda t: t.get("function", {}).get("name", ""))

    return clean_tools

async def get_mcp_tools(mcp_manager=None):
    """获取MCP工具，按名称排序"""
    if mcp_manager:
        mcp_tools = await mcp_manager.get_all_tools()
        clean_tools = [t for t in mcp_tools if isinstance(t, dict)]
        clean_tools.sort(key=lambda t: t.get("function", {}).get("name", ""))
        return clean_tools
    return []

    return clean_tools

async def execute_tool(func_name: str, arguments: Dict, mcp_manager=None, skill_registry=None, **extra_kwargs) -> str:
    """执行工具，优先本地工具，其次 code 型技能，最后 MCP 工具。
    extra_kwargs 会传递给需要额外上下文的本地工具（如 delegate_task 需要 llm_service）。"""
    if func_name in AVAILABLE_TOOLS:
        result = await AVAILABLE_TOOLS[func_name](**arguments, mcp_manager=mcp_manager, skill_registry=skill_registry, **extra_kwargs)
        return _stringify(result)
    elif skill_registry and skill_registry.is_skill_call(func_name):
        result = await skill_registry.execute(func_name, arguments)
        return _stringify(result)
    elif mcp_manager:
        result = await mcp_manager.call_tool(func_name, arguments)
        return _stringify(result)
    else:
        return f"Error: 工具 {func_name} 未找到"


def _stringify(result) -> str:
    """把工具返回值统一转成字符串。"""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)