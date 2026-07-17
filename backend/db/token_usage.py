# backend/db/token_usage.py
"""
Token 用量持久化读写。

每次 LLM 对话完成后记录 prompt/completion/total tokens，
供使用统计面板按用户聚合查询。
"""
from typing import Optional, Dict, Any
from backend.database import get_db


async def record_token_usage(
    user_id: int,
    chat_id: str = "",
    model_name: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """记录一次对话的 token 消耗。"""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO token_usage
               (user_id, chat_id, model_name, prompt_tokens, completion_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, model_name, prompt_tokens, completion_tokens, total_tokens),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_token_stats(user_id: int) -> Dict[str, Any]:
    """获取指定用户的 token 统计。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT
                 COUNT(*) as request_count,
                 COALESCE(SUM(prompt_tokens), 0) as total_prompt,
                 COALESCE(SUM(completion_tokens), 0) as total_completion,
                 COALESCE(SUM(total_tokens), 0) as total_all
               FROM token_usage WHERE user_id = ?""",
            (user_id,),
        )
        row = await cursor.fetchone()
        return {
            "request_count": row[0] or 0,
            "prompt_tokens": row[1] or 0,
            "completion_tokens": row[2] or 0,
            "total_tokens": row[3] or 0,
        }
    finally:
        await db.close()


async def get_token_breakdown_by_model(user_id: int) -> list:
    """按模型拆分指定用户的 token 消耗（用于 TOP N 展示）。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT model_name,
                 COUNT(*) as cnt,
                 COALESCE(SUM(total_tokens), 0) as total
               FROM token_usage WHERE user_id = ?
               GROUP BY model_name ORDER BY total DESC LIMIT 10""",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [{"model": r[0] or "unknown", "count": r[1], "total_tokens": r[2]} for r in rows]
    finally:
        await db.close()
