# backend/routes/user_prefs.py
"""
用户偏好设置（替代 localStorage，按用户隔离）。

所有偏好以 JSON 形式存于 app_settings 表（key=user_prefs, 按 user_id 隔离），
前端登录后加载、修改后保存，不同用户互不影响。
"""
import json
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Dict, Any, Optional
from backend.db.kb_settings import get_setting, set_setting

router = APIRouter(prefix="/api/user", tags=["user-prefs"])

_PREFS_KEY = "user_prefs"

# 默认偏好值（新用户首次登录时使用）
DEFAULT_PREFS: Dict[str, Any] = {
    "themeMode": "dark",
    "themeAccent": "",
    "themeRadius": 8,
    "themeFontSize": "16px",
    "thinking": False,
    "autoRead": False,
    "autoSwitch": False,
    "enableProfile": False,
    "activeModelId": "",
    "activeProfileId": None,
}


class PrefsUpdate(BaseModel):
    """前端可以只传要更新的字段，后端合并保存。"""
    prefs: Dict[str, Any] = {}


async def _read_prefs(user_id: int) -> Dict[str, Any]:
    raw = await get_setting(_PREFS_KEY, user_id)
    prefs = dict(DEFAULT_PREFS)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                prefs.update(stored)
        except json.JSONDecodeError:
            pass
    return prefs


async def _write_prefs(user_id: int, prefs: Dict[str, Any]):
    # 只保存与默认值不同的字段，减少存储
    diff = {k: v for k, v in prefs.items() if k in DEFAULT_PREFS and v != DEFAULT_PREFS.get(k)}
    await set_setting(_PREFS_KEY, json.dumps(diff, ensure_ascii=False), user_id)


@router.get("/prefs")
async def get_prefs(request: Request):
    """获取当前用户的所有偏好设置（与默认值合并后返回）。"""
    user_id = request.state.user["id"]
    return await _read_prefs(user_id)


@router.post("/prefs")
async def save_prefs(body: PrefsUpdate, request: Request):
    """保存偏好设置（增量合并：只更新传入的字段，其余保持不变）。"""
    user_id = request.state.user["id"]
    current = await _read_prefs(user_id)
    current.update(body.prefs)
    await _write_prefs(user_id, current)
    return {"status": "ok"}
