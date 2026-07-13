# backend/services/context_compressor.py
"""
上下文压缩模块：当对话历史过长时自动压缩，防止 token 溢出。

策略：
  - System 消息完整保留
  - 最近 N 条消息原样保留（维持对话连贯性）
  - 中间消息压缩为一条摘要（纯规则，无额外 LLM 调用）

使用方式：
  from backend.services.context_compressor import compress_messages
  messages = compress_messages(messages, max_tokens=8000, keep_recent=10)
"""

from typing import List, Dict, Any


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """
    粗略预估 token 数。
    中文约 1.5 字符/token，英文约 4 字符/token。
    这里取保守值 2 字符/token 以留足余量。
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part))
        # 工具调用的额外开销（name + arguments）
        tool_calls = msg.get("tool_calls", [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    total_chars += len(func.get("name", "")) + len(func.get("arguments", ""))

    return max(1, total_chars // 2)


def _extract_user_queries(messages: List[Dict[str, Any]], max_per_msg: int = 80) -> str:
    """
    从待压缩消息中提取用户问题要点。
    每条 user 消息取前 max_per_msg 个字符，用分号连接。
    """
    points = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not content:
            continue
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        else:
            continue

        if text:
            if len(text) > max_per_msg:
                text = text[:max_per_msg] + "…"
            points.append(text)

    if not points:
        return "（此前的对话）"
    return "；".join(points[:20])  # 最多保留 20 个要点


def compress_messages(
    messages: List[Dict[str, Any]],
    max_tokens: int = 8000,
    keep_recent: int = 10,
) -> List[Dict[str, Any]]:
    """
    压缩消息列表。

    参数:
        messages: 完整消息列表（含 system 消息）
        max_tokens: 触发压缩的 token 阈值
        keep_recent: 最近保留的消息条数

    返回:
        压缩后的消息列表
    """
    if not messages:
        return messages

    # 估算当前 token 用量
    current_tokens = estimate_tokens(messages)
    if current_tokens <= max_tokens:
        return messages  # 无需压缩

    # 分离 system 消息
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # 如果非 system 消息不多，用不着压缩
    if len(non_system) <= keep_recent:
        return messages

    # 最近 keep_recent 条保留
    recent = non_system[-keep_recent:]
    # 中间部分待压缩
    older = non_system[:-keep_recent]

    if not older:
        return messages

    # 生成摘要
    summary_text = _extract_user_queries(older)
    compressed_count = len(older)
    summary_msg = {
        "role": "user",
        "content": (
            f"【对话摘要】此前的 {compressed_count} 条消息已被压缩为摘要，"
            f"以下是用户的主要提问：\n{summary_text}\n"
            f"请在后续回复中结合以上上下文理解用户意图。"
        )
    }

    # 组装：system + 摘要 + 最近消息
    compressed = system_msgs + [summary_msg] + recent
    return compressed
