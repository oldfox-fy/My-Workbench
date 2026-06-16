# backend/routes/tool_calls.py
from fastapi import APIRouter, HTTPException
from backend.db.tool_calls import (
    get_tool_call_by_id,
    get_tool_calls_by_message,
)

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