# backend/routes/chats.py
import json
from http.client import HTTPException
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional, Any, Union
from backend.database import get_db


router = APIRouter(prefix="/api/chats", tags=["chats"])

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
async def create_chat():
    import uuid
    from datetime import datetime
    chat_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    db = await get_db()
    await db.execute("INSERT INTO chats (id, title, created_at) VALUES (?, ?, ?)", (chat_id, "新对话", now))
    await db.commit()
    await db.close()
    return {"id": chat_id, "title": "新对话", "created_at": now}

@router.patch("/{chat_id}")
async def update_chat_title(chat_id: str, data: UpdateChatTitle):
    db = await get_db()
    await db.execute("UPDATE chats SET title = ? WHERE id = ?", (data.title, chat_id))
    await db.commit()
    await db.close()
    return {"status": "ok"}

# 获取所有对话列表
@router.get("/", response_model=List[ChatResponse])
async def list_chats():
    db = await get_db()
    cursor = await db.execute("SELECT id, title, created_at FROM chats ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    await db.close()
    return [{"id": row[0], "title": row[1], "created_at": row[2]} for row in rows]

# 删除对话
@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    db = await get_db()
    await db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    await db.commit()
    await db.close()
    return {"status": "ok"}

# 获取对话消息
@router.get("/{chat_id}/messages", response_model=List[MessageResponse])
async def get_messages(chat_id: str):
    db = await get_db()
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
async def add_message(chat_id: str, req: AddMessageRequest):
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO messages (chat_id, role, content, file_ref) VALUES (?, ?, ?, ?)",
        (chat_id, req.role, req.content, json.dumps(req.file_ref) if req.file_ref else None)
    )
    await db.commit()
    msg_id = cursor.lastrowid
    await db.close()
    return {"id": msg_id, "role": req.role, "content": req.content, "file_ref": req.file_ref}

@router.put("/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, req: AddMessageRequest):
    db = await get_db()
    await db.execute("UPDATE messages SET content = ? WHERE id = ? AND chat_id = ?", (req.content, message_id, chat_id))
    if db.total_changes == 0:
        await db.close()
        raise HTTPException(status_code=404, detail="Message not found")
    await db.commit()
    await db.close()
    return {"status": "ok"}

@router.delete("/{chat_id}/messages/{message_id}")
async def delete_message(chat_id: str, message_id: int, cascade: bool = False):
    db = await get_db()
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
async def branch_chat(chat_id: str, message_id: int):
    """从指定消息处创建对话分叉。复制该消息及之前的所有消息到新对话。"""
    db = await get_db()
    try:
        # 获取父对话信息
        cursor = await db.execute("SELECT title FROM chats WHERE id = ?", (chat_id,))
        parent = await cursor.fetchone()
        if not parent:
            raise HTTPException(status_code=404, detail="Chat not found")

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
            "INSERT INTO chats (id, title, created_at, parent_chat_id, branched_at_message_id) VALUES (?, ?, ?, ?, ?)",
            (new_id, new_title, now, chat_id, message_id),
        )

        # 复制消息
        for msg in msgs:
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, file_ref) VALUES (?, ?, ?, ?)",
                (new_id, msg[0], msg[1], msg[2]),
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