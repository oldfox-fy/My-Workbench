# backend/system_tools/todo.py
"""
任务规划工具：LLM 用此工具创建和跟踪多步骤任务计划。

支持 Plan → Execute → Verify 工作流：
  1. LLM 收到复杂任务 → 调用 system_todo(action='create') 创建计划
  2. 逐步执行，每次完成一步后调用 system_todo(action='update') 更新状态
  3. 全部完成后做最终总结

计划持久化到 agent_plans / agent_plan_steps 表，并通过 SSE/WS 标记
实时推送到前端展示。
"""
import json
import uuid
from typing import Any, Dict, List, Optional

# ── 会话级任务存储（内存中，重启丢失） ──
_sessions: Dict[str, List[Dict]] = {}

# ── 计划事件队列：工具执行时累积 SSE 标记，执行完毕后由 llm_service 取出并 yield ──
_plan_events: List[str] = []


def push_plan_event(marker: str):
    """向计划事件队列追加一条 SSE 标记。"""
    _plan_events.append(marker)


def pop_plan_events() -> List[str]:
    """取出并清空当前累积的所有计划事件标记。"""
    global _plan_events
    events = _plan_events
    _plan_events = []
    return events


async def todo(
    action: str = "list",
    tasks: str = "[]",
    task_id: str = "",
    status: str = "",
    chat_id: str = "",
    message_id: Optional[int] = None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    管理任务计划。

    Args:
        action: 操作类型
            - "create": 创建新计划。tasks 为 JSON 数组 [{title, status}]
            - "update": 更新某任务状态。task_id 为任务序号(从1开始)，status 为 pending/in_progress/completed/cancelled
            - "list": 列出当前所有任务
        tasks: 任务列表 JSON 字符串，仅 action='create' 时需要
        task_id: 任务序号（从 1 开始），仅 action='update' 时需要
        status: 新状态，仅 action='update' 时需要
        chat_id: 当前对话 ID（由 execute_tool 注入）
        message_id: 当前消息 ID（由 execute_tool 注入）

    Returns:
        {success, tasks: [{id, title, status}], summary: "进度: 2/5"}
    """
    sid = "_default"
    if sid not in _sessions:
        _sessions[sid] = []

    current = _sessions[sid]

    try:
        if action == "create":
            task_list = json.loads(tasks) if isinstance(tasks, str) else tasks
            if not isinstance(task_list, list) or not task_list:
                return {"success": False, "error": "tasks 必须是非空数组"}
            current.clear()
            for i, t in enumerate(task_list, 1):
                current.append({
                    "id": str(i),
                    "title": t.get("title", f"步骤 {i}"),
                    "status": t.get("status", "pending"),
                })
            result = _format_result(current)

            # 持久化到 DB
            if chat_id:
                await _persist_plan(sid, message_id, chat_id, current)

            # 发送 plan:create SSE 标记
            plan_data = json.dumps({"tasks": current, "summary": result["summary"]}, ensure_ascii=False)
            push_plan_event(f"<!--plan:create:{plan_data}-->")

            return result

        elif action == "update":
            if not task_id:
                return {"success": False, "error": "update 需要 task_id"}
            if not status:
                return {"success": False, "error": "update 需要 status"}
            tid = str(task_id)
            for t in current:
                if t["id"] == tid:
                    t["status"] = status
                    break
            else:
                return {"success": False, "error": f"任务 {task_id} 不存在"}
            result = _format_result(current)

            # 持久化
            if chat_id:
                await _persist_plan(sid, message_id, chat_id, current)

            # 发送 plan:update SSE 标记
            update_data = json.dumps({
                "task_id": tid, "status": status,
                "summary": result["summary"]
            }, ensure_ascii=False)
            push_plan_event(f"<!--plan:update:{update_data}-->")

            return result

        elif action == "list":
            return _format_result(current)

        else:
            return {"success": False, "error": f"未知操作: {action}"}

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"tasks JSON 解析失败: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _persist_plan(sid: str, message_id: Optional[int], chat_id: str, tasks: List[Dict]):
    """将当前计划持久化到 SQLite。"""
    try:
        from backend.database import get_db
        db = await get_db()
        try:
            # 查找或创建 plan
            cursor = await db.execute(
                "SELECT id FROM agent_plans WHERE chat_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row:
                plan_id = row[0]
            else:
                plan_id = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO agent_plans (id, message_id, chat_id) VALUES (?, ?, ?)",
                    (plan_id, message_id or 0, chat_id),
                )

            # 删除旧步骤，重新插入
            await db.execute("DELETE FROM agent_plan_steps WHERE plan_id = ?", (plan_id,))
            for t in tasks:
                await db.execute(
                    "INSERT INTO agent_plan_steps (plan_id, step_index, title, status) VALUES (?, ?, ?, ?)",
                    (plan_id, int(t["id"]), t["title"], t["status"]),
                )

            # 检查是否全部完成
            all_done = all(t["status"] == "completed" for t in tasks)
            if all_done:
                await db.execute(
                    "UPDATE agent_plans SET status = 'completed' WHERE id = ?",
                    (plan_id,),
                )

            await db.commit()
        finally:
            await db.close()
    except Exception:
        pass  # 持久化失败不阻塞主流程


def _format_result(tasks: List[Dict]) -> Dict[str, Any]:
    completed = sum(1 for t in tasks if t["status"] == "completed")
    total = len(tasks)
    status_icons = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}

    lines = []
    for t in tasks:
        icon = status_icons.get(t["status"], "⬜")
        lines.append(f"{icon} {t['id']}. {t['title']} [{t['status']}]")

    return {
        "success": True,
        "tasks": tasks,
        "summary": f"进度: {completed}/{total}",
        "display": "\n".join(lines),
    }
