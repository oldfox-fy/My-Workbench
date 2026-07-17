# backend/routes/tool_calls.py
from fastapi import APIRouter, HTTPException, Request
from backend.database import get_db
from backend.db.tool_calls import (
    get_tool_call_by_id,
    get_tool_calls_by_message,
    delete_tool_calls_by_message,
)
from backend.db.token_usage import get_user_token_stats, get_token_breakdown_by_model
from typing import Optional
from pydantic import BaseModel
from backend.services.llm_service import set_approval_result

router = APIRouter(prefix="/api/tool-calls", tags=["tool-calls"])


# ──────────── 使用统计 ────────────
# 注意：/stats 必须在 /{call_id} 之前注册，否则 "stats" 会被当作 call_id 参数匹配

@router.get("/stats")
async def get_stats(request: Request):
    """聚合使用统计：Token 消耗、工具调用、对话数据（按用户隔离）。"""
    user_id = request.state.user["id"]
    db = await get_db()
    try:
        # 对话总数
        cur = await db.execute("SELECT COUNT(*) FROM chats WHERE user_id = ?", (user_id,))
        chat_count = (await cur.fetchone())[0]

        # 消息总数
        cur = await db.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,))
        msg_count = (await cur.fetchone())[0]

        # 工具调用总数 + 成功/失败
        cur = await db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM tool_calls WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        tool_total = row[0] or 0
        tool_success = row[1] or 0
        tool_error = row[2] or 0

        # TOP 10 工具
        cur = await db.execute(
            "SELECT tool_name, COUNT(*) c FROM tool_calls WHERE user_id = ? GROUP BY tool_name ORDER BY c DESC LIMIT 10",
            (user_id,),
        )
        tool_top = [{"name": r[0], "count": r[1]} for r in await cur.fetchall()]

        # Token 消耗统计
        token_stats = await get_user_token_stats(user_id)
        token_by_model = await get_token_breakdown_by_model(user_id)

        return {
            "chats": chat_count,
            "messages": msg_count,
            "tool_calls": {"total": tool_total, "success": tool_success, "error": tool_error},
            "tool_top": tool_top,
            "token_usage": {
                "request_count": token_stats["request_count"],
                "prompt_tokens": token_stats["prompt_tokens"],
                "completion_tokens": token_stats["completion_tokens"],
                "total_tokens": token_stats["total_tokens"],
                "by_model": token_by_model,
            },
        }
    finally:
        await db.close()


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


# ──────────── 参数化路由（必须放在最后，避免拦截 /stats 等具体路径） ────────────

@router.get("/{call_id}")
async def get_tool_call(call_id: str):
    """获取单个工具调用详情"""
    record = await get_tool_call_by_id(call_id)
    if not record:
        raise HTTPException(status_code=404, detail="Tool call not found")
    return record.to_dict()


# ──────────── Agent 追踪 ────────────

@router.get("/message/{message_id}/trace")
async def get_message_trace(message_id: int):
    """获取消息的 Agent 执行追踪数据（Trace + Spans）。"""
    from backend.services.tracer import get_trace_by_message
    trace = await get_trace_by_message(message_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace
