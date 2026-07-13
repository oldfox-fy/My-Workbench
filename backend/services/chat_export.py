# backend/services/chat_export.py
"""
会话导出/导入服务：支持 Markdown、JSON、ZIP 三种格式。

Markdown：人类可读，适合分享/存档
JSON：结构完整，可重新导入恢复对话
ZIP：JSON + 附件打包
"""
import json
import zipfile
import io
from datetime import datetime
from typing import Dict, List, Any, Optional
from backend.database import get_db


async def get_chat_data(chat_id: str) -> Optional[Dict[str, Any]]:
    """获取完整对话数据。返回 {chat: {...}, messages: [...]} 或 None。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, title, created_at FROM chats WHERE id = ?", (chat_id,))
        chat_row = await cursor.fetchone()
        if not chat_row:
            return None

        cursor = await db.execute(
            "SELECT id, role, content, file_ref, created_at FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        )
        msgs = await cursor.fetchall()
        messages = []
        for row in msgs:
            content = row[2]
            # 清理 SSE 标记
            if isinstance(content, str):
                import re
                content = re.sub(r'<!--[\s\S]*?-->', '', content).strip()
            messages.append({
                "id": row[0],
                "role": row[1],
                "content": content,
                "file_ref": json.loads(row[3]) if row[3] else None,
                "created_at": row[4],
            })

        return {
            "chat": {"id": chat_row[0], "title": chat_row[1], "created_at": chat_row[2]},
            "messages": messages,
        }
    finally:
        await db.close()


def export_as_markdown(data: Dict[str, Any]) -> str:
    """导出为 Markdown 格式。"""
    chat = data["chat"]
    lines = [
        f"# {chat['title']}",
        f"> 日期: {chat.get('created_at', '')}",
        f"> 消息数: {len(data['messages'])}",
        "",
        "---",
        "",
    ]
    for msg in data["messages"]:
        role_label = {"user": "## 用户", "assistant": "## AI", "system": "## 系统"}.get(msg["role"], f"## {msg['role']}")
        lines.append(role_label)
        lines.append("")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(content)
        else:
            lines.append(str(content))
        lines.append("")
        if msg.get("file_ref"):
            lines.append(f"> 附件: {json.dumps(msg['file_ref'], ensure_ascii=False)}")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def export_as_json(data: Dict[str, Any]) -> str:
    """导出为 JSON 格式（保留完整结构，可重新导入）。"""
    export = {
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "chat": data["chat"],
        "messages": [
            {k: v for k, v in msg.items() if k != "id"}
            for msg in data["messages"]
        ],
    }
    return json.dumps(export, ensure_ascii=False, indent=2)


def export_as_zip(data: Dict[str, Any]) -> bytes:
    """导出为 ZIP 格式（JSON + 附件占位）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("chat.json", export_as_json(data))
        # 后续可在此处打包 file_ref 对应的实际文件
    buf.seek(0)
    return buf.read()


async def import_from_json(payload: Dict[str, Any]) -> str:
    """
    从 JSON 导入对话，返回新对话的 chat_id。
    要求 payload 包含 chat 和 messages。
    """
    import uuid
    chat_title = payload.get("chat", {}).get("title", "导入的对话")
    messages = payload.get("messages", [])
    if not messages:
        raise ValueError("JSON 中无消息数据")

    chat_id = str(uuid.uuid4())
    now = datetime.now().isoformat()

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO chats (id, title, created_at) VALUES (?, ?, ?)",
            (chat_id, chat_title, now),
        )
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "user")
            file_ref = msg.get("file_ref")
            file_ref_json = json.dumps(file_ref, ensure_ascii=False) if file_ref else None
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, file_ref) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, file_ref_json),
            )
        await db.commit()
        return chat_id
    finally:
        await db.close()
