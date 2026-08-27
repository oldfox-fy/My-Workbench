# backend/routes/scheduled_tasks.py
# AI 定时自动化任务系统：REST API。
#
# 全部按当前登录用户隔离（request.state.user["id"]）。任务定义存
# scheduled_tasks，执行日志存 scheduled_task_runs；调度与执行逻辑在
# backend/services/scheduled_tasks.py。
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.database import get_db
from backend.services.scheduled_tasks import (
    compute_next_run,
    human_readable,
    parse_schedule_text,
    execute_task,
    _iso,
)

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled_tasks"])


# ── 请求模型 ──

class ScheduleParseIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    text: str
    llm_config: Optional[Dict[str, Any]] = Field(default=None, alias="model_config")


class ScheduledTaskIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    prompt: str
    schedule_type: str = "once"
    time_of_day: str = ""
    weekday: Optional[int] = None
    monthday: Optional[int] = None
    run_at: Optional[str] = None
    title: str = ""
    llm_config: Optional[Dict[str, Any]] = Field(default=None, alias="model_config")
    enable_tools: bool = True
    profile_id: Optional[int] = None
    auto_approve: bool = True


class ScheduledTaskPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    prompt: Optional[str] = None
    schedule_type: Optional[str] = None
    time_of_day: Optional[str] = None
    weekday: Optional[int] = None
    monthday: Optional[int] = None
    run_at: Optional[str] = None
    title: Optional[str] = None
    llm_config: Optional[Dict[str, Any]] = Field(default=None, alias="model_config")
    enable_tools: Optional[bool] = None
    profile_id: Optional[int] = None
    auto_approve: Optional[bool] = None


# ── 工具函数 ──

def _user_id(request: Request) -> int:
    return request.state.user["id"]


def _normalize_task_data(data: dict) -> dict:
    """把入参规整成 DB 字段，并校验调度类型。"""
    st = data.get("schedule_type") or "once"
    if st not in ("once", "daily", "workdays", "weekly", "monthly"):
        st = "once"
    return {
        "title": (data.get("title") or "").strip(),
        "prompt": (data.get("prompt") or "").strip(),
        "schedule_type": st,
        "time_of_day": data.get("time_of_day") or "",
        "weekday": data.get("weekday"),
        "monthday": data.get("monthday"),
        "run_at": data.get("run_at") or None,
    }


def _compute_next(data: dict, after: Optional[datetime] = None) -> Optional[str]:
    nxt = compute_next_run(
        data["schedule_type"], data["time_of_day"],
        data["weekday"], data["monthday"], data["run_at"],
        after=after,
    )
    return _iso(nxt) if nxt else None


async def _serialize_task(row: dict) -> dict:
    task = dict(row)
    task["human_readable"] = human_readable(task)
    # 最近一次执行状态
    db = await get_db()
    cur = await db.execute(
        "SELECT status, finished_at FROM scheduled_task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    )
    last = await cur.fetchone()
    await db.close()
    task["last_run_status"] = last["status"] if last else None
    task["last_run_at"] = last["finished_at"] if last else None
    return task


async def _get_owned_task(task_id: int, uid: int) -> dict:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?",
        (task_id, uid),
    )
    row = await cur.fetchone()
    await db.close()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return dict(row)


# ── 解析 ──

async def _build_parse_service(cfg: Optional[Dict[str, Any]]):
    """为自然语言解析构建一个轻量 LLMService（规则解析失败时的慢通道）。"""
    from backend.services.llm_service import LLMService
    from backend.services.model_router import get_default_model

    if not cfg:
        m = await get_default_model()
        if not m:
            return None
        cfg = {
            "type": m.get("type", "online"),
            "model_name": m.get("modelName", ""),
            "base_url": m.get("baseUrl", ""),
            "api_key": m.get("apiKey", ""),
            "thinking": "enabled",
            "role": m.get("role", "default"),
        }
    if not cfg.get("model_name"):
        return None
    return LLMService(
        model_type=cfg.get("type", "online"),
        model_name=cfg.get("model_name", ""),
        base_url=cfg.get("base_url") or None,
        api_key=cfg.get("api_key", ""),
        thinking=cfg.get("thinking", "enabled"),
        role=cfg.get("role", "default"),
    )


@router.post("/parse")
async def parse_schedule(body: ScheduleParseIn, request: Request):
    """自然语言解析调度 → 预览（不落库）。"""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请输入任务描述")
    service = await _build_parse_service(body.llm_config)
    result = await parse_schedule_text(text, llm_service=service)
    if result is None:
        raise HTTPException(status_code=422, detail="无法解析任务调度，请补充时间信息")
    return result


# ── 增删改查 ──

@router.post("")
async def create_task(body: ScheduledTaskIn, request: Request):
    uid = _user_id(request)
    data = _normalize_task_data(body.model_dump())
    if not data["prompt"]:
        raise HTTPException(status_code=400, detail="任务指令不能为空")
    next_run_at = _compute_next(data)
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO scheduled_tasks
           (user_id, title, prompt, schedule_type, time_of_day, weekday, monthday,
            run_at, next_run_at, status, model_config, enable_tools, profile_id, auto_approve)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
        (
            uid, data["title"] or data["prompt"][:30], data["prompt"],
            data["schedule_type"], data["time_of_day"], data["weekday"], data["monthday"],
            data["run_at"], next_run_at,
            json.dumps(body.llm_config or {}, ensure_ascii=False),
            1 if body.enable_tools else 0,
            body.profile_id,
            1 if body.auto_approve else 0,
        ),
    )
    task_id = cur.lastrowid
    await db.commit()
    await db.close()
    return await _serialize_task(await _get_owned_task(task_id, uid))


