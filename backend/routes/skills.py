# backend/routes/skills.py
"""
Skill 技能管理接口：前台可视化注册 / 启用禁用 / 编辑 / 删除自定义技能。

- prompt 型技能：人人可创建（提示词 + 工具白名单）。
- code   型技能：仅管理员可创建/编辑（可执行 Python 脚本）。

所有写操作后会热重载注册表（app.state.skill_registry），无需重启服务。
另外附带本机身份（管理员/普通用户）读写接口。
"""
import io
import json
import os
import re
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from urllib.parse import quote
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

import backend
from backend.bootstrap import logger
from backend.db import skills as skills_db
from backend.db.kb_settings import get_setting, set_setting
from backend.auth import get_current_user, get_current_admin
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


class SkillHubInstallRequest(BaseModel):
    slug: str                            # 技能 slug，如 lux3d-cn
    namespace: str = ""                  # 命名空间 handle，如 user_97275c6e
    api_key: str = ""


class ApiKeyRequest(BaseModel):
    api_key: str = ""


# skillhub.cn 后端 API（与 npm 的 skillhub CLI 不同——那是国际站 skills.palebluedot.live）
_SKILLHUB_API = "https://api.skillhub.cn"
_SKILLHUB_HEADERS = {
    "User-Agent": "MyWorkbench/1.0",
    "Referer": "https://skillhub.cn/",
}
# slug / namespace 仅允许安全字符（避免路径/URL 注入）
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_name(name: str) -> str:
    """把 SkillHub 技能名规整为 My Workbench 合法标识（字母开头，字母/数字/下划线/连字符）。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "-", (name or "").strip()).strip("-")
    if not name:
        name = "skillhub"
    if name[0].isdigit():
        name = "sk_" + name
    if not _NAME_RE.match(name):
        name = "skillhub"
    return name


async def _download_skillhub(slug: str, namespace: str) -> bytes:
    """从 skillhub.cn 下载技能 zip（跟随 302 重定向到 COS）。返回 zip 字节。"""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        r = await client.get(
            f"{_SKILLHUB_API}/api/v1/download",
            params={"slug": slug, "namespace": namespace},
            headers=_SKILLHUB_HEADERS,
        )
        r.raise_for_status()
        return r.content


def _extract_skill_zip(zip_bytes: bytes, dest_dir: str) -> None:
    """安全解压技能 zip 到 dest_dir（防 zip-slip：拒绝绝对路径 / 目录穿越 / 盘符）。"""
    os.makedirs(dest_dir, exist_ok=True)
    dest_abs = os.path.abspath(dest_dir)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith(("/", "\\")):
                continue
            norm = name.replace("\\", "/")
            if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
                logger.warning(f"[skillhub] 跳过可疑压缩包条目: {name}")
                continue
            target = os.path.abspath(os.path.join(dest_dir, *norm.split("/")))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                logger.warning(f"[skillhub] 跳过越界压缩包条目: {name}")
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())


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


# ---------- 身份（由认证系统接管，详见 backend/auth.py）----------


# ---------- 技能 CRUD ----------

@router.get("")
async def list_all(request: Request):
    """列出当前用户的技能 + 系统内置技能（user_id IS NULL 的视为系统级）。"""
    user_id = request.state.user["id"]
    return {"skills": await skills_db.list_skills(user_id=user_id)}


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
    user = request.state.user
    if req.skill_type == "code" and user.get("role") != "admin":
        raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")
    existing = await skills_db.get_skill_by_name(req.name)
    if existing:
        raise HTTPException(400, f"技能标识「{req.name}」已存在")
    data = req.model_dump()
    data["user_id"] = user["id"]
    skill = await skills_db.create_skill(data)
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
        if request.state.user.get("role") != "admin":
            raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")

    existing = await skills_db.get_skill_by_name(req.name)
    if existing:
        if not overwrite:
            raise HTTPException(409, f"技能标识「{req.name}」已存在")
        # 检查所有权
        if existing.get("user_id") is not None and existing["user_id"] != request.state.user["id"]:
            raise HTTPException(403, "无权覆盖他人的技能")
        if req.skill_type == "code" or existing["skill_type"] == "code":
            if request.state.user.get("role") != "admin":
                raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")
        data = req.model_dump()
        data["user_id"] = request.state.user["id"]
        skill = await skills_db.update_skill(existing["id"], data)
    else:
        data = req.model_dump()
        data["user_id"] = request.state.user["id"]
        skill = await skills_db.create_skill(data)
    await _reload(request)
    return skill



@router.get("/skillhub/search")
async def search_skillhub(q: str = ""):
    """搜索 skillhub.cn 技能（直连其 API，非 npm 的 skillhub CLI / 国际站）。"""
    q = (q or "").strip()
    if not q:
        return {"skills": []}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 注意：skillhub.cn 的搜索参数是 keyword（q 会被忽略，返回默认推荐列表）
            r = await client.get(
                f"{_SKILLHUB_API}/api/skills",
                params={"keyword": q, "page": 1, "pageSize": 30},
                headers=_SKILLHUB_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"搜索 SkillHub 失败（HTTP {e.response.status_code}）")
    except Exception as e:
        raise HTTPException(502, f"搜索 SkillHub 失败：{e}")

    skills = ((data or {}).get("data") or {}).get("skills") or []
    out = []
    for s in skills:
        ns = s.get("namespace") or {}
        labels = s.get("labels") or {}
        out.append({
            "slug": s.get("slug", ""),
            "name": s.get("name") or s.get("slug") or "",
            "namespace": ns.get("handle", ""),
            "canonical": ns.get("canonicalName", ""),
            "description": (s.get("description") or "").strip(),
            "requires_api_key": str(labels.get("requires_api_key", "")).lower() == "true",
            "version": s.get("version", ""),
            "category": s.get("category", ""),
        })
    return {"skills": out}


@router.post("/skillhub/install")
async def install_skillhub(req: SkillHubInstallRequest, request: Request):
    """从 skillhub.cn 安装技能：下载 zip → 解压到工作区 skills/<slug>/ → 注册为 prompt 型技能。"""
    slug = (req.slug or "").strip()
    namespace = (req.namespace or "").strip()
    if not slug or not namespace:
        raise HTTPException(400, "缺少技能标识（slug 或 namespace）。请先在「搜索技能」里找到目标技能再安装")
    if not _SLUG_RE.match(slug) or not _SLUG_RE.match(namespace):
        raise HTTPException(400, "技能标识包含非法字符（仅允许字母、数字、.、_、-）")

    workspace = backend.get_user_workspace_path() or backend.workspace_path
    skills_root = os.path.join(workspace, "skills")
    os.makedirs(skills_root, exist_ok=True)
    dest_dir = os.path.join(skills_root, slug)

    # 下载技能 zip
    try:
        zip_bytes = await _download_skillhub(slug, namespace)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(404, f"技能不存在或已下架（slug={slug}, namespace={namespace}）")
        raise HTTPException(502, f"下载技能失败（HTTP {e.response.status_code}）")
    except Exception as e:
        raise HTTPException(502, f"下载技能失败：{e}")

    if not zip_bytes:
        raise HTTPException(502, "下载到的技能包为空")

    # 解压到工作区 skills/<slug>/
    if os.path.isdir(dest_dir):
        raise HTTPException(409, f"技能目录已存在：{dest_dir}。请先删除同名技能再重装")
    try:
        _extract_skill_zip(zip_bytes, dest_dir)
    except zipfile.BadZipFile:
        raise HTTPException(400, "下载的技能包不是有效的 zip")
    except OSError as e:
        raise HTTPException(500, f"写入技能文件失败：{e}")

    # 解析 SKILL.md
    try:
        parsed = skill_package.parse_skill_dir(dest_dir)
    except ValueError as e:
        raise HTTPException(400, f"解析技能失败：{e}")

    # 技能标识以 slug 为准（与目录名一致），并规整为合法 name
    parsed["name"] = _sanitize_name(slug)

    existing = await skills_db.get_skill_by_name(parsed["name"])
    if existing:
        raise HTTPException(409, f"技能标识「{parsed['name']}」已存在，请先删除同名技能再重装")

    user_id = request.state.user["id"]

    api_key = (req.api_key or "").strip()
    if api_key:
        cfg_path = os.path.join(dest_dir, "config.json")
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"api_key": api_key}, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[skillhub] 写入 config.json 失败: {e}")
        parsed["instruction"] = (parsed["instruction"] or "") + (
            f"\n\n本技能需要 API Key，已保存在 `{cfg_path}`（键 `api_key`）。"
            "执行技能脚本前请先读取该 key，并按 SKILL.md 说明注入（通常是环境变量）。"
        )

    # 持久化到 app_settings，便于后续「改 key」
    await set_setting(f"skillhub_dir:{parsed['name']}", dest_dir, user_id)
    if api_key:
        await set_setting(f"skill_api_key:{parsed['name']}", api_key, user_id)

    parsed["user_id"] = user_id
    skill = await skills_db.create_skill(parsed)
    await _reload(request)
    return skill


@router.get("/api-keys/{name}")
async def get_api_key(name: str, request: Request):
    """读取某个技能的 API Key（仅当前用户自己的，供编辑框预填）。"""
    user_id = request.state.user["id"]
    key = await get_setting(f"skill_api_key:{name}", user_id)
    return {"name": name, "api_key": key or ""}


@router.put("/api-keys/{name}")
async def set_api_key(name: str, req: ApiKeyRequest, request: Request):
    """设置 / 更新 / 清除某个技能的 API Key（写 app_settings，并同步重写技能目录的 config.json）。"""
    api_key = (req.api_key or "").strip()
    user_id = request.state.user["id"]
    await set_setting(f"skill_api_key:{name}", api_key, user_id)

    # 若该技能有 SkillHub 安装目录，同步更新 config.json 供脚本读取
    skill_dir = await get_setting(f"skillhub_dir:{name}", user_id)
    if skill_dir and os.path.isdir(skill_dir):
        cfg_path = os.path.join(skill_dir, "config.json")
        try:
            if api_key:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump({"api_key": api_key}, f, ensure_ascii=False)
            else:
                try:
                    os.remove(cfg_path)
                except OSError:
                    pass
        except OSError as e:
            logger.warning(f"[skillhub] 更新 config.json 失败: {e}")

    return {"status": "ok", "name": name, "has_key": bool(api_key)}


@router.put("/{skill_id}")
async def update_one(skill_id: int, req: SkillRequest, request: Request):
    _validate(req)
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    # 检查所有权：只能编辑自己的技能（admin 也只能编辑自己的）
    user = request.state.user
    if old.get("user_id") is not None and old["user_id"] != user["id"]:
        raise HTTPException(403, "无权修改他人的技能")
    # 编辑 code 型（或把技能改为 code 型）需要管理员
    if req.skill_type == "code" or old["skill_type"] == "code":
        if user.get("role") != "admin":
            raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")
    # 名称唯一性（改名时）
    if req.name != old["name"]:
        dup = await skills_db.get_skill_by_name(req.name)
        if dup:
            raise HTTPException(400, f"技能标识「{req.name}」已存在")
    data = req.model_dump()
    data["user_id"] = user["id"]
    skill = await skills_db.update_skill(skill_id, data)
    await _reload(request)
    return skill


@router.post("/{skill_id}/toggle")
async def toggle(skill_id: int, req: ToggleRequest, request: Request):
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    user = request.state.user
    if old.get("user_id") is not None and old["user_id"] != user["id"]:
        raise HTTPException(403, "无权操作他人的技能")
    skill = await skills_db.set_enabled(skill_id, req.enabled)
    await _reload(request)
    return skill


@router.delete("/{skill_id}")
async def remove(skill_id: int, request: Request):
    old = await skills_db.get_skill(skill_id)
    if not old:
        raise HTTPException(404, "技能不存在")
    user = request.state.user
    if old.get("user_id") is not None and old["user_id"] != user["id"]:
        raise HTTPException(403, "无权删除他人的技能")
    if old["skill_type"] == "code":
        if user.get("role") != "admin":
            raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")
    await skills_db.delete_skill(skill_id)
    await _reload(request)
    return {"status": "ok"}
