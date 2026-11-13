# backend/routes/sqlbot.py
"""SQLBot 智能问数：模型同步、状态查询、数据源管理路由（M3/M4）。"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.services.sqlbot_sync import sync_models_to_sqlbot, get_sqlbot_status
from backend.services import sqlbot_datasource

router = APIRouter(prefix="/api/sqlbot", tags=["sqlbot"])


def _require_admin(request: Request) -> None:
    if request.state.user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


class DatasourcePayload(BaseModel):
    name: str
    description: str = ""
    type: str
    host: str = ""
    port: Optional[int] = 0
    username: str = ""
    password: str = ""
    database: str = ""
    dbSchema: str = ""
    extraJdbc: str = ""
    ssl: bool = False


@router.post("/sync")
async def trigger_sync(request: Request):
    """手动触发模型同步（仅管理员）。返回同步统计。"""
    _require_admin(request)
    return await sync_models_to_sqlbot()


@router.get("/status")
async def status():
    """返回 SQLBot 连接与同步状态（设置面板用）。"""
    return await get_sqlbot_status()


@router.get("/datasource-types")
async def datasource_types():
    """返回支持的数据源类型（前端下拉用）。"""
    return {"types": sqlbot_datasource.DATASOURCE_TYPES}


@router.get("/datasources")
async def list_datasources(request: Request):
    """列出 SQLBot 数据源（仅管理员）。"""
    _require_admin(request)
    return await sqlbot_datasource.list_datasources()


@router.post("/datasources/check")
async def check_datasource(payload: DatasourcePayload, request: Request):
    """测试数据源连接（仅管理员，不保存）。"""
    _require_admin(request)
    return await sqlbot_datasource.check_datasource(payload.dict())


@router.post("/datasources")
async def add_datasource(payload: DatasourcePayload, request: Request):
    """新增数据源（仅管理员）。"""
    _require_admin(request)
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="数据源名称不能为空")
    return await sqlbot_datasource.add_datasource(payload.dict())


@router.delete("/datasources/{ds_id}")
async def delete_datasource(ds_id: int, request: Request):
    """删除数据源（仅管理员）。"""
    _require_admin(request)
    return await sqlbot_datasource.delete_datasource(ds_id)