@router.get("")
async def list_tasks(request: Request):
    uid = _user_id(request)
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE user_id = ? ORDER BY "
        "CASE WHEN next_run_at IS NULL THEN 1 ELSE 0 END, next_run_at ASC",
        (uid,),
    )
    rows = await cur.fetchall()
    await db.close()
    return {"tasks": [await _serialize_task(dict(r)) for r in rows]}


@router.get("/{task_id}")
async def get_task(task_id: int, request: Request):
    uid = _user_id(request)
    task = await _get_owned_task(task_id, uid)
    return await _serialize_task(task)


@router.patch("/{task_id}")
async def update_task(task_id: int, body: ScheduledTaskPatch, request: Request):
    uid = _user_id(request)
    task = await _get_owned_task(task_id, uid)
    patch = body.model_dump(exclude_unset=True)

    merged = dict(task)
    for k, v in patch.items():
        if k in ("prompt", "title", "time_of_day", "run_at", "schedule_type"):
            merged[k] = v
        elif k in ("weekday", "monthday", "profile_id"):
            merged[k] = v
        elif k == "llm_config":
            merged[k] = json.dumps(v or {}, ensure_ascii=False)
        elif k == "enable_tools":
            merged[k] = 1 if v else 0
        elif k == "auto_approve":
            merged[k] = 1 if v else 0

    data = _normalize_task_data({
        "title": merged.get("title"),
        "prompt": merged.get("prompt"),
        "schedule_type": merged.get("schedule_type"),
        "time_of_day": merged.get("time_of_day"),
        "weekday": merged.get("weekday"),
        "monthday": merged.get("monthday"),
        "run_at": merged.get("run_at"),
    })
    next_run_at = _compute_next(data, after=datetime.now()) if task["status"] == "active" else merged.get("next_run_at")

    sets = []
    vals: List[Any] = []
    for k in ("title", "prompt", "schedule_type", "time_of_day", "weekday", "monthday", "run_at"):
        sets.append(f"{k}=?")
        vals.append(data[k] if k in data else merged.get(k))
    sets.append("next_run_at=?")
    vals.append(next_run_at)
    if "llm_config" in patch:
        sets.append("model_config=?")
        vals.append(merged["llm_config"])
    if "enable_tools" in patch:
        sets.append("enable_tools=?")
        vals.append(merged["enable_tools"])
    if "profile_id" in patch:
        sets.append("profile_id=?")
        vals.append(merged["profile_id"])
    if "auto_approve" in patch:
        sets.append("auto_approve=?")
        vals.append(merged["auto_approve"])
    sets.append("updated_at=?")
    vals.append(_iso(datetime.now()))

    db = await get_db()
    await db.execute(
        f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id=?",
        (*vals, task_id),
    )
    await db.commit()
    await db.close()
    return await _serialize_task(await _get_owned_task(task_id, uid))


@router.post("/{task_id}/toggle")
async def toggle_task(task_id: int, request: Request):
    uid = _user_id(request)
    task = await _get_owned_task(task_id, uid)
    if task["status"] == "active":
        new_status = "paused"
        next_run_at = task["next_run_at"]
    else:
        new_status = "active"
        next_run_at = _compute_next({
            "schedule_type": task["schedule_type"], "time_of_day": task["time_of_day"],
            "weekday": task["weekday"], "monthday": task["monthday"], "run_at": task["run_at"],
        }, after=datetime.now())
    db = await get_db()
    await db.execute(
        "UPDATE scheduled_tasks SET status=?, next_run_at=?, updated_at=? WHERE id=?",
        (new_status, next_run_at, _iso(datetime.now()), task_id),
    )
    await db.commit()
    await db.close()
    return await _serialize_task(await _get_owned_task(task_id, uid))


@router.delete("/{task_id}")
async def delete_task(task_id: int, request: Request):
    uid = _user_id(request)
    await _get_owned_task(task_id, uid)
    db = await get_db()
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, uid))
    await db.commit()
    await db.close()
    return {"status": "ok"}


# ── 执行日志 ──

@router.get("/{task_id}/runs")
async def list_runs(task_id: int, request: Request):
    uid = _user_id(request)
    await _get_owned_task(task_id, uid)
    db = await get_db()
    cur = await db.execute(
        "SELECT id, task_id, status, scheduled_for, started_at, finished_at, output, error "
        "FROM scheduled_task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 50",
        (task_id,),
    )
    rows = await cur.fetchall()
    await db.close()
    return {"runs": [dict(r) for r in rows]}


@router.post("/{task_id}/run-now")
async def run_now(task_id: int, request: Request):
    """手动立即执行一次（后台运行，结果经 WebSocket 广播 + 日志查看）。"""
    uid = _user_id(request)
    task = await _get_owned_task(task_id, uid)
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    skill_registry = getattr(request.app.state, "skill_registry", None)
    asyncio.create_task(execute_task(task_id, mcp_manager, skill_registry))
    return {"status": "started", "task_id": task_id}
