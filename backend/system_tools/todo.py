# backend/system_tools/todo.py
"""
任务规划工具：LLM 用此工具创建和跟踪多步骤任务计划。

支持 Plan → Execute → Verify 工作流：
  1. LLM 收到复杂任务 → 调用 system_todo(action='create') 创建计划
  2. 逐步执行，每次完成一步后调用 system_todo(action='update') 更新状态
  3. 全部完成后做最终总结

任务状态在对话上下文中维护（通过 messages 中的 tool 回复），
不需要持久化到数据库，保持轻量。
"""
import json
from typing import Any, Dict, List


# ── 会话级任务存储（内存中，重启丢失） ──
_sessions: Dict[str, List[Dict]] = {}


async def todo(
    action: str = "list",
    tasks: str = "[]",
    task_id: str = "",
    status: str = "",
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

    Returns:
        {success, tasks: [{id, title, status}], summary: "进度: 2/5"}
    """
    # 用固定 session id（单用户桌面应用场景）
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
            return _format_result(current)

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
            return _format_result(current)

        elif action == "list":
            return _format_result(current)

        else:
            return {"success": False, "error": f"未知操作: {action}"}

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"tasks JSON 解析失败: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
