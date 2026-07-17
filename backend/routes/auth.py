# backend/routes/auth.py
"""
认证与用户管理 API：
  - /api/auth/login       — 登录
  - /api/auth/register    — 注册
  - /api/auth/me          — 当前用户信息
  - /api/auth/logout      — 登出
  - /api/auth/change-password — 修改密码
  - /api/auth/pending-count   — 待审批用户数（管理员）
  - /api/admin/users/*    — 用户管理（管理员）
  - /api/admin/audit-logs — 审计日志（管理员）
"""
import re
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.database import get_db
from backend.auth import (
    hash_password, verify_password, generate_token, create_token,
    delete_token, validate_token, log_audit,
    get_current_user, get_current_admin,
)

router = APIRouter(prefix="/api", tags=["auth"])

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_一-鿿]{2,20}$")


# ──────────── Pydantic Models ────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class RegisterRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    status: str
    kb_path: str = ""
    created_at: Optional[str] = None


class AuditLogResponse(BaseModel):
    id: int
    user_id: Optional[int] = None
    username: str
    action: str
    target: str = ""
    detail: str = ""
    ip_address: str = ""
    created_at: Optional[str] = None


# ──────────── 公开端点（无需登录）────────────

@router.post("/auth/login")
async def login(req: LoginRequest, request: Request):
    """用户名+密码登录，返回 token 和用户信息"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, password_hash, role, status, kb_path, workspace_path FROM users WHERE username = ?",
            (req.username,),
        )
        row = await cursor.fetchone()
        if not row:
            await log_audit(None, req.username, "login_failed", detail="用户名不存在")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        user_id = row[0]
        username = row[1]
        password_hash = row[2]
        role = row[3]
        status = row[4]
        kb_path = row[5] or ""
        workspace_path = row[6] or ""

        if not verify_password(req.password, password_hash):
            await log_audit(user_id, username, "login_failed", detail="密码错误")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        if status == "disabled":
            await log_audit(user_id, username, "login_failed", detail="账号已禁用")
            raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

        if status == "pending":
            await log_audit(user_id, username, "login_failed", detail="账号待审批")
            raise HTTPException(status_code=403, detail="账号正在等待管理员审批")

        token = await create_token(user_id, req.remember_me)

        ip = request.client.host if request.client else ""
        await log_audit(user_id, username, "login", detail=f"remember_me={req.remember_me}", ip_address=ip)

        return {
            "token": token,
            "user": {
                "id": user_id,
                "username": username,
                "role": role,
                "status": status,
                "kb_path": kb_path,
                "workspace_path": workspace_path,
            },
        }
    finally:
        await db.close()


@router.post("/auth/register")
async def register(req: RegisterRequest, request: Request):
    """注册新用户，默认 status=pending，角色=user"""
    if not _USERNAME_RE.match(req.username):
        raise HTTPException(status_code=400, detail="用户名需 2-20 位，仅支持字母、数字、下划线、中文")

    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM users WHERE username = ?", (req.username,))
        if await cursor.fetchone():
            raise HTTPException(status_code=409, detail="用户名已存在")

        pwd_hash, _ = hash_password(req.password)
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, role, status) VALUES (?, ?, 'user', 'pending')",
            (req.username, pwd_hash),
        )
        await db.commit()
        user_id = cursor.lastrowid

        ip = request.client.host if request.client else ""
        await log_audit(user_id, req.username, "register", detail="等待管理员审批", ip_address=ip)

        return {"message": "注册成功，请等待管理员审批", "user_id": user_id}
    finally:
        await db.close()


# ──────────── 需登录端点 ────────────

@router.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "status": user["status"],
        "kb_path": user.get("kb_path", ""),
        "workspace_path": user.get("workspace_path", ""),
    }


@router.post("/auth/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)):
    """登出，删除当前 token"""
    token = getattr(request.state, "token", None)
    if token:
        await delete_token(token)
    await log_audit(user["id"], user["username"], "logout")
    return {"message": "已登出"}


@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, user: dict = Depends(get_current_user)):
    """修改密码：需要旧密码验证"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")

        if not verify_password(req.old_password, row[0]):
            raise HTTPException(status_code=400, detail="旧密码错误")

        if len(req.new_password) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")

        new_hash, _ = hash_password(req.new_password)
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user["id"]),
        )
        await db.commit()

        await log_audit(user["id"], user["username"], "change_password")
        return {"message": "密码修改成功，请重新登录"}
    finally:
        await db.close()


