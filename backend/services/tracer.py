# backend/services/tracer.py
"""
Agent 执行追踪器：Trace/Span 生命周期管理。

每个用户消息触发一个 Trace，包含多个 Span：
  - step：每轮 Agent LLM 调用
  - tool_call：每个工具调用
  - sub_agent：子 Agent 委派
  - approval：工具审批等待

Span 数据通过 SSE 标记实时推送到前端，同时持久化到 agent_traces/agent_spans 表。
"""
import uuid
import time
import json
import asyncio
from typing import Any, Dict, List, Optional
from backend.bootstrap import logger


class Span:
    """追踪 Span：单次操作"""
    def __init__(self, span_id: str, trace_id: str, parent_span_id: Optional[str],
                 span_type: str, name: str):
        self.id = span_id
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.span_type = span_type
        self.name = name
        self.status = "running"
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.duration_ms: Optional[int] = None
        self.input_preview = ""
        self.output_preview = ""
        self.error_message: Optional[str] = None

    def end(self, status: str = "success", output: str = "", error: str = None):
        self.end_time = time.time()
        self.duration_ms = int((self.end_time - self.start_time) * 1000)
        self.status = status
        self.output_preview = output[:300] if output else ""
        self.error_message = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "span_type": self.span_type,
            "name": self.name,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "error_message": self.error_message,
        }

    def to_sse_start(self) -> str:
        return f"<!--span:start:{self.id}:{self.span_type}:{_escape(self.name)}-->"

    def to_sse_end(self) -> str:
        return f"<!--span:end:{self.id}:{self.status}:{self.duration_ms or 0}-->"


class TraceManager:
    """管理单次 Agent 执行的 Trace 和所有 Span。"""

    def __init__(self, chat_id: str = "", message_id: int = 0):
        self.trace_id = str(uuid.uuid4())
        self.chat_id = chat_id
        self.message_id = message_id
        self.root_span: Optional[Span] = None
        self._spans: Dict[str, Span] = {}
        self._current_step: Optional[Span] = None
        self._total_steps = 0
        self._total_tool_calls = 0
        self._started = time.time()

    # ── Span 创建 ──

    def start_step(self, step_num: int) -> Span:
        span = Span(str(uuid.uuid4()), self.trace_id,
                    self.root_span.id if self.root_span else None,
                    "step", f"Step {step_num}")
        self._spans[span.id] = span
        self._current_step = span
        self._total_steps += 1
        return span

    def start_tool_call(self, tool_name: str, args_preview: str = "") -> Span:
        span = Span(str(uuid.uuid4()), self.trace_id,
                    self._current_step.id if self._current_step else None,
                    "tool_call", tool_name)
        span.input_preview = args_preview[:300]
        self._spans[span.id] = span
        self._total_tool_calls += 1
        return span

    def start_sub_agent(self, agent_name: str) -> Span:
        span = Span(str(uuid.uuid4()), self.trace_id,
                    self._current_step.id if self._current_step else None,
                    "sub_agent", agent_name)
        self._spans[span.id] = span
        return span

    def start_approval(self, tool_name: str) -> Span:
        span = Span(str(uuid.uuid4()), self.trace_id,
                    self._current_step.id if self._current_step else None,
                    "approval", f"审批: {tool_name}")
        self._spans[span.id] = span
        return span

    # ── Span 结束 ──

    def end_span(self, span_id: str, status: str = "success",
                 output: str = "", error: str = None):
        span = self._spans.get(span_id)
        if span:
            span.end(status, output, error)

    # ── 汇总 ──

    def finalize(self) -> Dict[str, Any]:
        total_ms = int((time.time() - self._started) * 1000)
        return {
            "trace_id": self.trace_id,
            "status": "completed",
            "total_steps": self._total_steps,
            "total_tool_calls": self._total_tool_calls,
            "total_time_ms": total_ms,
            "spans": [s.to_dict() for s in self._spans.values()],
        }

    # ── SSE 标记 ──

    def emit_root_start(self) -> str:
        self.root_span = Span(self.trace_id + "_root", self.trace_id, None,
                              "trace", "Agent 执行")
        self.root_span.start_time = self._started
        self._spans[self.root_span.id] = self.root_span
        return self.root_span.to_sse_start()

    def emit_root_end(self, status: str = "completed") -> str:
        if self.root_span:
            self.root_span.end(status)
            return self.root_span.to_sse_end()
        return ""


# ── 持久化 ──

async def persist_trace(trace: TraceManager) -> None:
    """将 Trace 和所有 Span 写入数据库。"""
    try:
        from backend.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO agent_traces (id, message_id, chat_id, status, total_steps, total_tool_calls, total_time_ms, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (trace.trace_id, trace.message_id, trace.chat_id,
                 "completed", trace._total_steps, trace._total_tool_calls,
                 int((time.time() - trace._started) * 1000)),
            )
            for span in trace._spans.values():
                await db.execute(
                    """INSERT OR REPLACE INTO agent_spans (id, trace_id, parent_span_id, span_type, name, status, start_time, end_time, duration_ms, input_preview, output_preview, error_message)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (span.id, span.trace_id, span.parent_span_id, span.span_type,
                     span.name, span.status, span.start_time, span.end_time,
                     span.duration_ms, span.input_preview[:300],
                     span.output_preview[:300], span.error_message),
                )
            await db.commit()
        finally:
            await db.close()
    except Exception as e:
        logger.warning(f"[tracer] 持久化追踪数据失败: {e}")


async def get_trace_by_message(message_id: int) -> Optional[Dict[str, Any]]:
    """获取某条消息的 Trace 数据。"""
    from backend.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status, total_steps, total_tool_calls, total_time_ms FROM agent_traces WHERE message_id = ? ORDER BY started_at DESC LIMIT 1",
            (message_id,),
        )
        trace_row = await cursor.fetchone()
        if not trace_row:
            return None
        cursor = await db.execute(
            "SELECT id, parent_span_id, span_type, name, status, duration_ms, input_preview, output_preview, error_message FROM agent_spans WHERE trace_id = ? ORDER BY start_time ASC",
            (trace_row[0],),
        )
        spans = []
        async for row in cursor:
            spans.append({
                "id": row[0], "parent_span_id": row[1], "span_type": row[2],
                "name": row[3], "status": row[4], "duration_ms": row[5],
                "input_preview": row[6], "output_preview": row[7], "error_message": row[8],
            })
        return {
            "trace_id": trace_row[0], "status": trace_row[1],
            "total_steps": trace_row[2], "total_tool_calls": trace_row[3],
            "total_time_ms": trace_row[4], "spans": spans,
        }
    finally:
        await db.close()


def _escape(s: str) -> str:
    """转义 SSE 标记中不允许的字符。"""
    return s.replace(":", "：").replace("-->", "-->")
