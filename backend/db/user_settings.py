# backend/db/user_settings.py
"""
本机用户身份（权限）读写，存于 app_settings 键值表。

单机桌面应用无登录鉴权体系，这里的「身份」是本机角色开关：
- admin：管理员，可创建/编辑「可执行代码技能」（code 型 Skill）。
- user ：普通用户，只能使用与创建「提示词技能」（prompt 型 Skill）。

首次未设置时默认为 admin（本机拥有者）。
"""
from fastapi import HTTPException
from backend.db.kb_settings import get_setting, set_setting

_USER_ROLE_KEY = "user_role"
_DEFAULT_ROLE = "admin"
_VALID_ROLES = {"admin", "user"}


async def get_user_role() -> str:
    """读取当前本机身份，未设置时返回默认（admin）。"""
    raw = await get_setting(_USER_ROLE_KEY)
    if raw in _VALID_ROLES:
        return raw
    return _DEFAULT_ROLE


async def set_user_role(role: str) -> str:
    """设置本机身份，仅接受 admin / user。"""
    if role not in _VALID_ROLES:
        raise HTTPException(400, "无效的身份，仅支持 admin / user")
    await set_setting(_USER_ROLE_KEY, role)
    return role


async def require_admin() -> None:
    """校验当前为管理员，否则抛 403。用于危险操作（如写入代码技能）。"""
    role = await get_user_role()
    if role != "admin":
        raise HTTPException(403, "该操作仅管理员可用（可执行代码技能）")
