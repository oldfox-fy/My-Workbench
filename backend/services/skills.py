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


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度。"""
    try:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
    except Exception:
        return 0.0

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
        # 技能描述 embedding 缓存（name → vector），用于智能选择
        self._skill_embeddings: Dict[str, List[float]] = {}
        self._embedder = None

    async def reload(self) -> None:
        """从数据库重新加载所有已启用技能。"""
        try:
            enabled = await skills_db.list_skills(only_enabled=True)
            self._skills = {s["name"]: s for s in enabled}
            # 清空 embedding 缓存（下次智能选择时重建）
            self._skill_embeddings = {}
            logger.info(f"Skill 注册表已加载，共 {len(self._skills)} 个启用技能。")
        except Exception as e:
            logger.error(f"加载 Skill 注册表失败: {e}", exc_info=True)
            self._skills = {}

    def invalidate_embedding(self, name: str):
        """使特定 skill 的 embedding 缓存失效。"""
        self._skill_embeddings.pop(name, None)

    async def _ensure_embeddings(self, skill_names: List[str]) -> None:
        """确保指定技能名称的 embedding 已缓存。"""
        to_embed = []
        for name in skill_names:
            if name not in self._skill_embeddings and name in self._skills:
                to_embed.append(name)
        if not to_embed:
            return
        try:
            from backend.services.embedding import get_embedder
            if self._embedder is None:
                self._embedder = await get_embedder()
            texts = []
            for name in to_embed:
                sk = self._skills[name]
                desc = (sk.get("description") or sk.get("title") or name)
                texts.append(desc)
            vectors = await self._embedder.embed(texts)
            for name, vec in zip(to_embed, vectors):
                self._skill_embeddings[name] = vec
        except Exception:
            pass  # embedding 不可用时降级为全量注入

    async def select_relevant_skills_with_scores(
        self,
        user_query: str,
        profile_skills: List[str],
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> List[tuple]:
        """
        从角色的技能列表中选出与用户查询最相关的 Top-K 个，
        返回 (skill_name, similarity_score) 元组列表，按分数降序排列。
        如果 embedding 不可用或技能数 ≤ top_k，返回全部（dummy 分数 0.5）。
        """
        available = [s for s in profile_skills if s in self._skills]
        if len(available) <= top_k:
            return [(name, 0.5) for name in available]

        await self._ensure_embeddings(available)
        if not self._skill_embeddings:
            return [(name, 0.5) for name in available]  # embedding 不可用，全量注入

        try:
            from backend.services.embedding import get_embedder
            if self._embedder is None:
                self._embedder = await get_embedder()
            qvec = await self._embedder.embed_one(user_query)
            if not qvec:
                return [(name, 0.5) for name in available]

            # 余弦相似度排序
            scored = []
            for name in available:
                svec = self._skill_embeddings.get(name)
                if svec and len(svec) == len(qvec):
                    sim = _cosine_similarity(qvec, svec)
                    scored.append((name, sim))
                else:
                    scored.append((name, 0.5))  # 无缓存时给中等分数

            scored.sort(key=lambda x: x[1], reverse=True)
            result = [(name, sim) for name, sim in scored[:top_k] if sim >= min_similarity]
            if not result:
                result = [scored[0]]  # 至少选一个
            return result
        except Exception:
            return [(name, 0.5) for name in available]

    async def select_relevant_skills(
        self,
        user_query: str,
        profile_skills: List[str],
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> List[str]:
        """
        从角色的技能列表中选出与用户查询最相关的 Top-K 个。
        委托给 select_relevant_skills_with_scores()，只返回名称。
        """
        scored = await self.select_relevant_skills_with_scores(
            user_query, profile_skills, top_k, min_similarity
        )
        return [name for name, _ in scored]

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

    def build_skill_first_prompt(
        self,
        ranked_skills: List[tuple],
        threshold: float,
    ) -> tuple:
        """
        生成"技能优先"系统提示词指令，同时返回需抑制的系统工具列表。

        ranked_skills: (skill_name, similarity_score) 元组列表，按分数降序。
        threshold: 只有分数 ≥ 此值的技能才会被列入优先指令。

        Returns: (directive_text, suppressed_system_tools_set)
        """
        top = [(name, sim) for name, sim in ranked_skills if sim >= threshold]
        if not top:
            return "", set()

        # 关键词 → 需抑制的系统工具映射
        # 注意：system_run_command 不在此列表中，因为技能优先模式下仍需保留命令执行能力。
        # skill 可通过 prompt 指令引导 LLM 优先使用技能，但不应阻止 LLM 在需要时
        # 执行命令（如 node compile.js 等脚本）来完成技能无法覆盖的任务。
        _TOOL_CONFLICT_MAP = {
            "system_generate_pptx": ["ppt", "pptx", "演示文稿", "幻灯片", "presentation", "slide"],
        }

        suppressed_tools = set()
        skill_lines = []
        for name, _sim in top:
            sk = self._skills.get(name)
            if not sk:
                continue
            title = sk.get("title", name)
            desc = sk.get("description", "")
            skill_type = sk.get("skill_type", "prompt")
            combined = f"{title} {desc} {name}".lower()

            # 检测技能覆盖的系统工具域，标记需抑制的工具
            for tool_name, keywords in _TOOL_CONFLICT_MAP.items():
                if any(kw.lower() in combined for kw in keywords):
                    suppressed_tools.add(tool_name)

            type_label = "🔧 可执行技能" if skill_type == "code" else "📋 提示词技能"
            if skill_type == "code":
                skill_lines.append(
                    f"- **{title}** `skill_{name}`：{desc}\n"
                    f"  （{type_label}：直接调用 `skill_{name}` 函数即可）"
                )
            else:
                skill_lines.append(
                    f"- **{title}** `{name}`：{desc}\n"
                    f"  （{type_label}：请严格按照上述技能的指令执行，不要使用其他工具替代）"
                )

        # 构建抑制工具列表说明
        suppress_lines = []
        if suppressed_tools:
            suppress_lines.append("")
            suppress_lines.append(
                "**重要：** 以下系统工具已被上述技能覆盖，"
                "本次对话中**禁止调用**它们，必须使用对应的技能代替："
            )
            for t in sorted(suppressed_tools):
                suppress_lines.append(f"- ❌ 禁止使用 `{t}`")

        lines = [
            "### ⚠️ 技能优先模式（最高优先级）",
            "以下技能与当前需求高度匹配。**你必须优先使用这些技能来完成任务。**",
            "直接调用技能而不是系统工具。只有在技能确实无法满足需求时才考虑其他方案。",
            "",
        ] + skill_lines + suppress_lines

        return "\n".join(lines), suppressed_tools

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


# ── 共享工具函数：供 SSE 和 WebSocket 两端复用 ──

async def assemble_skill_first_context(
    skill_registry,       # SkillRegistry | None
    profile_skills,       # List[str]
    user_query: str,
    config,               # AppConfig
) -> Dict[str, Any]:
    """
    技能上下文组装（智能选择 + 技能优先判断），供 chat.py 和 ws_chat.py 复用。

    Returns:
        {
            "selected_skills": List[str],        # 选出的技能名称
            "skill_prompt": str,                 # prompt 型技能指令（注入系统提示词）
            "skill_first_directive": str,        # 优先指令（"" 表示不触发）
            "suppressed_tool_names": Set[str],   # 需从工具列表移除的系统工具
            "code_tool_names": List[str],        # code 型技能函数名
            "allowed_tools_extra": Set[str],     # prompt 技能带来的工具白名单
        }
    """
    result = {
        "selected_skills": [],
        "skill_prompt": "",
        "skill_first_directive": "",
        "suppressed_tool_names": set(),
        "code_tool_names": [],
        "allowed_tools_extra": set(),
    }

    if not skill_registry or not profile_skills:
        return result

    # ---- Step 1: 智能技能选择 ----
    se_enabled = getattr(config, "skill_selection_enabled", True)
    top_k = getattr(config, "skill_selection_top_k", 5)
    min_sim = getattr(config, "skill_selection_min_similarity", 0.3)

    if se_enabled and len(profile_skills) > 3:
        scored = await skill_registry.select_relevant_skills_with_scores(
            user_query, profile_skills, top_k=top_k, min_similarity=min_sim
        )
        selected = [name for name, _ in scored]
    else:
        selected = profile_skills
        scored = [(name, 0.5) for name in selected]

    result["selected_skills"] = selected

    # ---- Step 2: 展开选中技能 ----
    expanded = skill_registry.expand_for_profile(selected)
    if expanded["instructions"]:
        result["skill_prompt"] = "\n\n".join(expanded["instructions"])
    result["allowed_tools_extra"] = expanded["allowed_tools"]
    result["code_tool_names"] = expanded["code_tool_names"]

    # ---- Step 3: 技能优先指令（含工具冲突检测）----
    sf_enabled = getattr(config, "skill_first_enabled", True)
    sf_threshold = getattr(config, "skill_first_threshold", 0.4)

    if sf_enabled:
        directive, suppressed = skill_registry.build_skill_first_prompt(
            scored, sf_threshold
        )
        result["skill_first_directive"] = directive
        result["suppressed_tool_names"] = suppressed

    return result
