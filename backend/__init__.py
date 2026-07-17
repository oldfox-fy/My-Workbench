# backend/__init__.py
import sys
import contextvars
from pathlib import Path


# ── 全局默认值（向后兼容）──
workspace_dir = Path.cwd() / 'workspace'
workspace_path = str(workspace_dir)

_DEV_DEFAULT_KB = r"E:\fuyu\LearnAI\MyKg"
_DEFAULT_KB = _DEV_DEFAULT_KB if (not getattr(sys, "frozen", False) and Path(_DEV_DEFAULT_KB).is_dir()) else ""

# ── 用户级路径（contextvars，按请求注入）──
_user_kb_path: contextvars.ContextVar[str] = contextvars.ContextVar("user_kb_path", default=_DEFAULT_KB)
_user_workspace_path: contextvars.ContextVar[str] = contextvars.ContextVar("user_workspace", default=str(workspace_dir))


def set_user_kb_path(path: str):
    """设置当前请求上下文的用户知识库路径"""
    _user_kb_path.set(path)


def set_user_workspace_path(path: str):
    """设置当前请求上下文的用户工作区路径"""
    _user_workspace_path.set(path)


def get_user_kb_path() -> str:
    """获取当前请求上下文的用户知识库路径"""
    try:
        return _user_kb_path.get()
    except LookupError:
        return _DEFAULT_KB


def get_user_workspace_path() -> str:
    """获取当前请求上下文的用户工作区路径"""
    try:
        return _user_workspace_path.get()
    except LookupError:
        return str(workspace_dir)


# ── 向后兼容：模块级 kb_path 属性（供旧代码读取）──
# 新代码应使用 get_user_kb_path() / get_user_workspace_path()
kb_path = _DEFAULT_KB
