# backend/services/memory_extractor.py
"""
结构化长期记忆抽取服务（豆包式记忆）。

从一轮「用户输入 + AI 回复」中抽取关于用户本人、值得长期记住的信息，
分类存入 user_memories 表，供后续对话作为「用户画像」注入 system prompt。

设计要点：
  - 只抽取用户明确透露的事实/偏好/项目/关系，不做臆测；
  - 抽取失败静默降级（不影响主对话流程）；
  - 相同内容自动去重（精确匹配，简单够用）。
"""
import json
import re
from typing import List, Dict, Optional

from backend.database import get_db
from backend.bootstrap import logger
from config_loader import config as app_config

# 分类标签（用于前端展示 + system prompt 注入）
CATEGORY_LABELS = {
    "fact": "个人信息",
    "preference": "偏好习惯",
    "project": "项目工作",
    "relationship": "人际关系",
    "other": "其它",
}

_VALID_CATEGORIES = set(CATEGORY_LABELS.keys())

_EXTRACTION_SYSTEM_PROMPT = (
    "你是用户的长期记忆整理助手。你的任务是从一段「用户输入 + AI 回复」中，"
    "提取关于用户本人的、值得长期记住的信息，以便未来对话能更好地理解用户背景。\n\n"
    "只提取用户明确透露过的内容，禁止臆测或补全。信息分类：\n"
    '- "fact"：个人事实（姓名、职业、身份、地点、技能、擅长领域等）\n'
    '- "preference"：偏好、习惯、明确要求（「我喜欢…」「以后都要…」「不要…」）\n'
    '- "project"：正在做的事、项目、目标、进展\n'
    '- "relationship"：用户提到的其他人及其与用户的关系\n'
    '- "other"：其它值得记住的信息\n\n'
    "规则：\n"
    "1. 每条记忆用一句独立、完整的中文陈述，主语用「用户」（如「用户正在开发一个 RAG 知识库应用」）。\n"
    "2. 只保留对未来对话有帮助的、稳定的信息；一次性的闲聊话题不要提取。\n"
    "3. 没有值得记的内容时返回空数组 []。\n"
    "4. 严格只输出 JSON 数组，不要输出任何解释文字，格式："
    '[{"category": "fact", "content": "用户是…"}]'
)


def _extract_text(content) -> str:
    """把可能是 str / list（多模态）的消息内容统一成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return " ".join(parts).strip()
    return ""


async def _call_extraction(service, user_message: str, assistant_message: str) -> List[Dict]:
    """调用当前对话模型抽取结构化记忆。返回解析后的列表，失败返回 []。"""
    if service is None:
        return []

    # 生图 / 视频模型不是文本对话模型，无法做抽取
    if getattr(service, "role", "default") in ("image_gen", "video"):
        return []

    user_block = (
        f"用户输入：\n{user_message[:1500]}\n\n"
        f"AI 回复（节选）：\n{assistant_message[:1500]}"
    )

    try:
        resp = await service.client.chat.completions.create(
            model=service.model_name,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_block},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"[memory_extractor] 抽取调用失败（非致命）：{e}")
        return []

    if not raw:
        return []

    # 去掉可能的代码围栏
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    # 只取第一个 JSON 数组
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    raw = raw[start:end + 1]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[memory_extractor] 抽取结果 JSON 解析失败：{raw[:200]}")
        return []

    if not isinstance(data, list):
        return []

    memories: List[Dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "other")).strip().lower()
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if category not in _VALID_CATEGORIES:
            category = "other"
        memories.append({"category": category, "content": content})
    return memories


async def _store_memory(user_id: int, category: str, content: str,
                        chat_id: str, message_id: Optional[int]) -> Optional[int]:
    """插入一条结构化记忆（精确去重）。返回新记忆 id，重复则返回 None。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM user_memories WHERE user_id = ? AND content = ?",
            (user_id, content),
        )
        if await cursor.fetchone():
            return None

        cursor = await db.execute(
            "INSERT INTO user_memories (user_id, category, content, source_chat_id, source_message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, category, content, chat_id, message_id),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def extract_memories_from_turn(
    service,
    user_message: str,
    assistant_message: str,
    user_id: Optional[int],
    chat_id: str,
    message_id: Optional[int] = None,
) -> List[Dict]:
    """
    从一轮对话中抽取并存储结构化长期记忆。

    返回实际写入的记忆列表。整个流程失败静默降级。
    """
    if not user_id:
        return []
    if not getattr(app_config, "memory_extraction_enabled", True):
        return []

    user_text = _extract_text(user_message)
    assistant_text = _extract_text(assistant_message)
    if not user_text or len(user_text) < 4:
        return []

    extracted = await _call_extraction(service, user_text, assistant_text)
    if not extracted:
        return []

    stored: List[Dict] = []
    for mem in extracted:
        mem_id = await _store_memory(
            user_id, mem["category"], mem["content"], chat_id, message_id
        )
        if mem_id is not None:
            mem["id"] = mem_id
            stored.append(mem)

    if stored:
        logger.info(f"[memory_extractor] 已抽取 {len(stored)} 条长期记忆（user_id={user_id}）")
    return stored


async def get_user_profile(user_id: Optional[int], limit: int = 20) -> List[Dict]:
    """获取用户的结构化长期记忆（最新优先），用于注入 system prompt。"""
    if not user_id:
        return []
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, category, content, created_at FROM user_memories "
            "WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "category": r[1], "content": r[2], "created_at": r[3]}
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[memory_extractor] 读取用户画像失败（非致命）：{e}")
        return []
    finally:
        await db.close()
