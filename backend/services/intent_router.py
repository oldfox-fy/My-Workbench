# backend/services/intent_router.py
"""
意图路由器：方案 A（关键词规则）+ 方案 B（LLM 轻量分类器）混合。

流程：
  第一步：关键词快速检测（零延迟，对标 _detect_command_intent）
    → 高置信度命中 → 直接路由
    → 未命中 → 进入第二步

  第二步：LLM 轻量分类（~100-200ms，~50-100 tokens）
    → 补充关键词无法覆盖的模糊意图
    → 分类器返回意图标签 + 置信度
    → 低于阈值 → 默认标准模式

支持的意图类别：
  - simple_qa：简单事实问答（快速回答，无需复杂工具）
  - kb_query：知识库检索（自动注入 system_kb_search）
  - deep_research：深度研究分析（多步探索，全量工具）
  - code_task：代码编写/脚本执行（自动注入 system_run_command）
  - content_create：内容创作/PPT/文档生成
  - standard：默认标准模式
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

# ── 方案 A：关键词规则 ──

_KB_QUERY_KEYWORDS = [
    # 中文
    "知识库", "我的笔记", "搜索笔记", "查找笔记", "检索笔记",
    "笔记里有", "知识库里有", "我的知识库", "查一下笔记",
    "回忆", "之前记录的", "我记过的", "我的文档",
    "资料库", "找一下", "帮我查", "查查",
    # 英文
    "search my notes", "my knowledge base", "find in notes",
    "look up in my", "check my notes",
]

_DEEP_RESEARCH_KEYWORDS = [
    # 中文 — 多步分析/深度任务
    "深入分析", "全面评估", "调研", "竞品分析", "对比分析",
    "架构设计", "技术方案", "系统设计", "源码分析",
    "审查", "审计", "优化方案", "重构方案",
    "帮我写一个完整的", "设计一个", "规划",
    # 英文
    "deep dive", "comprehensive analysis", "architecture review",
    "system design", "technical proposal",
]

_SIMPLE_QA_KEYWORDS = [
    # 中文 — 显然只需 1 轮回答
    "什么意思", "是什么", "定义", "解释一下",
    "今天天气", "现在几点", "今天是几号",
    "你好", "谢谢", "再见",
    "翻译", "总结一下这段",
    # 英文
    "what is", "what does", "define", "explain briefly",
    "hello", "thanks", "goodbye",
    "translate", "summarize this",
]

_CODE_TASK_KEYWORDS = [
    # 中文
    "写代码", "写脚本", "写一个程序", "编写函数",
    "debug", "调试", "修复 bug", "报错",
    "实现一个", "写个", "写一个",
    # 英文
    "write code", "write a script", "implement a function",
    "debug", "fix bug", "fix this error",
    "create a program",
]


@dataclass
class IntentResult:
    """意图路由结果"""
    intent: str  # simple_qa | kb_query | deep_research | code_task | content_create | standard
    confidence: str  # "high"（关键词命中）| "llm"（LLM 分类）| "fallback"（降级默认）
    max_steps: int
    max_tokens: int
    auto_inject_tools: List[str]  # 自动注入的工具名列表
    prompt_style: str  # "concise" | "kb" | "standard" | "code"
    reasoning: str  # 诊断信息


def _extract_last_user_text(messages: list) -> str:
    """提取最后一条用户消息的纯文本。"""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            return str(content)
    return ""


def _keyword_detect(text: str) -> Optional[str]:
    """
    方案 A：关键词快速检测。
    返回意图标签，或 None 表示未命中。
    """
    text_lower = text.lower()

    # 按优先级检测（先匹配更明确的意图）

    # 知识库查询（最明确——提到"笔记""知识库"）
    for kw in _KB_QUERY_KEYWORDS:
        if kw in text_lower:
            return "kb_query"

    # 深度研究
    for kw in _DEEP_RESEARCH_KEYWORDS:
        if kw in text_lower:
            return "deep_research"

    # 代码任务
    for kw in _CODE_TASK_KEYWORDS:
        if kw in text_lower:
            return "code_task"

    # 简单问答（最后检测，因为关键词更通用）
    for kw in _SIMPLE_QA_KEYWORDS:
        if kw in text_lower:
            return "simple_qa"

    return None


# ── 方案 B：LLM 轻量分类器 ──

_INTENT_CLASSIFY_PROMPT = """你是一个意图分类器。根据用户消息，判断其意图类别。

类别定义：
- simple_qa：简单事实问答、打招呼、翻译、定义解释。问题可以在 1 轮内回答，不需要搜索或复杂工具。
- kb_query：用户想在自己的笔记/知识库中查找信息。关键词："我的笔记""知识库""我记录的""搜索我的"。
- deep_research：需要多步骤分析、深度研究、技术方案设计、代码审查、竞品分析等复杂任务。
- code_task：编写/修改/调试代码、运行脚本。
- content_create：创建 PPT、文档、报告等内容。
- standard：以上均不符合，或无法明确判断。

用户消息：
{user_message}

