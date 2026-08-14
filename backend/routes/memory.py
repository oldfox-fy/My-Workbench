# backend/routes/memory.py
"""
记忆管理接口：
  - 结构化长期记忆（user_memories）：查看 / 手动新增 / 编辑 / 删除
  - 原始会话记忆（session_memories）：查看 / 删除

所有接口挂在 /api/memory 前缀下，按当前登录用户隔离。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.database import get_db
from backend.db import vec_store
from backend.services.memory_extractor import CATEGORY_LABELS

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _uid(request: Request) -> int:
    return request.state.user["id"]


# ──────────────────────── 统计 ────────────────────────

@router.get("/stats")
async def memory_stats(request: Request):
    """记忆条数统计：结构化记忆（按分类）+ 原始会话记忆（按角色）。"""
    uid = _uid(request)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM user_memories WHERE user_id = ?", (uid,)
        )
        structured_total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT category, COUNT(*) FROM user_memories WHERE user_id = ? GROUP BY category",
            (uid,),
        )
        by_category = {r[0]: r[1] for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT COUNT(*) FROM session_memories WHERE user_id = ?", (uid,)
        )
        raw_total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT role, COUNT(*) FROM session_memories WHERE user_id = ? GROUP BY role",
            (uid,),
        )
        raw_by_role = {r[0]: r[1] for r in await cursor.fetchall()}

        return {
            "structured": {"total": structured_total, "by_category": by_category},
            "raw": {"total": raw_total, "by_role": raw_by_role},
        }
    finally:
        await db.close()


# ──────────────────────── 数据模型 ────────────────────────

class MemoryCreate(BaseModel):
    content: str
    category: str = "fact"


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None


def _mem_row(row) -> dict:
    return {
        "id": row[0],
        "category": row[1],
        "content": row[2],
        "source_chat_id": row[3],
        "source_message_id": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }


# ──────────────────────── 结构化长期记忆 ────────────────────────

@router.get("")
async def list_memories(request: Request, category: str = "", limit: int = 200):
    """列出当前用户的结构化长期记忆。可按 category 过滤。"""
    uid = _uid(request)
    limit = max(1, min(limit, 1000))
    db = await get_db()
    try:
        if category:
            cursor = await db.execute(
                "SELECT id, category, content, source_chat_id, source_message_id, created_at, updated_at "
                "FROM user_memories WHERE user_id = ? AND category = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (uid, category, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, category, content, source_chat_id, source_message_id, created_at, updated_at "
                "FROM user_memories WHERE user_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (uid, limit),
            )
        rows = await cursor.fetchall()
        return {
            "memories": [_mem_row(r) for r in rows],
            "categories": CATEGORY_LABELS,
        }
    finally:
        await db.close()


@router.post("")
async def create_memory(body: MemoryCreate, request: Request):
    """手动新增一条结构化记忆。"""
    uid = _uid(request)
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "记忆内容不能为空。")
    category = body.category.strip().lower()
    if category not in CATEGORY_LABELS:
        category = "other"

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO user_memories (user_id, category, content, source_chat_id, source_message_id) "
            "VALUES (?, ?, ?, '', NULL)",
            (uid, category, content),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "category": category, "content": content}
    finally:
        await db.close()


@router.put("/{mem_id}")
async def update_memory(mem_id: int, body: MemoryUpdate, request: Request):
    """编辑一条结构化记忆的内容或分类。"""
    uid = _uid(request)
    updates = []
    params = []
    if body.content is not None and body.content.strip():
        updates.append("content = ?")
        params.append(body.content.strip())
    if body.category is not None:
        category = body.category.strip().lower()
        if category not in CATEGORY_LABELS:
            category = "other"
        updates.append("category = ?")
        params.append(category)
    if not updates:
        raise HTTPException(400, "没有需要更新的字段。")

    params.append(uid)
    params.append(mem_id)
    updates.append("updated_at = CURRENT_TIMESTAMP")

    db = await get_db()
    try:
        cursor = await db.execute(
            f"UPDATE user_memories SET {', '.join(updates)} WHERE user_id = ? AND id = ?",
            params,
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "记忆不存在或无权操作。")
        return {"status": "ok"}
    finally:
        await db.close()


@router.delete("/{mem_id}")
async def delete_memory(mem_id: int, request: Request):
    """删除一条结构化记忆。"""
    uid = _uid(request)
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM user_memories WHERE user_id = ? AND id = ?", (uid, mem_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "记忆不存在或无权操作。")
        return {"status": "ok"}
    finally:
        await db.close()


# ──────────────────────── 原始会话记忆 ────────────────────────

@router.get("/raw")
async def list_raw_memories(request: Request, role: str = "", limit: int = 200):
    """列出当前用户的原始会话记忆（用户输入 + AI 回复）。可按 role 过滤。"""
    uid = _uid(request)
    limit = max(1, min(limit, 1000))
    db = await get_db()
    try:
        if role:
            cursor = await db.execute(
                "SELECT id, chat_id, message_id, content, role, created_at "
                "FROM session_memories WHERE user_id = ? AND role = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (uid, role, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, chat_id, message_id, content, role, created_at "
                "FROM session_memories WHERE user_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (uid, limit),
            )
        rows = await cursor.fetchall()
        return {
            "memories": [
                {
                    "id": r[0],
                    "chat_id": r[1],
                    "message_id": r[2],
                    "content": r[3],
                    "role": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@router.delete("/raw/{mem_id}")
async def delete_raw_memory(mem_id: int, request: Request):
    """删除一条原始会话记忆（同步清理向量行）。"""
    uid = _uid(request)
    db = await get_db()
    try:
        # 兼容迁移前的旧数据（user_id 为 NULL 时也允许管理员清理）
        cursor = await db.execute(
            "DELETE FROM session_memories WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
            (mem_id, uid),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "记忆不存在或无权操作。")
    finally:
        await db.close()

    # 清理向量表中的对应行（失败不致命，孤儿向量行会被检索 JOIN 自然过滤）
    try:
        await vec_store.delete_session_ids([mem_id])
    except Exception:
        pass
    return {"status": "ok"}
