# backend/routes/tool_calls.py
from fastapi import APIRouter, HTTPException
from backend.db.tool_calls import (
    get_tool_call_by_id,
    get_tool_calls_by_message,
    delete_tool_calls_by_message,
)
from pydantic import BaseModel
from backend.services.llm_service import set_approval_result

router = APIRouter(prefix="/api/tool-calls", tags=["tool-calls"])


@router.get("/{call_id}")
async def get_tool_call(call_id: str):
    """获取单个工具调用详情"""
    record = await get_tool_call_by_id(call_id)
    if not record:
        raise HTTPException(status_code=404, detail="Tool call not found")
    return record.to_dict()


@router.get("/message/{message_id}")
async def get_message_tool_calls(message_id: int):
    """获取消息关联的所有工具调用"""
    records = await get_tool_calls_by_message(message_id)
    return [r.to_dict() for r in records]

@router.delete("/message/{message_id}")
async def delete_message_tool_calls(message_id: int):
    """根据 message_id 删除关联的所有工具调用记录"""
    deleted_count = await delete_tool_calls_by_message(message_id)
    return {
        "message": "Tool calls deleted successfully",
        "deleted_count": deleted_count
    }


# ──────────── 工具审批 ────────────

class ToolApprovalRequest(BaseModel):
    call_id: str
    approved: bool
    answer: Optional[str] = None  # system_ask_user 的用户文本回复


@router.post("/approval")
async def approve_tool(body: ToolApprovalRequest):
    """前端发送工具审批结果，唤醒等待中的工具执行协程。"""
    set_approval_result(body.call_id, body.approved, body.answer)
    return {"status": "ok"}


# ──────────── 使用统计 ────────────

@router.get("/stats")
async def get_stats():
    """聚合使用统计：Token 消耗、工具调用、对话数据。"""
    db = await get_db()
    try:
        # 对话总数
        cur = await db.execute("SELECT COUNT(*) FROM chats")
        chat_count = (await cur.fetchone())[0]

        # 消息总数
        cur = await db.execute("SELECT COUNT(*) FROM messages")
        msg_count = (await cur.fetchone())[0]

        # 工具调用总数 + 成功/失败
        cur = await db.execute("SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM tool_calls")
        row = await cur.fetchone()
        tool_total = row[0] or 0
        tool_success = row[1] or 0
        tool_error = row[2] or 0

        # TOP 10 工具
        cur = await db.execute("SELECT tool_name, COUNT(*) c FROM tool_calls GROUP BY tool_name ORDER BY c DESC LIMIT 10")
        tool_top = [{"name": r[0], "count": r[1]} for r in await cur.fetchall()]

        # 最近 30 天每日工具调用趋势
        cur = await db.execute("""
            SELECT DATE(created_at) as d, COUNT(*) as c
            FROM tool_calls WHERE created_at >= DATE('now', '-30 days')
            GROUP BY d ORDER BY d
        """)
        daily_trend = [{"date": r[0], "count": r[1]} for r in await cur.fetchall()]

        return {
            "chats": chat_count,
            "messages": msg_count,
            "tool_calls": {"total": tool_total, "success": tool_success, "error": tool_error},
            "tool_top": tool_top,
            "daily_trend": daily_trend,
        }
    finally:
        await db.close()