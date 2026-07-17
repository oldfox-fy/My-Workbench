# backend/auth.py
"""
认证与授权核心模块：
  - 密码哈希/验证
  - Token 生成/验证
  - FastAPI 依赖注入（get_current_user / get_current_admin）
  - 审计日志
"""
import hashlib
import secrets
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Header, HTTPException, Request, Depends
from backend.database import get_db


# ──────────── 密码工具 ────────────

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """返回 (完整存储值, salt)。存储格式: salt:hash"""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}", salt


def verify_password(password: str, stored: str) -> bool:
    """验证密码。stored 格式为 salt:hash"""
    try:
        salt, expected = stored.split(":", 1)
        actual = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
        return actual == expected
    except (ValueError, IndexError):
        return False


# ──────────── Token 工具 ────────────

def generate_token() -> str:
    return secrets.token_hex(32)


async def validate_token(token: str) -> Optional[dict]:
    """验证 token 并返回用户 dict，无效/过期返回 None"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT u.id, u.username, u.role, u.status, u.kb_path, u.workspace_path, t.expires_at "
            "FROM auth_tokens t JOIN users u ON t.user_id = u.id "
            "WHERE t.token = ?",
            (token,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        expires_at = row[5]
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if exp < datetime.now():
                    # 删除过期 token
                    await db.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
                    await db.commit()
                    return None
            except (ValueError, TypeError):
                pass

        return {
            "id": row[0],
            "username": row[1],
            "role": row[2],
            "status": row[3],
            "kb_path": row[4] or "",
            "workspace_path": row[5] or "",
        }
    finally:
        await db.close()


async def create_token(user_id: int, remember_me: bool = False) -> str:
    """创建 token 并存入数据库，返回 token 字符串"""
    token = generate_token()
    if remember_me:
        expires_at = datetime.now() + timedelta(days=7)
    else:
        expires_at = datetime.now() + timedelta(hours=24)

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at.isoformat()),
        )
        await db.commit()
        return token
    finally:
        await db.close()


async def delete_token(token: str) -> None:
    """删除 token（登出）"""
    db = await get_db()
    try:
        await db.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
        await db.commit()
    finally:
        await db.close()


# ──────────── 审计日志 ────────────

async def log_audit(
    user_id: Optional[int],
    username: str,
    action: str,
    target: str = "",
    detail: str = "",
    ip_address: str = "",
):
    """写入操作审计日志（非关键路径，静默失败）"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_logs (user_id, username, action, target, detail, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, action, target, detail, ip_address),
        )
        await db.commit()
        await db.close()
    except Exception:
        pass  # 审计日志写入失败不阻塞主流程


# ──────────── FastAPI 依赖注入 ────────────

async def get_current_user(
    request: Request,
    authorization: str = Header(None, alias="Authorization"),
) -> dict:
    """从 Authorization header 解析 token 并返回当前用户，未登录抛 401"""
    token = None
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        else:
            token = authorization

    if not token:
        raise HTTPException(status_code=401, detail="请先登录")

    user = await validate_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    if user["status"] == "disabled":
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    # 将 user 注入 request.state 供下游使用
    request.state.user = user
    request.state.token = token
    return user


async def get_current_admin(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """要求当前用户为管理员，否则抛 403。配合 Depends(get_current_admin) 使用。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="该操作仅管理员可用")
    return user


async def get_current_active_user(user: dict) -> dict:
    """要求当前用户状态为 active（非 pending）。配合 Depends(get_current_user) 使用。"""
    if user.get("status") == "pending":
        raise HTTPException(status_code=403, detail="账号正在等待管理员审批")
    return user
