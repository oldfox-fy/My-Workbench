# backend/routes/chats.py
import json
from http.client import HTTPException
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import List, Optional, Any, Union
from backend.database import get_db
from backend.auth import get_current_user


router = APIRouter(prefix="/api/chats", tags=["chats"])


def _get_user_id(request: Request) -> int:
    """从 request.state 获取当前用户 ID"""
    return request.state.user["id"]

class ChatResponse(BaseModel):
    id: str
    title: str
    created_at: Optional[str] = None

class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    file_ref: Optional[Union[dict, list]] = None

class AddMessageRequest(BaseModel):
    role: str
    content: Any
    file_ref: Optional[Union[dict, list]] = None

class UpdateChatTitle(BaseModel):
    title: str

# 创建新对话
@router.post("/", response_model=ChatResponse)
async def create_chat(request: Request):
    import uuid
    from datetime import datetime
    chat_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    user_id = request.state.user["id"]
    db = await get_db()
    await db.execute("INSERT INTO chats (id, title, created_at, user_id) VALUES (?, ?, ?, ?)", (chat_id, "新对话", now, user_id))
    await db.commit()
    await db.close()
    return {"id": chat_id, "title": "新对话", "created_at": now}

@router.patch("/{chat_id}")
async def update_chat_title(chat_id: str, data: UpdateChatTitle, request: Request):
    db = await get_db()
    user_id = request.state.user["id"]
    cursor = await db.execute("UPDATE chats SET title = ? WHERE id = ? AND user_id = ?", (data.title, chat_id, user_id))
    if cursor.rowcount == 0:
        await db.close()
        raise HTTPException(status_code=403, detail="无权修改此对话")
    await db.commit()
    await db.close()
    return {"status": "ok"}

