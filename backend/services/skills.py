# backend/services/skills.py
"""
Skill 技能注册与执行引擎。

职责：
- 从数据库加载已启用的 Skill，构建 OpenAI function-calling 定义（与 tools.py 同构）。
- prompt 型：不产出可调用 function，仅提供 instruction（注入系统提示）与
             tools 白名单（展开为该角色可调用工具）。
- code   型：产出一个 skill_<name> function；调用时在受限命名空间执行源码的
             run(**kwargs) 并返回结果。支持上下文隔离（isolated）。

热更新：注册表挂在 app.state.skill_registry，增删改后调 reload() 即时生效，无需重启。

安全说明：code 型执行使用受限 exec（限制内建、按白名单放行 import）。这是尽力而为的
沙箱，非强隔离；仅管理员可写入代码，UI 会明确提示风险。
"""
import json
import asyncio
import inspect
from typing import Any, Dict, List, Optional

from backend.bootstrap import logger
from backend.db import skills as skills_db

# code 型技能的调用前缀（对齐 mcp_ / system_ 的命名习惯）
SKILL_PREFIX = "skill_"

# 受限执行环境允许的内建函数（安全白名单）
_SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "len", "list", "map", "max", "min", "range", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "print", "isinstance",
    "reversed", "repr", "format",
}

# 受限执行环境允许 import 的模块（安全白名单）
_ALLOWED_IMPORTS = {
    "json", "math", "re", "datetime", "random", "statistics",
    "collections", "itertools", "functools", "string", "textwrap",
    "urllib", "urllib.parse", "base64", "hashlib", "time",
}


class SkillRegistry:
    """内存中的技能注册表，反映数据库中已启用的技能。"""

    def __init__(self):
        # name -> skill dict
        self._skills: Dict[str, Dict[str, Any]] = {}

    async def reload(self) -> None:
        """从数据库重新加载所有已启用技能。"""
        try:
            enabled = await skills_db.list_skills(only_enabled=True)
            self._skills = {s["name"]: s for s in enabled}
            logger.info(f"Skill 注册表已加载，共 {len(self._skills)} 个启用技能。")
        except Exception as e:
            logger.error(f"加载 Skill 注册表失败: {e}", exc_info=True)
            self._skills = {}

    # ---------- 查询 ----------

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self._skills.get(name)

    def all_enabled(self) -> List[Dict[str, Any]]:
        return list(self._skills.values())

    def code_tool_definitions(self) -> List[Dict[str, Any]]:
        """把 code 型技能转成 OpenAI function 定义（可被 LLM 调用）。"""
        defs = []
        for s in self._skills.values():
            if s["skill_type"] != "code":
                continue
            params = s.get("parameters") or {"type": "object", "properties": {}}
            defs.append({
                "type": "function",
                "function": {
                    "name": f"{SKILL_PREFIX}{s['name']}",
                    "title": s.get("title", s["name"]),
                    "description": s.get("description", ""),
                    "parameters": params,
                    "meta": {"skill": True, "isolated": bool(s.get("isolated"))},
                },
            })
        return defs

    def expand_for_profile(self, skill_names: List[str]) -> Dict[str, Any]:
        """
        为某角色勾选的技能，展开出：
        - instructions: 需注入系统提示的 prompt 技能指令列表
        - allowed_tools: prompt 技能带来的工具白名单（去重）
        - code_tool_names: 该角色可用的 code 技能对应的 function 名（skill_<name>）
        仅纳入已启用技能。
        """
        instructions: List[str] = []
        allowed_tools: set = set()
        code_tool_names: List[str] = []
        for name in skill_names or []:
            s = self._skills.get(name)
            if not s:
                continue
            if s["skill_type"] == "prompt":
                if s.get("instruction"):
                    instructions.append(
                        f"【技能：{s.get('title', name)}】\n{s['instruction']}"
                    )
                for t in s.get("tools", []):
                    allowed_tools.add(t)
            elif s["skill_type"] == "code":
                code_tool_names.append(f"{SKILL_PREFIX}{s['name']}")
        return {
            "instructions": instructions,
            "allowed_tools": allowed_tools,
            "code_tool_names": code_tool_names,
        }

    def expand_for_all_prompt_skills(self) -> Dict[str, Any]:
        """
        展开所有已启用 prompt 型技能的指令和工具白名单。
        用于"全能助手"角色 —— 不按白名单过滤，注入全部 prompt 技能指令。
        """
        instructions: List[str] = []
        for s in self._skills.values():
            if s.get("skill_type") != "prompt":
                continue
            if s.get("instruction"):
                instructions.append(
                    f"【技能：{s.get('title', s['name'])}】\n{s['instruction']}"
                )
        return {"instructions": instructions}

    def is_skill_call(self, func_name: str) -> bool:
        return func_name.startswith(SKILL_PREFIX)

    # ---------- 执行 ----------

    async def execute(self, func_name: str, arguments: Dict[str, Any]) -> Any:
        """执行 code 型技能调用（func_name 形如 skill_<name>）。"""
        name = func_name[len(SKILL_PREFIX):]
        skill = self._skills.get(name)
        if not skill:
            return {"success": False, "error": f"技能 {name} 未找到或未启用"}
        if skill["skill_type"] != "code":
            return {"success": False, "error": f"技能 {name} 不是可执行代码技能"}
        try:
            run_fn = self._compile_run(skill)
        except Exception as e:
            return {"success": False, "error": f"技能代码编译失败：{e}"}

        try:
            if inspect.iscoroutinefunction(run_fn):
                result = await run_fn(**arguments)
            else:
                # 同步函数放线程池，避免阻塞事件循环
                result = await asyncio.to_thread(lambda: run_fn(**arguments))
            return result
        except Exception as e:
            return {"success": False, "error": f"技能执行出错：{e}"}

    def _compile_run(self, skill: Dict[str, Any]):
        """在受限命名空间中编译技能源码，返回其中的 run 函数。"""
        code = skill.get("code") or ""
        if "def run" not in code:
            raise ValueError("技能代码必须定义 run(**kwargs) 函数")

        import builtins as _bi
        safe_builtins = {k: getattr(_bi, k) for k in _SAFE_BUILTINS if hasattr(_bi, k)}

        def _safe_import(name, *args, **kwargs):
            root = name.split(".")[0]
            if name in _ALLOWED_IMPORTS or root in _ALLOWED_IMPORTS:
                return __import__(name, *args, **kwargs)
            raise ImportError(f"技能沙箱禁止导入模块：{name}")

        safe_builtins["__import__"] = _safe_import
        # isolated 时使用全新命名空间（不共享任何模块级状态）
        namespace: Dict[str, Any] = {"__builtins__": safe_builtins}
        exec(compile(code, f"<skill:{skill['name']}>", "exec"), namespace)
        run_fn = namespace.get("run")
        if not callable(run_fn):
            raise ValueError("技能代码必须定义可调用的 run 函数")
        return run_fn