请只返回 JSON（不要其他任何文字）：
{{"intent": "<类别>", "confidence": <0.0-1.0>}}"""


async def _llm_classify(user_message: str, llm_service=None) -> Tuple[str, float]:
    """
    方案 B：LLM 轻量分类。
    返回 (intent_label, confidence)。
    分类失败时返回 ("standard", 0.0)。
    """
    if not llm_service:
        return "standard", 0.0

    try:
        # 用极简参数调用 LLM（max_tokens 很小，temperature=0 确定性输出）
        classify_prompt = _INTENT_CLASSIFY_PROMPT.format(user_message=user_message[:500])
        classify_messages = [
            {"role": "user", "content": classify_prompt},
        ]

        # 直接调用非流式 API（更快）
        import json as _json
        response = await llm_service.client.chat.completions.create(
            model=llm_service.model_name,
            messages=classify_messages,
            max_tokens=50,
            temperature=0.0,
            stream=False,
        )

        raw = response.choices[0].message.content.strip() if response.choices else ""
        # 清理可能的 markdown 包裹
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        result = _json.loads(raw)
        intent = result.get("intent", "standard")
        confidence = float(result.get("confidence", 0.0))

        valid_intents = {"simple_qa", "kb_query", "deep_research", "code_task", "content_create", "standard"}
        if intent not in valid_intents:
            intent = "standard"

        return intent, confidence
    except Exception:
        return "standard", 0.0


# ── 意图 → 路由参数映射 ──

_INTENT_PARAMS = {
    "simple_qa": {
        "max_steps": 3,
        "max_tokens": 4096,
        "auto_inject_tools": [],
        "prompt_style": "concise",
    },
    "kb_query": {
        "max_steps": 5,
        "max_tokens": 8192,
        "auto_inject_tools": ["system_kb_search"],
        "prompt_style": "kb",
    },
    "deep_research": {
        "max_steps": 25,
        "max_tokens": 16384,
        "auto_inject_tools": ["system_kb_search"],
        "prompt_style": "standard",
    },
    "code_task": {
        "max_steps": 15,
        "max_tokens": 8192,
        "auto_inject_tools": ["system_run_command"],
        "prompt_style": "code",
    },
    "content_create": {
        "max_steps": 20,
        "max_tokens": 16384,
        "auto_inject_tools": ["system_generate_pptx", "system_run_command"],
        "prompt_style": "standard",
    },
    "standard": {
        "max_steps": 25,
        "max_tokens": 16384,
        "auto_inject_tools": [],
        "prompt_style": "standard",
    },
}

# prompt_style → 对应的系统提示词追加内容
_PROMPT_STYLE_SUFFIX = {
    "concise": (
        "\n\n**当前模式：快捷回答**\n"
        "尽可能简洁直接地回答用户问题，1-2 段为宜。不需要使用工具，不需要深入分析。"
    ),
    "kb": (
        "\n\n**当前模式：知识库检索**\n"
        "用户的个人知识库中有相关笔记。优先使用 system_kb_search 检索知识库，"
        "基于检索结果回答，并在回答中引用来源。"
    ),
    "code": (
        "\n\n**当前模式：代码任务**\n"
        "用户需要编写或调试代码。写代码后如需运行验证，可使用 system_run_command。"
        "输出代码时注意语法正确性和错误处理。"
    ),
    "standard": "",  # 不追加额外指令
}


async def detect_intent(
    messages: list,
    llm_service=None,
    llm_threshold: float = 0.7,
) -> IntentResult:
    """
    主入口：混合意图检测（方案 A + B）。

    Args:
        messages: 用户消息列表
        llm_service: LLMService 实例（用于方案 B 分类，可为 None）
        llm_threshold: LLM 分类置信度阈值（低于此值降级为 standard）

    Returns:
        IntentResult 对象
    """
    user_text = _extract_last_user_text(messages)
    if not user_text:
        return IntentResult(
            intent="standard", confidence="fallback",
            max_steps=25, max_tokens=16384,
            auto_inject_tools=[], prompt_style="standard",
            reasoning="无用户文本，使用默认模式",
        )

    # 第一步：关键词快速检测
    keyword_intent = _keyword_detect(user_text)
    if keyword_intent:
        params = _INTENT_PARAMS[keyword_intent]
        return IntentResult(
            intent=keyword_intent, confidence="high",
            max_steps=params["max_steps"],
            max_tokens=params["max_tokens"],
            auto_inject_tools=params["auto_inject_tools"],
            prompt_style=params["prompt_style"],
            reasoning=f"关键词命中 → {keyword_intent}",
        )

    # 第二步：LLM 轻量分类
    if llm_service:
        intent, confidence = await _llm_classify(user_text, llm_service)
        if confidence >= llm_threshold and intent != "standard":
            params = _INTENT_PARAMS[intent]
            return IntentResult(
                intent=intent, confidence="llm",
                max_steps=params["max_steps"],
                max_tokens=params["max_tokens"],
                auto_inject_tools=params["auto_inject_tools"],
                prompt_style=params["prompt_style"],
                reasoning=f"LLM 分类 → {intent}（置信度 {confidence:.2f}）",
            )

    # 降级：默认标准模式
    params = _INTENT_PARAMS["standard"]
    return IntentResult(
        intent="standard", confidence="fallback",
        max_steps=params["max_steps"],
        max_tokens=params["max_tokens"],
        auto_inject_tools=params["auto_inject_tools"],
        prompt_style=params["prompt_style"],
        reasoning=f"降级默认 → standard（关键词未命中，LLM 分类置信度不足或不可用）",
    )


def get_prompt_suffix(prompt_style: str) -> str:
    """获取指定 prompt_style 的系统提示词追加内容。"""
    return _PROMPT_STYLE_SUFFIX.get(prompt_style, "")
