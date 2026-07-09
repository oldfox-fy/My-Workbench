# backend/routes/skills.py
"""
Skill 技能管理接口：前台可视化注册 / 启用禁用 / 编辑 / 删除自定义技能。

- prompt 型技能：人人可创建（提示词 + 工具白名单）。
- code   型技能：仅管理员可创建/编辑（可执行 Python 脚本）。

所有写操作后会热重载注册表（app.state.skill_registry），无需重启服务。
另外附带本机身份（管理员/普通用户）读写接口。
"""
import re
from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from urllib.parse import quote
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

from backend.db import skills as skills_db
from backend.db.user_settings import get_user_role, set_user_role, require_admin
from backend.services import skill_package

router = APIRouter(prefix="/api/skills", tags=["skills"])

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


class SkillRequest(BaseModel):
    name: str
    title: str
    description: str = ""
    skill_type: str = "prompt"          # 'prompt' | 'code'
    enabled: bool = True
    instruction: str = ""               # prompt 型：注入的技能指令
    tools: List[str] = []               # 允许调用的工具白名单（权限划分）
    code: str = ""                      # code 型：Python 源码
    parameters: Dict[str, Any] = Field(default_factory=dict)  # code 型：JSON Schema
    isolated: bool = False              # 上下文隔离


class ToggleRequest(BaseModel):
    enabled: bool


class RoleRequest(BaseModel):
    role: str


def _get_registry(request: Request):
    return getattr(request.app.state, "skill_registry", None)


async def _reload(request: Request):
    reg = _get_registry(request)
    if reg:
        await reg.reload()


def _validate(req: SkillRequest):
    if not req.name or not _NAME_RE.match(req.name):
        raise HTTPException(400, "技能标识只能包含字母、数字、下划线、连字符，且以字母开头")
    if not req.title.strip():
        raise HTTPException(400, "技能显示名不能为空")
    if req.skill_type not in ("prompt", "code"):
        raise HTTPException(400, "技能类型只能是 prompt 或 code")
    if req.skill_type == "code" and "def run" not in (req.code or ""):
        raise HTTPException(400, "可执行代码技能必须定义 run(**kwargs) 函数")


# ---------- 身份 ----------

@router.get("/user-role")
async def read_user_role():
    return {"role": await get_user_role()}


@router.post("/user-role")
async def write_user_role(req: RoleRequest):
    role = await set_user_role(req.role)
    return {"role": role}


# ---------- 技能 CRUD ----------

@router.get("")
async def list_all():
    return {"skills": await skills_db.list_skills()}


@router.get("/{skill_id}")
async def get_one(skill_id: int):
    skill = await skills_db.get_skill(skill_id)
    if not skill:
        raise HTTPException(404, "技能不存在")
    return skill


@router.get("/{skill_id}/export")
async def export_package(skill_id: int):
    """把指定技能导出为 SKILL.md 压缩包，便于分享 / 迁移。"""
    skill = await skills_db.get_skill(skill_id)
    if not skill:
        raise HTTPException(404, "技能不存在")
    data = skill_package.build_skill_zip(skill)
    filename = f"{skill['name']}.zip"
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


@router.post("")
async def create_or_ignore(req: SkillRequest, request: Request):
    _validate(req)
    if req.skill_type == "code":
        await require_admin()
    existing = await skills_db.get_skill_by_name(req.name)
    if existing:
        raise HTTPException(400, f"技能标识「{req.name}」已存在")
    skill = await skills_db.create_skill(req.model_dump())
    await _reload(request)
    return skill


@router.post("/import")
async def import_package(request: Request, file: UploadFile = File(...), overwrite: bool = False):
    """上传本地 skill 压缩包（含 SKILL.md）注册技能。name 冲突时按 overwrite 决定覆盖或报错。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传的压缩包为空")
    try:
        parsed = skill_package.parse_skill_zip(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        req = SkillRequest(**parsed)
    except Exception as e:
        raise HTTPException(400, f"技能元数据不合法：{e}")
    _validate(req)
    if req.skill_type == "code":
        await require_admin()

    existing = await skills_db.get_skill_by_name(req.name)
    if existing:
        if not overwrite:
            raise HTTPException(409, f"技能标识「{req.name}」已存在")
        # 覆盖既有技能（改为 code 型或原为 code 型需管理员）
        if req.skill_type == "code" or existing["skill_type"] == "code":
            await require_admin()
        skill = await skills_db.update_skill(existing["id"], req.model_dump())
    else:
        skill = await skills_db.create_skill(req.model_dump())
    await _reload(request)
    return skill



@router.put("/{skill_id}")
async def update_one(skill_id: int, req: SkillRequest, request: Request):
    _validate(req)
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    # 编辑 code 型（或把技能改为 code 型）需要管理员
    if req.skill_type == "code" or old["skill_type"] == "code":
        await require_admin()
    # 名称唯一性（改名时）
    if req.name != old["name"]:
        dup = await skills_db.get_skill_by_name(req.name)
        if dup:
            raise HTTPException(400, f"技能标识「{req.name}」已存在")
    skill = await skills_db.update_skill(skill_id, req.model_dump())
    await _reload(request)
    return skill


@router.post("/{skill_id}/toggle")
async def toggle(skill_id: int, req: ToggleRequest, request: Request):
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    skill = await skills_db.set_enabled(skill_id, req.enabled)
    await _reload(request)
    return skill


@router.delete("/{skill_id}")
async def remove(skill_id: int, request: Request):
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    if old["skill_type"] == "code":
        await require_admin()
    await skills_db.delete_skill(skill_id)
    await _reload(request)
    return {"status": "ok"}