# 获取所有对话列表
@router.get("/", response_model=List[ChatResponse])
async def list_chats(request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, title, created_at FROM chats WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    await db.close()
    return [{"id": row[0], "title": row[1], "created_at": row[2]} for row in rows]

# 删除对话
@router.delete("/{chat_id}")
async def delete_chat(chat_id: str, request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    # 验证对话属于当前用户
    cursor = await db.execute("SELECT user_id FROM chats WHERE id = ?", (chat_id,))
    row = await cursor.fetchone()
    if not row:
        await db.close()
        raise HTTPException(status_code=404, detail="Chat not found")
    if row[0] is not None and row[0] != user_id:
        await db.close()
        raise HTTPException(status_code=403, detail="无权操作此对话")
    await db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    await db.commit()
    await db.close()
    return {"status": "ok"}

# 获取对话消息
@router.get("/{chat_id}/messages", response_model=List[MessageResponse])
async def get_messages(chat_id: str, request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    # 验证对话属于当前用户
    cursor = await db.execute("SELECT user_id FROM chats WHERE id = ?", (chat_id,))
    chat_row = await cursor.fetchone()
    if not chat_row:
        await db.close()
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat_row[0] is not None and chat_row[0] != user_id:
        await db.close()
        raise HTTPException(status_code=403, detail="无权访问此对话")

    cursor = await db.execute("SELECT id, role, content, file_ref FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
    rows = await cursor.fetchall()
    await db.close()
    return [
        {
            "id": row[0],
            "role": row[1],
            "content": json.loads(row[2]) if isinstance(row[2], str) and (row[2].startswith('[') or row[2].startswith('{')) else row[2],
            "file_ref": json.loads(row[3]) if row[3] else None
        }
        for row in rows
    ]

@router.post("/{chat_id}/messages", response_model=MessageResponse)
async def add_message(chat_id: str, req: AddMessageRequest, request: Request):
    user_id = request.state.user["id"]
    db = await get_db()
    # 验证对话属于当前用户
    cursor = await db.execute("SELECT user_id FROM chats WHERE id = ?", (chat_id,))
    chat_row = await cursor.fetchone()
    if not chat_row:
        await db.close()
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat_row[0] is not None and chat_row[0] != user_id:
        await db.close()
        raise HTTPException(status_code=403, detail="无权操作此对话")

    cursor = await db.execute(
        "INSERT INTO messages (chat_id, role, content, file_ref, user_id) VALUES (?, ?, ?, ?, ?)",
        (chat_id, req.role, req.content, json.dumps(req.file_ref) if req.file_ref else None, user_id)
    )
    await db.commit()
    msg_id = cursor.lastrowid
    await db.close()
    return {"id": msg_id, "role": req.role, "content": req.content, "file_ref": req.file_ref}

@router.put("/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, req: AddMessageRequest, request: Request):
    db = await get_db()
    user_id = request.state.user["id"]
    # 验证 chat 属于当前用户
    cur = await db.execute("SELECT id FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
    if not await cur.fetchone():
        await db.close()
        raise HTTPException(status_code=403, detail="无权修改此对话的消息")
    await db.execute("UPDATE messages SET content = ? WHERE id = ? AND chat_id = ?", (req.content, message_id, chat_id))
    if db.total_changes == 0:
        await db.close()
        raise HTTPException(status_code=404, detail="Message not found")
    await db.commit()
    await db.close()
    return {"status": "ok"}

@router.delete("/{chat_id}/messages/{message_id}")
async def delete_message(chat_id: str, message_id: int, request: Request, cascade: bool = False):
    db = await get_db()
    user_id = request.state.user["id"]
    # 验证 chat 属于当前用户
    cur = await db.execute("SELECT id FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
    if not await cur.fetchone():
        await db.close()
        raise HTTPException(status_code=403, detail="无权删除此对话的消息")
    if cascade:
        await db.execute("DELETE FROM messages WHERE chat_id = ? AND id >= ?", (chat_id, message_id))
    else:
        await db.execute("DELETE FROM messages WHERE id = ? AND chat_id = ?", (message_id, chat_id))
    await db.commit()
    await db.close()
    return {"status": "ok"}


# ──────────── 导入 / 导出 ────────────
from fastapi.responses import PlainTextResponse, Response
from backend.services.chat_export import get_chat_data, export_as_markdown, export_as_json, export_as_zip, import_from_json


@router.get("/{chat_id}/export")
async def export_chat(chat_id: str, format: str = "md"):
    data = await get_chat_data(chat_id)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")
    if format == "md":
        content = export_as_markdown(data)
        return PlainTextResponse(content, media_type="text/markdown",
                                 headers={"Content-Disposition": f"attachment; filename=chat_{chat_id}.md"})
    elif format == "json":
        content = export_as_json(data)
        return PlainTextResponse(content, media_type="application/json",
                                 headers={"Content-Disposition": f"attachment; filename=chat_{chat_id}.json"})
    elif format == "zip":
        content = export_as_zip(data)
        return Response(content, media_type="application/zip",
                       headers={"Content-Disposition": f"attachment; filename=chat_{chat_id}.zip"})
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}")


class ImportRequest(BaseModel):
    data: Optional[dict] = None


@router.post("/import")
async def import_chat(body: ImportRequest):
    if not body.data:
        raise HTTPException(status_code=400, detail="缺少导入数据")
    try:
        chat_id = await import_from_json(body.data)
        return {"status": "ok", "chat_id": chat_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ──────────── 对话分叉 ────────────
import uuid
from datetime import datetime


@router.post("/{chat_id}/branch")
async def branch_chat(chat_id: str, message_id: int, request: Request):
    """从指定消息处创建对话分叉。复制该消息及之前的所有消息到新对话。"""
    user_id = request.state.user["id"]
    db = await get_db()
    try:
        # 获取父对话信息并验证所有权
        cursor = await db.execute("SELECT title, user_id FROM chats WHERE id = ?", (chat_id,))
        parent = await cursor.fetchone()
        if not parent:
            raise HTTPException(status_code=404, detail="Chat not found")
        if parent[1] is not None and parent[1] != user_id:
            raise HTTPException(status_code=403, detail="无权操作此对话")

        # 复制消息（截至 message_id）
        cursor = await db.execute(
            "SELECT role, content, file_ref FROM messages WHERE chat_id = ? AND id <= ? ORDER BY id",
            (chat_id, message_id),
        )
        msgs = await cursor.fetchall()
        if not msgs:
            raise HTTPException(status_code=400, detail="No messages to branch from")

        # 创建新对话
        new_id = str(uuid.uuid4())
        new_title = f"{parent[0]} (分叉)"
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO chats (id, title, created_at, parent_chat_id, branched_at_message_id, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, new_title, now, chat_id, message_id, user_id),
        )

        # 复制消息
        for msg in msgs:
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, file_ref, user_id) VALUES (?, ?, ?, ?, ?)",
                (new_id, msg[0], msg[1], msg[2], user_id),
            )

        await db.commit()
        return {"status": "ok", "chat_id": new_id, "title": new_title}
    finally:
        await db.close()


@router.get("/{chat_id}/branches")
async def list_branches(chat_id: str):
    """获取某对话的所有分叉子对话。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, parent_chat_id, branched_at_message_id, created_at FROM chats WHERE parent_chat_id = ? ORDER BY created_at",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "title": r[1], "parent_chat_id": r[2], "branched_at_message_id": r[3], "created_at": r[4]}
            for r in rows
        ]
    finally:
        await db.close()