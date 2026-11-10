# backend/routes/tool_calls.py
from datetime import datetime, timedelta, timezone
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

def _period_utc_range(period: str, date_str: str) -> tuple:
    """
    按「本地日历」的 日/月/年 周期，换算成 UTC 时间窗 [start, end)。
    数据库 created_at 由 SQLite CURRENT_TIMESTAMP 生成（UTC），故比较需用 UTC。
    """
    local_tz = datetime.now().astimezone().tzinfo
    now_local = datetime.now(local_tz)

    if period == "year":
        y = int(date_str) if date_str else now_local.year
        start = datetime(y, 1, 1, tzinfo=local_tz)
        end = datetime(y + 1, 1, 1, tzinfo=local_tz)
    elif period == "month":
        if date_str:
            y, m = map(int, date_str.split("-"))
        else:
            y, m = now_local.year, now_local.month
        start = datetime(y, m, 1, tzinfo=local_tz)
        end = datetime(y + 1, 1, 1, tzinfo=local_tz) if m == 12 else datetime(y, m + 1, 1, tzinfo=local_tz)
    else:  # day
        if date_str:
            y, m, d = map(int, date_str.split("-"))
        else:
            y, m, d = now_local.year, now_local.month, now_local.day
        start = datetime(y, m, d, tzinfo=local_tz)
        end = start + timedelta(days=1)

    return (
        start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


@router.get("/stats")
async def get_stats(request: Request, period: str = "day", date: str = ""):
    """
    聚合使用统计：Token 消耗、工具调用、对话数据（按用户隔离）。
    period: all | day | month | year（默认 day=当日）。
    date:   对应周期字符串，如 day="2026-11-10"、month="2026-11"、year="2026"；缺省取当日。
    """
    user_id = request.state.user["id"]
    period = (period or "day").lower()

    range_clause = ""
    range_params: tuple = ()
    start_ts = end_ts = None
    if period != "all":
        if period not in ("day", "month", "year"):
            period = "day"
        start_ts, end_ts = _period_utc_range(period, date)
        range_clause = " AND created_at >= ? AND created_at < ?"
        range_params = (start_ts, end_ts)

    db = await get_db()
    try:
        # 对话总数
        cur = await db.execute(
            f"SELECT COUNT(*) FROM chats WHERE user_id = ?{range_clause}",
            (user_id, *range_params),
        )
        chat_count = (await cur.fetchone())[0]

        # 消息总数
        cur = await db.execute(
            f"SELECT COUNT(*) FROM messages WHERE user_id = ?{range_clause}",
            (user_id, *range_params),
        )
        msg_count = (await cur.fetchone())[0]

        # 工具调用总数 + 成功/失败
        cur = await db.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM tool_calls WHERE user_id = ?{range_clause}",
            (user_id, *range_params),
        )
        row = await cur.fetchone()
        tool_total = row[0] or 0
        tool_success = row[1] or 0
        tool_error = row[2] or 0

        # TOP 10 工具
        cur = await db.execute(
            f"SELECT tool_name, COUNT(*) c FROM tool_calls WHERE user_id = ?{range_clause} "
            f"GROUP BY tool_name ORDER BY c DESC LIMIT 10",
            (user_id, *range_params),
        )
        tool_top = [{"name": r[0], "count": r[1]} for r in await cur.fetchall()]

        # Token 消耗统计
        token_stats = await get_user_token_stats(user_id, start_ts, end_ts)
        token_by_model = await get_token_breakdown_by_model(user_id, start_ts, end_ts)

        return {
            "period": period,
            "date": date,
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
