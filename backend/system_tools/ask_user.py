# backend/system_tools/ask_user.py
"""
向用户提问工具：LLM 在执行复杂任务时主动暂停，向用户确认/询问。

实现方式：该工具被标记为敏感工具，通过工具审批流弹出带输入框的对话框，
用户输入文本后作为工具结果返回给 LLM。

与普通审批（仅批准/拒绝）不同，本工具返回用户的自由文本回复。
"""
from typing import Any, Dict


async def ask_user(question: str) -> Dict[str, Any]:
    """
    向用户提问并等待回复。

    Args:
        question: 要向用户展示的问题

    Returns:
        {success, answer: "用户的回复", cancelled: false}
    """
    if not question or not question.strip():
        return {"success": False, "error": "question 不能为空"}

    # 实际交互由 llm_service 的审批流处理
    # 这里只需返回占位 — 审批流会注入用户回复
    return {"success": True, "answer": "", "cancelled": False, "_placeholder": True}
