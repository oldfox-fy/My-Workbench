# backend/services/session_memory.py
"""
会话记忆服务：将 AI 助手的重要回复向量化存储，支持跨对话语义检索。

使用方式：
  1. 自动索引：对话流结束后调用 index_assistant_message()
  2. 检索注入：新对话开始时调用 search_relevant_memories() 获取相关历史
"""
from typing import List, Dict, Optional
from backend.database import get_db
from backend.db import vec_store
from backend.bootstrap import logger


async def index_assistant_message(
    chat_id: str,
    message_id: int,
    content: str,
) -> bool:
    """
    将 AI 回复向量化并存入会话记忆。

    仅当内容 > 200 字符时才索引（过滤简短回复和工具调用结果）。
    失败时静默跳过，不影响主对话流程。
    """
    if len(content) < 200:
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
                "INSERT INTO session_memories (chat_id, message_id, content) VALUES (?, ?, ?)",
                (chat_id, message_id, text_to_embed),
            )
            await db.commit()
            mem_id = cursor.lastrowid

            # 写入向量
            await vec_store.upsert_session([(mem_id, vec)])

            logger.info(f"[session_memory] 已索引消息 {message_id}（{len(text_to_embed)} 字符）")
            return True
        finally:
            await db.close()

    except Exception as e:
        logger.warning(f"[session_memory] 索引失败（非致命）：{e}")
        return False


async def search_relevant_memories(
    query: str,
    k: int = 3,
) -> List[Dict]:
    """
    语义搜索与当前问题相关的历史会话记忆。

    返回格式：[{"content": str, "chat_id": str, "created_at": str, "distance": float}, ...]
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
            cursor = await db.execute(
                f"SELECT id, chat_id, content, created_at FROM session_memories "
                f"WHERE id IN ({placeholders}) ORDER BY created_at DESC",
                mem_ids,
            )
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                results.append({
                    "content": row[2],
                    "chat_id": row[1],
                    "created_at": row[3] if row[3] else "",
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


async def get_recent_memories(k: int = 5) -> List[Dict]:
    """
    获取最近的会话记忆（不依赖向量检索，纯时序）。

    用于新对话初始化时注入背景信息。
    """
    try:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id, chat_id, content, created_at FROM session_memories "
                "ORDER BY created_at DESC LIMIT ?",
                (k,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "content": row[2],
                    "chat_id": row[1],
                    "created_at": row[3] if row[3] else "",
                }
                for row in rows
            ]
        finally:
            await db.close()
    except Exception:
        return []