@router.get("/auth/pending-count")
async def pending_count(user: dict = Depends(get_current_admin)):
    """管理员：获取待审批用户数量"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'")
        row = await cursor.fetchone()
        return {"count": row[0] if row else 0}
    finally:
        await db.close()


# ──────────── 管理员端点 ────────────

@router.get("/admin/users", response_model=List[UserResponse])
async def list_users(user: dict = Depends(get_current_admin)):
    """管理员：获取所有用户列表"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, role, status, kb_path, created_at FROM users ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "username": r[1], "role": r[2],
                "status": r[3], "kb_path": r[4] or "",
                "created_at": r[5],
            }
            for r in rows
        ]
    finally:
        await db.close()


@router.post("/admin/users/{target_id}/approve")
async def approve_user(target_id: int, request: Request, user: dict = Depends(get_current_admin)):
    """管理员：审批通过用户注册"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT username, status FROM users WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row[1] != "pending":
            raise HTTPException(status_code=400, detail="该用户不是待审批状态")

        await db.execute("UPDATE users SET status = 'active' WHERE id = ?", (target_id,))
        await db.commit()

        await log_audit(user["id"], user["username"], "approve_user", target=row[0])
        return {"message": f"已通过用户 {row[0]} 的注册审批"}
    finally:
        await db.close()


@router.post("/admin/users/{target_id}/disable")
async def disable_user(target_id: int, request: Request, user: dict = Depends(get_current_admin)):
    """管理员：禁用用户"""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="不能禁用自己")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT username FROM users WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")

        await db.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (target_id,))
        # 删除该用户所有 token（强制下线）
        await db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (target_id,))
        await db.commit()

        await log_audit(user["id"], user["username"], "disable_user", target=row[0])
        return {"message": f"已禁用用户 {row[0]}"}
    finally:
        await db.close()


@router.post("/admin/users/{target_id}/enable")
async def enable_user(target_id: int, request: Request, user: dict = Depends(get_current_admin)):
    """管理员：重新启用已禁用的用户"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT username, status FROM users WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row[1] != "disabled":
            raise HTTPException(status_code=400, detail="该用户不是禁用状态")

        await db.execute("UPDATE users SET status = 'active' WHERE id = ?", (target_id,))
        await db.commit()

        await log_audit(user["id"], user["username"], "enable_user", target=row[0])
        return {"message": f"已启用用户 {row[0]}"}
    finally:
        await db.close()


@router.delete("/admin/users/{target_id}")
async def delete_user(target_id: int, request: Request, user: dict = Depends(get_current_admin)):
    """管理员：删除用户"""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT username FROM users WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")

        await db.execute("DELETE FROM users WHERE id = ?", (target_id,))
        await db.commit()

        await log_audit(user["id"], user["username"], "delete_user", target=row[0])
        return {"message": f"已删除用户 {row[0]}"}
    finally:
        await db.close()


@router.post("/admin/users/{target_id}/reset-password")
async def reset_user_password(target_id: int, req: ResetPasswordRequest, request: Request, user: dict = Depends(get_current_admin)):
    """管理员：重置用户密码"""
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT username FROM users WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")

        new_hash, _ = hash_password(req.new_password)
        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, target_id))
        # 强制下线
        await db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (target_id,))
        await db.commit()

        await log_audit(user["id"], user["username"], "reset_password", target=row[0])
        return {"message": f"已重置用户 {row[0]} 的密码"}
    finally:
        await db.close()


# ──────────── 审计日志 ────────────

@router.get("/admin/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    request: Request,
    action: Optional[str] = Query(None, description="按操作类型过滤"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_admin),
):
    """管理员：查看审计日志"""
    db = await get_db()
    try:
        if action:
            cursor = await db.execute(
                "SELECT id, user_id, username, action, target, detail, ip_address, created_at "
                "FROM audit_logs WHERE action = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (action, limit, offset),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, username, action, target, detail, ip_address, created_at "
                "FROM audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "username": r[2],
                "action": r[3], "target": r[4], "detail": r[5],
                "ip_address": r[6], "created_at": r[7],
            }
            for r in rows
        ]
    finally:
        await db.close()
