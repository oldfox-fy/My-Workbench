# backend/services/session_memory.py
"""
会话记忆服务：将「用户输入 + AI 回复」双向向量化存储，支持跨对话语义检索。

使用方式：
  1. 自动索引：对话流结束后调用 index_memory()（role=user / role=assistant）
  2. 检索注入：新对话开始时调用 search_relevant_memories() 获取相关历史
  3. 用户隔离：所有读写按 user_id 过滤，多用户互不影响
"""
from typing import List, Dict, Optional
from backend.database import get_db
from backend.db import vec_store
from backend.bootstrap import logger

# 用户消息与 AI 回复采用不同的索引阈值：
#   - AI 回复常含大量解释性文字，仅索引较长的（>200 字符）避免噪声；
#   - 用户输入往往是短句但信息密度高（如「我叫张三」「我在做 XX」），
#     只要不是「继续」「好的」这类无信息量回复即可索引。
_ASSISTANT_MIN_CHARS = 200
_USER_MIN_CHARS = 8


async def index_memory(
    chat_id: str,
    message_id: int,
    content: str,
    role: str = "assistant",
    user_id: Optional[int] = None,
) -> bool:
    """
    将一条消息（用户输入或 AI 回复）向量化并存入会话记忆。

    仅当内容超过对应阈值时才索引；失败时静默跳过，不影响主对话流程。
    """
    content = (content or "").strip()
    if not content:
        return False

    min_chars = _ASSISTANT_MIN_CHARS if role == "assistant" else _USER_MIN_CHARS
    if len(content) < min_chars:
        return False

    try:
        from backend.services.embedding import get_embedder

        # 截断长文本
        text_to_embed = content[:2000]

        # 获取 embedder 并生成向量
        embedder = await get_embedder()
        dim = embedder.cfg.dim
        if dim <= 0:
            logger.warning("[session_memory] embedding 维度未配置，跳过索引")
            return False

        vec = await embedder.embed_one(text_to_embed)
        if not vec:
            return False

        # 确保向量表存在
        await vec_store.ensure_session_table(dim)

        # 插入会话记忆元数据
        db = await get_db()
        try:
            cursor = await db.execute(
                "INSERT INTO session_memories (chat_id, message_id, content, role, user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, message_id, text_to_embed, role, user_id),
            )
            await db.commit()
            mem_id = cursor.lastrowid

            # 写入向量
            await vec_store.upsert_session([(mem_id, vec)])

            logger.info(f"[session_memory] 已索引 {role} 消息 {message_id}（{len(text_to_embed)} 字符）")
            return True
        finally:
            await db.close()

    except Exception as e:
        logger.warning(f"[session_memory] 索引失败（非致命）：{e}")
        return False


async def index_assistant_message(
    chat_id: str,
    message_id: int,
    content: str,
    user_id: Optional[int] = None,
) -> bool:
    """兼容旧接口：仅索引 AI 回复。"""
    return await index_memory(chat_id, message_id, content, role="assistant", user_id=user_id)


async def search_relevant_memories(
    query: str,
    k: int = 3,
    user_id: Optional[int] = None,
) -> List[Dict]:
    """
    语义搜索与当前问题相关的历史会话记忆（用户输入 + AI 回复）。

    返回格式：[{"content": str, "role": str, "chat_id": str, "created_at": str, "distance": float}, ...]
    按语义距离升序。
    """
    if not query or len(query) < 10:
        return []

    try:
        from backend.services.embedding import get_embedder

        embedder = await get_embedder()
        dim = embedder.cfg.dim
        if dim <= 0:
            return []

        # 确保向量表存在
        await vec_store.ensure_session_table(dim)

        # 向量化查询
        query_vec = await embedder.embed_one(query[:500])
        if not query_vec:
            return []

        # KNN 搜索
        hits = await vec_store.search_sessions(query_vec, k)
        if not hits:
            return []

        # JOIN 会话记忆元数据
        mem_ids = [h[0] for h in hits]
        distances = {h[0]: h[1] for h in hits}

        db = await get_db()
        try:
            placeholders = ",".join("?" * len(mem_ids))
            if user_id is None:
                sql = (
                    f"SELECT id, chat_id, content, role, created_at FROM session_memories "
                    f"WHERE id IN ({placeholders}) ORDER BY created_at DESC"
                )
                params: List = list(mem_ids)
            else:
                sql = (
                    f"SELECT id, chat_id, content, role, created_at FROM session_memories "
                    f"WHERE id IN ({placeholders}) AND user_id = ? ORDER BY created_at DESC"
                )
                params = [*mem_ids, user_id]

            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                results.append({
                    "content": row[2],
                    "role": row[3] if row[3] else "assistant",
                    "chat_id": row[1],
                    "created_at": row[4] if row[4] else "",
                    "distance": distances.get(row[0], 999.0),
                })

            # 按距离排序
            results.sort(key=lambda r: r["distance"])
            return results[:k]
        finally:
            await db.close()

    except Exception as e:
        logger.warning(f"[session_memory] 检索失败（非致命）：{e}")
        return []


async def get_recent_memories(k: int = 5, user_id: Optional[int] = None) -> List[Dict]:
    """
    获取最近的会话记忆（不依赖向量检索，纯时序）。

    用于新对话初始化时注入背景信息。
    """
    try:
        db = await get_db()
        try:
            if user_id is None:
                cursor = await db.execute(
                    "SELECT id, chat_id, content, role, created_at FROM session_memories "
                    "ORDER BY created_at DESC LIMIT ?",
                    (k,),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, chat_id, content, role, created_at FROM session_memories "
                    "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, k),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "content": row[2],
                    "role": row[3] if row[3] else "assistant",
                    "chat_id": row[1],
                    "created_at": row[4] if row[4] else "",
                }
                for row in rows
            ]
        finally:
            await db.close()
    except Exception:
        return []
